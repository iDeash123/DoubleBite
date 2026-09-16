from typing import Any

from django.http import HttpRequest
from menu.models import Dish
from menu.selectors import search_dishes_for_agent
from orders.exceptions import DishUnavailableError
from orders.services import CartService

from support.models import ChatSession, TicketReason
from support.services import (
    ChatSessionService,
    check_order_status_for_request,
    get_faq_answers,
)


def _find_dish(dish_id: int | None = None, dish_name: str | None = None) -> Dish | None:
    if dish_id:
        d = Dish.objects.filter(id=int(dish_id)).first()
        if d:
            return d
    if dish_name and isinstance(dish_name, str):
        name = dish_name.strip()
        if not name:
            return None
        # 1. Exact match
        d = Dish.objects.filter(title__iexact=name).first()
        if d:
            return d
        # 2. Contains match
        d = Dish.objects.filter(title__icontains=name).first()
        if d:
            return d
        # 3. Keyword match (skip generic category words)
        stop_words = {'піца', 'рол', 'бургер', 'салат', 'боул', 'сет', 'десерт', 'та', 'і', 'з', 'в', 'для', 'на'}
        keywords = [w for w in name.split() if len(w) > 2 and w.lower() not in stop_words]
        for kw in keywords:
            d = Dish.objects.filter(title__icontains=kw).first()
            if d:
                return d
    return None


SUPPORT_AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "check_order_status",
            "description": "Отримати актуальний статус доставки замовлення, орієнтовний час прибуття (ETA) та адресу за номером замовлення.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "Числовий номер замовлення (наприклад: 1042)"
                    }
                },
                "required": ["order_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_dishes",
            "description": "Пошук та перегляд страв і напоїв у меню за назвою, категорією (наприклад 'drinks' для напоїв, 'pizza', 'sushi', 'burgers', 'desserts', 'bowls-salads', 'sets'), максимальною ціною або дієтичними параметрами. Обов'язково викликай цей інструмент, коли клієнт просить порадити або порекомендувати страви чи напої!",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Ключове слово для пошуку (наприклад: 'піца', 'суші', 'бургер', 'салат', 'лимонад')"
                    },
                    "category_slug": {
                        "type": "string",
                        "description": "Слаг категорії: 'drinks' (для напоїв, фрешів, чаю, кави), 'pizza', 'sushi', 'burgers', 'desserts', 'bowls-salads', 'sets'"
                    },
                    "max_price": {
                        "type": "number",
                        "description": "Максимальна ціна страви у гривнях"
                    },
                    "is_vegetarian": {
                        "type": "boolean",
                        "description": "Фільтр лише вегетаріанських страв"
                    },
                    "max_calories": {
                        "type": "integer",
                        "description": "Максимальна калорійність"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_faq_answer",
            "description": "Пошук у базі знань Double Bite: умови доставки, мінімальна сума замовлення, години роботи, способи оплати, повернення.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Запитання або тема (наприклад: 'вартість доставки', 'графік роботи')"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": "Автономно додати страву та обраний модифікатор до кошика клієнта. Обов'язково викликай цей інструмент, коли клієнт просить додати будь-яку страву! Можна вказувати dish_id АБО назву страви dish_name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dish_id": {
                        "type": "integer",
                        "description": "Числовий ID страви (якщо відомий)"
                    },
                    "dish_name": {
                        "type": "string",
                        "description": "Назва або частина назви страви для додавання (наприклад: 'Піца Буррата', 'Пепероні')"
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Кількість порцій (за замовчуванням 1)"
                    },
                    "option_id": {
                        "type": "integer",
                        "description": "ID опції чи розміру страви (необов'язково)"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_cart_quantity",
            "description": "Змінити кількість порцій існуючої страви у кошику клієнта (наприклад, зменшити до 2 або збільшити). Обов'язково викликай цей інструмент, коли клієнт просить змінити/зменшити кількість! Якщо quantity=0, страва видаляється.",
            "parameters": {
                "type": "object",
                "properties": {
                    "quantity": {
                        "type": "integer",
                        "description": "Нова підсумкова кількість порцій (якщо 0 — страва видаляється)"
                    },
                    "dish_id": {
                        "type": "integer",
                        "description": "ID страви у кошику"
                    },
                    "dish_name": {
                        "type": "string",
                        "description": "Назва страви у кошику (наприклад: 'Піца Веганська з Артишоками')"
                    },
                    "item_id": {
                        "type": "integer",
                        "description": "ID позиції у кошику (якщо відомий)"
                    }
                },
                "required": ["quantity"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_from_cart",
            "description": "Видалити позицію або страву з кошика клієнта за назвою dish_name, dish_id або item_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dish_name": {
                        "type": "string",
                        "description": "Назва страви для видалення з кошика (наприклад: 'Піца Веганська')"
                    },
                    "dish_id": {
                        "type": "integer",
                        "description": "ID страви для видалення, якщо відомий"
                    },
                    "item_id": {
                        "type": "integer",
                        "description": "ID позиції CartItem у кошику"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "view_cart",
            "description": "Отримати поточний склад кошика клієнта (список страв, порції, проміжна та підсумкова сума).",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_operator",
            "description": "Викликати чергового адміністратора/оператора ресторану при скарзі, конфлікті, нездатності вирішити проблему чи прямому проханні клієнта.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Причина ескалації: 'COMPLAINT', 'ORDER_ISSUE', 'HUMAN_REQUESTED', 'BOT_CONFUSED'"
                    },
                    "details": {
                        "type": "string",
                        "description": "Стислий опис проблеми та суті звернення для персоналу"
                    },
                    "order_id": {
                        "type": "integer",
                        "description": "Номер замовлення, якщо відомий або згаданий клієнтом"
                    },
                    "customer_phone": {
                        "type": "string",
                        "description": "Контактний телефон клієнта (якщо відомий або наданий)"
                    }
                },
                "required": ["reason", "details"]
            }
        }
    }
]


def execute_agent_tool(
    tool_name: str,
    arguments: dict[str, Any],
    request: HttpRequest | None = None,
    session: ChatSession | None = None,
) -> dict[str, Any]:
    if tool_name == "check_order_status":
        order_id = arguments.get("order_id")
        if not order_id:
            return {"error": "Будь ласка, вкажіть номер замовлення."}
        if request is None:
            return {"error": "Запит не надано для перевірки прав доступу."}
        return check_order_status_for_request(request, order_id)

    elif tool_name == "search_dishes":
        query = arguments.get("query", "")
        category_slug = arguments.get("category_slug")
        max_price = arguments.get("max_price")
        is_vegetarian = arguments.get("is_vegetarian")
        max_calories = arguments.get("max_calories")

        dishes = search_dishes_for_agent(
            query=query or "",
            category_slug=category_slug,
            max_price=max_price,
            is_vegetarian=is_vegetarian,
            max_calories=max_calories,
        )
        return {
            "count": len(dishes),
            "dishes": dishes,
        }

    elif tool_name == "get_faq_answer":
        query = arguments.get("query", "")
        answers = get_faq_answers(query)
        if not answers:
            return {
                "faqs": [],
                "message": "За вашим запитом інформації в базі знань не знайдено."
            }
        return {
            "faqs": answers,
            "count": len(answers),
        }

    elif tool_name == "escalate_to_operator":
        reason = arguments.get("reason", TicketReason.HUMAN_REQUESTED)
        details = arguments.get("details", "Клієнт звернувся за допомогою оператора.")
        order_id = arguments.get("order_id")
        customer_phone = arguments.get("customer_phone", "")

        order = None
        if order_id and request:
            order_data = check_order_status_for_request(request, order_id)
            if "error" not in order_data:
                from orders.models import Order
                order = Order.objects.filter(id=order_data.get("order_id")).first()

        if session is None and request:
            session = ChatSessionService.get_or_create_active_session(request)

        if session:
            ticket = ChatSessionService.escalate_session(
                session=session,
                reason=reason,
                details=details,
                order=order,
                customer_phone=customer_phone,
            )
            ticket_id = ticket.id
            ticket_status = ticket.status
        else:
            ticket_id = None
            ticket_status = "PENDING"

        return {
            "ticket_id": ticket_id,
            "status": ticket_status,
            "assigned_role": "RESTAURANT_ADMIN",
            "support_phone": "+380 44 123 45 67",
            "message": "Звернення зареєстровано та передано черговому адміністратору ресторану."
        }

    elif tool_name == "add_to_cart":
        if request is None:
            return {"error": "Неможливо оновити кошик: сесія не знайдена."}

        dish_id = arguments.get("dish_id")
        dish_name = arguments.get("dish_name")
        quantity = int(arguments.get("quantity") or 1)
        option_id = arguments.get("option_id")

        dish = _find_dish(dish_id=dish_id, dish_name=dish_name)
        if not dish:
            identifier = dish_name or dish_id or "невідома страва"
            return {"error": f"Страву «{identifier}» не знайдено в меню. Будь ласка, перевірте назву або скористайтеся пошуком."}

        try:
            item = CartService.add_dish(
                request=request,
                dish_id=dish.id,
                quantity=quantity,
                option_id=option_id,
            )
            if hasattr(request, "session"):
                request.session.save()

            cart = CartService.get_cart(request)
            total_amount = float(cart.total_amount) if cart else 0.0
            items_count = CartService.get_items_count(request)

            return {
                "success": True,
                "action": "add_to_cart",
                "dish_id": dish.id,
                "dish_title": item.dish.title,
                "quantity": item.quantity,
                "added_quantity": quantity,
                "unit_price": float(item.unit_price),
                "total_amount": total_amount,
                "cart_items_count": items_count,
                "message": f"Страву «{item.dish.title}» ({quantity} шт.) успішно додано до кошика."
            }
        except DishUnavailableError as e:
            return {"error": str(e)}
        except (ValueError, KeyError, TypeError) as e:
            return {"error": f"Помилка при додаванні до кошика: {e!s}"}

    elif tool_name == "update_cart_quantity":
        if request is None:
            return {"error": "Неможливо оновити кошик: сесія не знайдена."}

        cart = CartService.get_cart(request)
        if not cart or cart.items.count() == 0:
            return {"error": "Ваш кошик наразі порожній, немає страв для зміни кількості."}

        item_id = arguments.get("item_id")
        dish_id = arguments.get("dish_id")
        dish_name = arguments.get("dish_name")
        try:
            quantity = int(arguments.get("quantity", 0))
        except (ValueError, TypeError):
            quantity = 0

        target_item = None
        if item_id:
            target_item = cart.items.filter(id=int(item_id)).first()
        if not target_item and dish_id:
            target_item = cart.items.filter(dish_id=int(dish_id)).first()
        if not target_item and dish_name:
            name = str(dish_name).strip()
            target_item = cart.items.filter(dish__title__iexact=name).first()
            if not target_item:
                target_item = cart.items.filter(dish__title__icontains=name).first()
            if not target_item:
                stop_words = {'піца', 'рол', 'бургер', 'салат', 'боул', 'сет', 'десерт', 'та', 'і', 'з', 'в'}
                keywords = [w for w in name.split() if len(w) > 2 and w.lower() not in stop_words]
                for kw in keywords:
                    target_item = cart.items.filter(dish__title__icontains=kw).first()
                    if target_item:
                        break

        if not target_item:
            identifier = dish_name or dish_id or item_id or "вказану страву"
            return {"error": f"Позицію «{identifier}» не знайдено у вашому кошику."}

        dish_title = target_item.dish.title
        dish_id_val = target_item.dish_id
        if quantity <= 0:
            CartService.remove_item(request, target_item.id)
            action_desc = f"Позицію «{dish_title}» видалено з кошика."
        else:
            CartService.update_item_quantity(request, target_item.id, quantity)
            action_desc = f"Кількість страви «{dish_title}» успішно оновлено до {quantity} шт."

        if hasattr(request, "session"):
            request.session.save()

        cart = CartService.get_cart(request)
        total_amount = float(cart.total_amount) if cart else 0.0
        items_count = CartService.get_items_count(request)

        return {
            "success": True,
            "action": "update_cart_quantity",
            "dish_id": dish_id_val,
            "dish_title": dish_title,
            "quantity": quantity,
            "total_amount": total_amount,
            "cart_items_count": items_count,
            "message": action_desc,
        }

    elif tool_name == "remove_from_cart":
        if request is None:
            return {"error": "Неможливо оновити кошик: сесія не знайдена."}

        item_id = arguments.get("item_id")
        dish_id = arguments.get("dish_id")
        dish_name = arguments.get("dish_name")

        cart = CartService.get_cart(request)
        if not cart or cart.items.count() == 0:
            return {"success": False, "message": "Кошик порожній."}

        target_item = None
        if item_id:
            target_item = cart.items.filter(id=int(item_id)).first()
        if not target_item and dish_id:
            target_item = cart.items.filter(dish_id=int(dish_id)).first()
        if not target_item and dish_name:
            name = str(dish_name).strip()
            target_item = cart.items.filter(dish__title__iexact=name).first() or cart.items.filter(dish__title__icontains=name).first()
            if not target_item:
                stop_words = {'піца', 'рол', 'бургер', 'салат', 'боул', 'сет', 'десерт', 'та', 'і', 'з', 'в'}
                keywords = [w for w in name.split() if len(w) > 2 and w.lower() not in stop_words]
                for kw in keywords:
                    target_item = cart.items.filter(dish__title__icontains=kw).first()
                    if target_item:
                        break

        if target_item:
            dish_title = target_item.dish.title
            dish_id_val = target_item.dish_id
            CartService.remove_item(request, target_item.id)
            if hasattr(request, "session"):
                request.session.save()

            cart = CartService.get_cart(request)
            total_amount = float(cart.total_amount) if cart else 0.0
            items_count = CartService.get_items_count(request)

            return {
                "success": True,
                "action": "remove_from_cart",
                "dish_id": dish_id_val,
                "dish_title": dish_title,
                "total_amount": total_amount,
                "cart_items_count": items_count,
                "message": f"Позицію «{dish_title}» успішно видалено з кошика."
            }

        identifier = dish_name or dish_id or item_id or "позицію"
        return {
            "success": False,
            "message": f"Позицію «{identifier}» не знайдено у вашому кошику."
        }

    elif tool_name == "view_cart":
        if request is None:
            return {"error": "Неможливо отримати кошик: сесія не знайдена."}

        cart = CartService.get_cart(request)
        if not cart or cart.items.count() == 0:
            return {
                "items": [],
                "total_amount": 0.0,
                "cart_items_count": 0,
                "message": "Ваш кошик наразі порожній."
            }

        items_list = [
            {
                "item_id": item.id,
                "dish_id": item.dish_id,
                "title": item.dish.title if item.dish else "Страва",
                "quantity": item.quantity,
                "unit_price": float(item.unit_price),
                "total_price": float(item.total_price),
                "is_available": item.is_available,
            }
            for item in cart.items.select_related('dish').all()
        ]
        return {
            "items": items_list,
            "total_amount": float(cart.total_amount),
            "cart_items_count": CartService.get_items_count(request),
        }

    return {"error": f"Невідомий інструмент: {tool_name}"}
