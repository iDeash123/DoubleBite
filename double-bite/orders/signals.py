from typing import Any

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.http import HttpRequest

from .services import CartService

try:
    from allauth.account.signals import user_logged_in as allauth_user_logged_in
except ImportError:
    allauth_user_logged_in = None


@receiver(user_logged_in)
def merge_cart_on_login(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    if request:
        CartService.merge_guest_cart_to_user(request, user)


if allauth_user_logged_in:
    @receiver(allauth_user_logged_in)
    def merge_cart_on_allauth_login(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
        if request:
            CartService.merge_guest_cart_to_user(request, user)

