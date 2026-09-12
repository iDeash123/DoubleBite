from typing import Any

from django.http import HttpRequest
from menu.selectors import search_dishes_for_agent
from orders.exceptions import DishUnavailableError
from orders.services import CartService

from support.models import ChatSession, TicketReason
from support.services import (
    ChatSessionService,
    check_order_status_for_request,
    get_faq_answers,
)

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
            "description": "Пошук страв у меню за назвою, категорією, максимальною ціною, калорійністю або дієтичними параметрами.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Ключове слово для пошуку (наприклад: 'піца', 'суші', 'бургер', 'салат')"
                    },
                    "category_slug": {
                        "type": "string",
                        "description": "Слаг категорії (наприклад: 'pizza', 'sushi', 'burgers', 'desserts', 'drinks')"
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
            "description": "Автономно додати страву та обраний модифікатор до кошика клієнта.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dish_id": {
                        "type": "integer",
                        "description": "Числовий ID страви"
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Кількість порцій (за замовчуванням 1)"
                    },
                    "option_id": {
                        "type": "integer",
                        "description": "ID опції чи розміру страви (необов'язково)"
                    }
                },
                "required": ["dish_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_from_cart",
            "description": "Видалити позицію або страву з кошика клієнта.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "integer",
                        "description": "ID позиції CartItem у кошику"
                    },
                    "dish_id": {
                        "type": "integer",
                        "description": "ID страви для видалення, якщо item_id невідомий"
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
        quantity = int(arguments.get("quantity") or 1)
        option_id = arguments.get("option_id")

        if not dish_id:
            return {"error": "Не вказано ID страви для додавання."}

        try:
            item = CartService.add_dish(
                request=request,
                dish_id=dish_id,
                quantity=quantity,
                option_id=option_id,
            )
            cart = CartService.get_cart(request)
            total_amount = float(cart.total_amount) if cart else 0.0
            items_count = CartService.get_items_count(request)

            return {
                "success": True,
                "action": "add_to_cart",
                "dish_id": dish_id,
                "dish_title": item.dish.title,
                "quantity": item.quantity,
                "unit_price": float(item.unit_price),
                "total_amount": total_amount,
                "cart_items_count": items_count,
                "message": f"Страву «{item.dish.title}» ({item.quantity} шт.) успішно додано до кошика."
            }
        except DishUnavailableError as e:
            return {"error": str(e)}
        except (ValueError, KeyError, TypeError) as e:
            return {"error": f"Помилка при додаванні до кошика: {e!s}"}

    elif tool_name == "remove_from_cart":
        if request is None:
            return {"error": "Неможливо оновити кошик: сесія не знайдена."}

        item_id = arguments.get("item_id")
        dish_id = arguments.get("dish_id")

        cart = CartService.get_cart(request)
        if not cart:
            return {"success": False, "message": "Кошик порожній."}

        removed = False
        if item_id:
            removed = CartService.remove_item(request, int(item_id))
        elif dish_id:
            item = cart.items.filter(dish_id=int(dish_id)).first()
            if item:
                removed = CartService.remove_item(request, item.id)

        cart = CartService.get_cart(request)
        total_amount = float(cart.total_amount) if cart else 0.0
        items_count = CartService.get_items_count(request)

        if removed:
            return {
                "success": True,
                "action": "remove_from_cart",
                "total_amount": total_amount,
                "cart_items_count": items_count,
                "message": "Позицію успішно видалено з кошика."
            }
        return {
            "success": False,
            "message": "Позицію не знайдено у вашому кошику."
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
