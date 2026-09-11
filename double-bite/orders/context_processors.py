import os
from decimal import Decimal
from typing import Any

from django.http import HttpRequest

from .services import CartService


def cart_context(request: HttpRequest) -> dict[str, Any]:
    cart = CartService.get_cart(request)
    items = list(cart.items.select_related('dish', 'dish__category').all()) if cart else []
    total_amount = sum((item.total_price for item in items if item.is_available), Decimal('0.00'))
    total_quantity = sum((item.quantity or 0 for item in items if item.is_available), 0)
    min_order_amount = Decimal(os.getenv('MIN_ORDER_AMOUNT', '200'))
    meets_min_amount = (total_amount >= min_order_amount)

    return {
        'cart': cart,
        'items': items,
        'total_amount': total_amount,
        'total_quantity': total_quantity,
        'min_order_amount': min_order_amount,
        'meets_min_amount': meets_min_amount,
        'cart_items_count': total_quantity,
    }
