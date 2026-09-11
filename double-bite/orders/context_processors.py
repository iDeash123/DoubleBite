from django.http import HttpRequest

from .services import CartService


def cart_context(request: HttpRequest) -> dict[str, int]:
    return {'cart_items_count': CartService.get_items_count(request)}
