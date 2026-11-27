import re
from decimal import Decimal, InvalidOperation
from typing import Any

from config.constants import DEFAULT_RESTAURANT_HOTLINE
from django.conf import settings
from django.http import Http404, HttpRequest
from menu.models import Dish
from menu.selectors import search_dishes_for_agent
from orders.exceptions import (
    CartEmptyError,
    CartForbiddenError,
    DishNotFoundError,
    DishUnavailableError,
    InvalidOptionError,
)
from orders.services import CartService

from support.models import ChatSession, TicketReason
from support.services import (
    ChatSessionService,
    SupportDeskError,
    check_order_status_for_request,
    get_faq_answers,
)

_STRICT_INT_RE = re.compile(r'-?[0-9]+')
_INT32_MAX = 2**31 - 1
_MAX_INT_STR_LEN = 12
_DISH_STOP_WORDS = frozenset({
    'піца',
    'рол',
    'бургер',
    'салат',
    'боул',
    'сет',
    'десерт',
    'та',
    'і',
    'з',
    'в',
    'для',
    'на',
})
_PUNCT_STRIP = '.,!?:;"\'«»()[]{}'


def _parse_strict_int(
    raw: Any,
    *,
    min_value: int = 1,
    max_value: int = _INT32_MAX,
) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text or len(text) > _MAX_INT_STR_LEN or not _STRICT_INT_RE.fullmatch(text):
            return None
        val = int(text)
    else:
        return None
    if min_value <= val <= max_value:
        return val
    return None


def _extract_meaningful_keywords(text_cf: str) -> list[str]:
    words = [w.strip(_PUNCT_STRIP) for w in text_cf.split()]
    return [w for w in words if len(w) > 2 and w not in _DISH_STOP_WORDS]


def _get_hotline() -> str:
    hotline = getattr(settings, 'RESTAURANT_HOTLINE', DEFAULT_RESTAURANT_HOTLINE)
    return str(hotline or DEFAULT_RESTAURANT_HOTLINE)


def _find_dish(dish_id: int | None = None, dish_name: str | None = None) -> Dish | None:
    available_qs = Dish.objects.filter(
        is_available=True,
        category__is_active=True,
    ).select_related('category')

    parsed_id = _parse_strict_int(dish_id, min_value=1)
    if parsed_id is not None:
        dish = available_qs.filter(id=parsed_id).first()
        if dish is not None:
            return dish

    if dish_name and isinstance(dish_name, str):
        name = ' '.join(dish_name.strip().split())
        if not name:
            return None
        target_cf = name.casefold()
        dishes = list(available_qs.order_by('id'))

        for dish in dishes:
            if dish.title.strip().casefold() == target_cf:
                return dish

        substring_matches = [
            dish for dish in dishes if target_cf in dish.title.casefold()
        ]
        if substring_matches:
            substring_matches.sort(key=lambda d: (len(d.title), d.id))
            return substring_matches[0]

        keywords = _extract_meaningful_keywords(target_cf)
        if keywords:
            keyword_matches = [
                dish
                for dish in dishes
                if all(kw in dish.title.casefold() for kw in keywords)
            ]
            if keyword_matches:
                keyword_matches.sort(key=lambda d: (len(d.title), d.id))
                return keyword_matches[0]

    return None


def _item_matches_option_id(item: Any, option_id: int) -> bool:
    opts = item.selected_options if isinstance(item.selected_options, list) else []
    for opt in opts:
        if not isinstance(opt, dict):
            continue
        raw_id = opt.get('option_id') if opt.get('option_id') is not None else opt.get('id')
        if _parse_strict_int(raw_id, min_value=1) == option_id:
            return True
    return False


def _item_matches_option_name(item: Any, option_cf: str, *, exact: bool) -> bool:
    opts = item.selected_options if isinstance(item.selected_options, list) else []
    for opt in opts:
        if not isinstance(opt, dict):
            continue
        name = str(opt.get('name') or '').strip().casefold()
        if not name:
            continue
        if exact and name == option_cf:
            return True
        if not exact and option_cf in name:
            return True
    return False


def _resolve_cart_item(
    cart: Any,
    arguments: dict[str, Any],
) -> tuple[Any | None, dict[str, Any] | None]:
    raw_item_id = arguments.get('cart_item_id')
    if raw_item_id in (None, ''):
        raw_item_id = arguments.get('item_id')
    raw_dish_id = arguments.get('dish_id')
    dish_name = arguments.get('dish_name')
    raw_option_id = arguments.get('option_id')
    option_name = arguments.get('option_name')

    item_id: int | None = None
    if raw_item_id not in (None, ''):
        item_id = _parse_strict_int(raw_item_id, min_value=1)
        if item_id is None:
            msg = 'Некоректний ідентифікатор позиції кошика.'
            return None, {'success': False, 'error': msg, 'message': msg}

    dish_id: int | None = None
    if raw_dish_id not in (None, ''):
        dish_id = _parse_strict_int(raw_dish_id, min_value=1)

    option_id: int | None = None
    if raw_option_id not in (None, ''):
        option_id = _parse_strict_int(raw_option_id, min_value=1)
        if option_id is None:
            msg = 'Некоректний ідентифікатор опції.'
            return None, {'success': False, 'error': msg, 'message': msg}

    cart_items = list(cart.items.select_related('dish').all())

    if item_id is not None:
        matched_by_id = [it for it in cart_items if it.id == item_id]
        if matched_by_id:
            return matched_by_id[0], None
        msg = f'Позицію «{item_id}» не знайдено у вашому кошику.'
        return None, {'success': False, 'error': msg, 'message': msg}

    candidates: list[Any] = []
    if dish_id is not None:
        candidates = [it for it in cart_items if it.dish_id == dish_id]

    if not candidates and dish_name and isinstance(dish_name, str):
        name = ' '.join(dish_name.strip().split())
        if name:
            target_cf = name.casefold()
            candidates = [
                it
                for it in cart_items
                if it.dish and it.dish.title.strip().casefold() == target_cf
            ]
            if not candidates:
                candidates = [
                    it
                    for it in cart_items
                    if it.dish and target_cf in it.dish.title.casefold()
                ]
            if not candidates:
                keywords = _extract_meaningful_keywords(target_cf)
                if keywords:
                    candidates = [
                        it
                        for it in cart_items
                        if it.dish
                        and all(kw in it.dish.title.casefold() for kw in keywords)
                    ]

    if option_id is not None and candidates:
        candidates = [it for it in candidates if _item_matches_option_id(it, option_id)]

    if option_name and isinstance(option_name, str) and candidates:
        opt_cf = option_name.strip().casefold()
        if opt_cf:
            exact_opts = [
                it for it in candidates if _item_matches_option_name(it, opt_cf, exact=True)
            ]
            candidates = exact_opts or [
                it for it in candidates if _item_matches_option_name(it, opt_cf, exact=False)
            ]

    if not candidates:
        identifier = dish_name or raw_dish_id or raw_item_id or 'вказану страву'
        msg = f'Позицію «{identifier}» не знайдено у вашому кошику.'
        return None, {'success': False, 'error': msg, 'message': msg}

    if len(candidates) == 1:
        return candidates[0], None

    candidates_payload = [
        {
            'cart_item_id': it.id,
            'item_id': it.id,
            'dish_id': it.dish_id,
            'title': it.dish.title if it.dish else 'Страва',
            'quantity': it.quantity,
            'selected_options': it.selected_options or [],
        }
        for it in candidates
    ]
    ambiguous_msg = (
        'У кошику знайдено кілька позицій цієї страви з різними опціями. '
        'Уточніть cart_item_id, option_id або option_name.'
    )
    return None, {
        'success': False,
        'error': ambiguous_msg,
        'message': ambiguous_msg,
        'ambiguous': True,
        'candidates': candidates_payload,
    }


SUPPORT_AGENT_TOOLS: list[dict[str, Any]] = [
    {
        'type': 'function',
        'function': {
            'name': 'check_order_status',
            'description': (
                'Отримати актуальний статус доставки замовлення, орієнтовний час '
                'прибуття (ETA) та адресу за номером замовлення.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'order_id': {
                        'type': 'string',
                        'description': (
                            'Номер замовлення у форматі DB-0001 або числовий ID '
                            "(наприклад: 'DB-0001' або '1042')"
                        ),
                    }
                },
                'required': ['order_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_dishes',
            'description': (
                'Пошук та перегляд страв і напоїв у меню за назвою, категорією '
                "(наприклад 'drinks' для напоїв, 'pizza', 'sushi', 'burgers', "
                "'desserts', 'bowls-salads', 'sets'), максимальною ціною або "
                'дієтичними параметрами. Обов’язково викликай цей інструмент, '
                'коли клієнт просить порадити або порекомендувати страви чи напої!'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': (
                            "Ключове слово для пошуку (наприклад: 'піца', "
                            "'суші', 'бургер', 'салат', 'лимонад')"
                        ),
                    },
                    'category_slug': {
                        'type': 'string',
                        'description': (
                            "Слаг категорії: 'drinks' (для напоїв, фрешів, "
                            "чаю, кави), 'pizza', 'sushi', 'burgers', "
                            "'desserts', 'bowls-salads', 'sets'"
                        ),
                    },
                    'max_price': {
                        'type': 'number',
                        'description': 'Максимальна ціна страви у гривнях',
                    },
                    'is_vegetarian': {
                        'type': 'boolean',
                        'description': 'Фільтр лише вегетаріанських страв',
                    },
                    'max_calories': {
                        'type': 'integer',
                        'description': 'Максимальна калорійність',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_faq_answer',
            'description': (
                'Пошук у базі знань Double Bite: умови доставки, мінімальна сума '
                'замовлення, години роботи, способи оплати, повернення.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': (
                            "Запитання або тема (наприклад: 'вартість доставки', "
                            "'графік роботи')"
                        ),
                    }
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'add_to_cart',
            'description': (
                'Автономно додати страву та обраний модифікатор до кошика клієнта. '
                'Обов’язково викликай цей інструмент, коли клієнт просить додати '
                'будь-яку страву! Можна вказувати dish_id АБО назву страви dish_name.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'dish_id': {
                        'type': 'integer',
                        'description': 'Числовий ID страви (якщо відомий)',
                    },
                    'dish_name': {
                        'type': 'string',
                        'description': (
                            'Назва або частина назви страви для додавання '
                            "(наприклад: 'Піца Буррата', 'Пепероні')"
                        ),
                    },
                    'quantity': {
                        'type': 'integer',
                        'description': 'Кількість порцій (за замовчуванням 1)',
                    },
                    'option_id': {
                        'type': 'integer',
                        'description': "ID опції чи розміру страви (необов'язково)",
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'update_cart_quantity',
            'description': (
                'Змінити кількість порцій існуючої страви у кошику клієнта '
                '(наприклад, зменшити до 2 або збільшити). Обов’язково викликай '
                'цей інструмент, коли клієнт просить змінити/зменшити кількість! '
                'Якщо quantity=0, страва видаляється.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'quantity': {
                        'type': 'integer',
                        'description': (
                            'Нова підсумкова кількість порцій '
                            '(якщо 0 — страва видаляється)'
                        ),
                    },
                    'dish_id': {
                        'type': 'integer',
                        'description': 'ID страви у кошику',
                    },
                    'dish_name': {
                        'type': 'string',
                        'description': (
                            'Назва страви у кошику '
                            "(наприклад: 'Піца Веганська з Артишоками')"
                        ),
                    },
                    'item_id': {
                        'type': 'integer',
                        'description': 'ID позиції у кошику (якщо відомий)',
                    },
                    'cart_item_id': {
                        'type': 'integer',
                        'description': (
                            'ID позиції CartItem у кошику (аліас для item_id)'
                        ),
                    },
                    'option_id': {
                        'type': 'integer',
                        'description': (
                            "ID опції страви для точного вибору позиції (необов'язково)"
                        ),
                    },
                    'option_name': {
                        'type': 'string',
                        'description': (
                            'Назва опції страви для точного вибору позиції '
                            "(необов'язково)"
                        ),
                    },
                },
                'required': ['quantity'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'remove_from_cart',
            'description': (
                'Видалити позицію або страву з кошика клієнта за назвою '
                'dish_name, dish_id, item_id/cart_item_id або опцією.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'dish_name': {
                        'type': 'string',
                        'description': (
                            'Назва страви для видалення з кошика '
                            "(наприклад: 'Піца Веганська')"
                        ),
                    },
                    'dish_id': {
                        'type': 'integer',
                        'description': 'ID страви для видалення, якщо відомий',
                    },
                    'item_id': {
                        'type': 'integer',
                        'description': 'ID позиції CartItem у кошику',
                    },
                    'cart_item_id': {
                        'type': 'integer',
                        'description': (
                            'ID позиції CartItem у кошику (аліас для item_id)'
                        ),
                    },
                    'option_id': {
                        'type': 'integer',
                        'description': (
                            "ID опції страви для точного вибору позиції (необов'язково)"
                        ),
                    },
                    'option_name': {
                        'type': 'string',
                        'description': (
                            'Назва опції страви для точного вибору позиції '
                            "(необов'язково)"
                        ),
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'view_cart',
            'description': (
                'Отримати поточний склад кошика клієнта (список страв, порції, '
                'опції, проміжна та підсумкова сума).'
            ),
            'parameters': {
                'type': 'object',
                'properties': {},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'escalate_to_operator',
            'description': (
                'Викликати чергового адміністратора/оператора ресторану при скарзі, '
                'конфлікті, нездатності вирішити проблему чи прямому проханні клієнта.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'reason': {
                        'type': 'string',
                        'description': (
                            "Причина ескалації: 'COMPLAINT', 'ORDER_ISSUE', "
                            "'HUMAN_REQUESTED', 'BOT_CONFUSED'"
                        ),
                    },
                    'details': {
                        'type': 'string',
                        'description': (
                            'Стислий опис проблеми та суті звернення для персоналу'
                        ),
                    },
                    'order_id': {
                        'type': 'string',
                        'description': (
                            'Номер замовлення (наприклад: DB-0001 або 1042), '
                            'якщо відомий або згаданий клієнтом'
                        ),
                    },
                    'customer_phone': {
                        'type': 'string',
                        'description': (
                            'Контактний телефон клієнта (якщо відомий або наданий)'
                        ),
                    },
                },
                'required': ['reason', 'details'],
            },
        },
    },
]


def _handle_check_order_status(
    arguments: dict[str, Any],
    request: HttpRequest | None,
) -> dict[str, Any]:
    order_id = arguments.get('order_id')
    if (
        order_id is None
        or isinstance(order_id, bool)
        or (isinstance(order_id, str) and not order_id.strip())
        or order_id == 0
    ):
        return {'error': 'Будь ласка, вкажіть номер замовлення.'}
    if request is None:
        return {'error': 'Запит не надано для перевірки прав доступу.'}
    return check_order_status_for_request(request, order_id)


def _handle_search_dishes(arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments.get('query') or '')
    category_slug = arguments.get('category_slug')
    if category_slug is not None and not isinstance(category_slug, str):
        category_slug = str(category_slug)

    raw_max_price = arguments.get('max_price')
    max_price: Decimal | None = None
    if raw_max_price not in (None, ''):
        if isinstance(raw_max_price, bool):
            return {'error': 'Некоректне значення max_price.'}
        try:
            dec = (
                raw_max_price
                if isinstance(raw_max_price, Decimal)
                else Decimal(str(raw_max_price))
            )
            if not dec.is_finite():
                return {'error': 'Некоректне значення max_price.'}
            max_price = dec
        except (InvalidOperation, ValueError, TypeError):
            return {'error': 'Некоректне значення max_price.'}

    raw_is_veg = arguments.get('is_vegetarian')
    is_vegetarian: bool | None = raw_is_veg if isinstance(raw_is_veg, bool) else None

    raw_max_cal = arguments.get('max_calories')
    max_calories: int | None = None
    if raw_max_cal not in (None, ''):
        max_calories = _parse_strict_int(raw_max_cal, min_value=0, max_value=_INT32_MAX)
        if max_calories is None:
            return {'error': 'Некоректне значення max_calories.'}

    dishes = search_dishes_for_agent(
        query=query,
        category_slug=category_slug,
        max_price=max_price,
        is_vegetarian=is_vegetarian,
        max_calories=max_calories,
    )
    return {
        'count': len(dishes),
        'dishes': dishes,
    }


def _handle_get_faq_answer(arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments.get('query') or '')
    answers = get_faq_answers(query)
    if not answers:
        return {
            'faqs': [],
            'message': 'За вашим запитом інформації в базі знань не знайдено.',
        }
    return {
        'faqs': answers,
        'count': len(answers),
    }


def _handle_escalate_to_operator(
    arguments: dict[str, Any],
    request: HttpRequest | None,
    session: ChatSession | None,
) -> dict[str, Any]:
    reason = arguments.get('reason', TicketReason.HUMAN_REQUESTED)
    details = str(
        arguments.get('details') or 'Клієнт звернувся за допомогою оператора.'
    )[:1000]
    order_id_raw = arguments.get('order_id')
    if order_id_raw is None or isinstance(order_id_raw, bool):
        order_ref = ''
    else:
        order_ref = str(order_id_raw).strip()
    customer_phone = str(arguments.get('customer_phone') or '')

    order = None
    if order_ref and request is not None:
        order_data = check_order_status_for_request(request, order_ref)
        if 'error' not in order_data and order_data.get('order_id'):
            from orders.models import Order

            order = Order.objects.filter(id=order_data['order_id']).first()

    if session is None and request is not None:
        session = ChatSessionService.get_or_create_active_session(request)

    if session is not None:
        try:
            ticket = ChatSessionService.escalate_session(
                session=session,
                reason=reason,
                details=details,
                order=order,
                customer_phone=customer_phone,
            )
        except SupportDeskError as exc:
            return {'error': str(exc)}
        ticket_id = ticket.id
        ticket_status = ticket.status
    else:
        ticket_id = None
        ticket_status = 'PENDING'

    return {
        'ticket_id': ticket_id,
        'status': ticket_status,
        'assigned_role': 'RESTAURANT_ADMIN',
        'support_phone': _get_hotline(),
        'message': (
            'Звернення зареєстровано та передано черговому адміністратору ресторану.'
        ),
    }


def _handle_add_to_cart(
    arguments: dict[str, Any],
    request: HttpRequest | None,
) -> dict[str, Any]:
    if request is None:
        return {'error': 'Неможливо оновити кошик: сесія не знайдена.'}

    dish_id_raw = arguments.get('dish_id')
    dish_id = (
        _parse_strict_int(dish_id_raw, min_value=1)
        if dish_id_raw not in (None, '')
        else None
    )
    dish_name = arguments.get('dish_name')

    qty_raw = arguments.get('quantity')
    if qty_raw in (None, ''):
        quantity = 1
    else:
        parsed_qty = _parse_strict_int(
            qty_raw, min_value=-_INT32_MAX, max_value=_INT32_MAX
        )
        if parsed_qty is None:
            return {'error': 'Некоректна кількість порцій.'}
        quantity = 1 if parsed_qty <= 0 else min(parsed_qty, 99)

    option_id_raw = arguments.get('option_id')
    option_id: int | None = None
    if option_id_raw not in (None, ''):
        option_id = _parse_strict_int(option_id_raw, min_value=1)
        if option_id is None:
            return {'error': 'Некоректний ідентифікатор опції.'}

    dish = _find_dish(dish_id=dish_id, dish_name=dish_name)
    if not dish:
        identifier = dish_name or dish_id_raw or 'невідома страва'
        return {
            'error': (
                f'Страву «{identifier}» не знайдено в меню. '
                'Будь ласка, перевірте назву або скористайтеся пошуком.'
            )
        }

    try:
        item = CartService.add_dish(
            request=request,
            dish_id=dish.id,
            quantity=quantity,
            option_id=option_id,
        )
        if hasattr(request, 'session'):
            request.session.save()

        cart = CartService.get_cart(request)
        total_amount = float(cart.total_amount) if cart else 0.0
        items_count = CartService.get_items_count(request)

        return {
            'success': True,
            'action': 'add_to_cart',
            'dish_id': dish.id,
            'dish_title': item.dish.title,
            'quantity': item.quantity,
            'added_quantity': quantity,
            'unit_price': float(item.unit_price),
            'total_amount': total_amount,
            'cart_items_count': items_count,
            'message': (
                f'Страву «{item.dish.title}» ({quantity} шт.) '
                'успішно додано до кошика.'
            ),
        }
    except (
        Http404,
        CartEmptyError,
        CartForbiddenError,
        DishNotFoundError,
        DishUnavailableError,
        InvalidOptionError,
    ) as exc:
        return {'error': str(exc)}
    except (ValueError, KeyError, TypeError) as exc:
        return {'error': f'Помилка при додаванні до кошика: {exc!s}'}


def _handle_update_cart_quantity(
    arguments: dict[str, Any],
    request: HttpRequest | None,
) -> dict[str, Any]:
    if request is None:
        return {
            'success': False,
            'error': 'Неможливо оновити кошик: сесія не знайдена.',
        }

    quantity = _parse_strict_int(
        arguments.get('quantity'),
        min_value=0,
        max_value=_INT32_MAX,
    )
    if quantity is None:
        return {
            'success': False,
            'error': 'Некоректна кількість порцій. Вкажіть ціле число від 0.',
        }

    cart = CartService.get_cart(request)
    if not cart or cart.items.count() == 0:
        return {
            'success': False,
            'error': 'Ваш кошик наразі порожній, немає страв для зміни кількості.',
        }

    target_item, resolve_error = _resolve_cart_item(cart, arguments)
    if resolve_error is not None or target_item is None:
        return resolve_error or {
            'success': False,
            'error': 'Позицію не знайдено у вашому кошику.',
        }

    dish_title = target_item.dish.title
    dish_id_val = target_item.dish_id
    target_item_id = target_item.id

    if quantity <= 0:
        CartService.remove_item(request, target_item_id)
        actual_quantity = 0
        action_desc = f'Позицію «{dish_title}» видалено з кошика.'
    else:
        updated_item = CartService.update_item_quantity(
            request, target_item_id, quantity
        )
        if updated_item is None:
            return {
                'success': False,
                'error': f'Позицію «{dish_title}» не знайдено у вашому кошику.',
            }
        actual_quantity = updated_item.quantity
        action_desc = (
            f'Кількість страви «{dish_title}» успішно оновлено '
            f'до {actual_quantity} шт.'
        )

    if hasattr(request, 'session'):
        request.session.save()

    cart = CartService.get_cart(request)
    total_amount = float(cart.total_amount) if cart else 0.0
    items_count = CartService.get_items_count(request)

    return {
        'success': True,
        'action': 'update_cart_quantity',
        'cart_item_id': target_item_id,
        'dish_id': dish_id_val,
        'dish_title': dish_title,
        'quantity': actual_quantity,
        'total_amount': total_amount,
        'cart_items_count': items_count,
        'message': action_desc,
    }


def _handle_remove_from_cart(
    arguments: dict[str, Any],
    request: HttpRequest | None,
) -> dict[str, Any]:
    if request is None:
        return {
            'success': False,
            'error': 'Неможливо оновити кошик: сесія не знайдена.',
        }

    cart = CartService.get_cart(request)
    if not cart or cart.items.count() == 0:
        return {
            'success': False,
            'error': 'Кошик порожній.',
            'message': 'Кошик порожній.',
        }

    target_item, resolve_error = _resolve_cart_item(cart, arguments)
    if resolve_error is not None or target_item is None:
        return resolve_error or {
            'success': False,
            'error': 'Позицію не знайдено у вашому кошику.',
            'message': 'Позицію не знайдено у вашому кошику.',
        }

    dish_title = target_item.dish.title
    dish_id_val = target_item.dish_id
    target_item_id = target_item.id
    CartService.remove_item(request, target_item_id)
    if hasattr(request, 'session'):
        request.session.save()

    cart = CartService.get_cart(request)
    total_amount = float(cart.total_amount) if cart else 0.0
    items_count = CartService.get_items_count(request)

    return {
        'success': True,
        'action': 'remove_from_cart',
        'cart_item_id': target_item_id,
        'dish_id': dish_id_val,
        'dish_title': dish_title,
        'total_amount': total_amount,
        'cart_items_count': items_count,
        'message': f'Позицію «{dish_title}» успішно видалено з кошика.',
    }


def _handle_view_cart(request: HttpRequest | None) -> dict[str, Any]:
    if request is None:
        return {'error': 'Неможливо отримати кошик: сесія не знайдена.'}

    cart = CartService.get_cart(request)
    if not cart or cart.items.count() == 0:
        return {
            'items': [],
            'total_amount': 0.0,
            'cart_items_count': 0,
            'message': 'Ваш кошик наразі порожній.',
        }

    items_list = [
        {
            'item_id': item.id,
            'cart_item_id': item.id,
            'dish_id': item.dish_id,
            'title': item.dish.title if item.dish else 'Страва',
            'quantity': item.quantity,
            'unit_price': float(item.unit_price),
            'total_price': float(item.total_price),
            'selected_options': item.selected_options or [],
            'is_available': item.is_available,
        }
        for item in cart.items.select_related('dish').all()
    ]
    return {
        'items': items_list,
        'total_amount': float(cart.total_amount),
        'cart_items_count': CartService.get_items_count(request),
    }


def execute_agent_tool(
    tool_name: str,
    arguments: dict[str, Any],
    request: HttpRequest | None = None,
    session: ChatSession | None = None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {'error': 'Некоректні аргументи інструменту.'}

    if tool_name == 'check_order_status':
        return _handle_check_order_status(arguments, request)
    if tool_name == 'search_dishes':
        return _handle_search_dishes(arguments)
    if tool_name == 'get_faq_answer':
        return _handle_get_faq_answer(arguments)
    if tool_name == 'escalate_to_operator':
        return _handle_escalate_to_operator(arguments, request, session)
    if tool_name == 'add_to_cart':
        return _handle_add_to_cart(arguments, request)
    if tool_name == 'update_cart_quantity':
        return _handle_update_cart_quantity(arguments, request)
    if tool_name == 'remove_from_cart':
        return _handle_remove_from_cart(arguments, request)
    if tool_name == 'view_cart':
        return _handle_view_cart(request)
    return {'error': f'Невідомий інструмент: {tool_name}'}
