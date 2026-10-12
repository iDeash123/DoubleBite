from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import SynchronousOnlyOperation
from django.db import DatabaseError
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject, empty

from .services import CartService


class LazyValue(SimpleLazyObject):
    def __format__(self, format_spec: str) -> str:
        if self._wrapped is empty:
            self._setup()
        return self._wrapped.__format__(format_spec)


def cart_context(request: HttpRequest) -> dict[str, Any]:
    def _get_cart():
        if hasattr(request, '_cached_cart_obj'):
            return request._cached_cart_obj
        try:
            cart = CartService.get_cart(request)
        except (SynchronousOnlyOperation, DatabaseError, AttributeError, KeyError):
            cart = None
        request._cached_cart_obj = cart
        return cart

    def _get_summary():
        if hasattr(request, '_cached_cart_summary_obj'):
            return request._cached_cart_summary_obj
        cart = _get_cart()
        try:
            summary = CartService.summary(cart)
        except (SynchronousOnlyOperation, DatabaseError, AttributeError, KeyError):
            summary = CartService.summary(None)
        request._cached_cart_summary_obj = summary
        return summary

    return {
        'cart': LazyValue(_get_cart),
        'items': LazyValue(lambda: _get_summary().items),
        'total_amount': LazyValue(lambda: _get_summary().total_amount),
        'total_quantity': LazyValue(lambda: _get_summary().total_quantity),
        'min_order_amount': getattr(settings, 'MIN_ORDER_AMOUNT', Decimal(200)),
        'meets_min_amount': LazyValue(lambda: _get_summary().meets_min_amount),
        'cart_items_count': LazyValue(lambda: _get_summary().total_quantity),
    }
