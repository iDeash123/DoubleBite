from typing import Any

from config.constants import (
    DEFAULT_DELIVERY_FEE,
    DEFAULT_FREE_DELIVERY_THRESHOLD,
    DEFAULT_MIN_ORDER_AMOUNT,
    DEFAULT_RESTAURANT_ADDRESS,
    DEFAULT_RESTAURANT_HOTLINE,
)
from django.conf import settings
from django.core.exceptions import SynchronousOnlyOperation
from django.db import DatabaseError
from django.http import HttpRequest
from django.utils.functional import LazyObject, SimpleLazyObject, empty

from .services import CartService


class LazyValue(SimpleLazyObject):
    def _eval(self) -> Any:
        if self._wrapped is empty:
            self._setup()
        return self._wrapped

    def _unwrap_other(self, other: Any) -> Any:
        if isinstance(other, LazyObject):
            if other._wrapped is empty:
                other._setup()
            return other._wrapped
        return other

    def __format__(self, format_spec: str) -> str:
        return self._eval().__format__(format_spec)

    def __int__(self) -> int:
        return int(self._eval())

    def __float__(self) -> float:
        return float(self._eval())

    def __round__(self, ndigits: int | None = None) -> Any:
        wrapped = self._eval()
        return round(wrapped, ndigits) if ndigits is not None else round(wrapped)

    def __lt__(self, other: Any) -> bool:
        return self._eval() < self._unwrap_other(other)

    def __le__(self, other: Any) -> bool:
        return self._eval() <= self._unwrap_other(other)

    def __gt__(self, other: Any) -> bool:
        return self._eval() > self._unwrap_other(other)

    def __ge__(self, other: Any) -> bool:
        return self._eval() >= self._unwrap_other(other)


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
        'min_order_amount': getattr(settings, 'MIN_ORDER_AMOUNT', DEFAULT_MIN_ORDER_AMOUNT),
        'free_delivery_threshold': getattr(
            settings, 'FREE_DELIVERY_THRESHOLD', DEFAULT_FREE_DELIVERY_THRESHOLD
        ),
        'delivery_fee': getattr(settings, 'DELIVERY_FEE', DEFAULT_DELIVERY_FEE),
        'restaurant_address': getattr(settings, 'RESTAURANT_ADDRESS', '') or DEFAULT_RESTAURANT_ADDRESS,
        'restaurant_hotline': getattr(settings, 'RESTAURANT_HOTLINE', '') or DEFAULT_RESTAURANT_HOTLINE,
        'meets_min_amount': LazyValue(lambda: _get_summary().meets_min_amount),
        'cart_items_count': LazyValue(lambda: _get_summary().total_quantity),
    }
