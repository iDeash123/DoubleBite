from typing import Any


def get_user_profile_context(
    user: Any,
    user_form: Any,
    address_form: Any,
    editing_address: Any = None,
) -> dict[str, Any]:
    """Assemble context data for the user profile and address management views."""
    from allauth.socialaccount.models import SocialAccount
    from orders.models import Order

    from .models import DeliveryAddress

    addresses = list(DeliveryAddress.objects.filter(user=user))
    orders = list(
        Order.objects.filter(user=user)
        .prefetch_related('items')
        .order_by('-created_at')[:5]
    )
    social_accounts = list(SocialAccount.objects.filter(user=user))
    return {
        'user_form': user_form,
        'address_form': address_form,
        'addresses': addresses,
        'orders': orders,
        'social_accounts': social_accounts,
        'editing_address': editing_address,
    }
