from typing import Any

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.http import HttpRequest

from .services import CartService


@receiver(user_logged_in)
def merge_cart_on_login(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    if request:
        CartService.merge_guest_cart(request, user)
