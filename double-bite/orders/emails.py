import logging
from typing import Any

from config.emails import send_templated_email
from django.conf import settings
from django.urls import reverse

logger = logging.getLogger('orders')


def get_tracking_url(order: Any, domain: str | None = None, protocol: str = 'https') -> str:
    path = reverse('orders:tracking', kwargs={'order_number': order.order_number})
    effective_domain = domain or getattr(settings, 'SITE_DOMAIN', None)
    if not effective_domain and getattr(settings, 'ALLOWED_HOSTS', None):
        allowed = [h for h in settings.ALLOWED_HOSTS if h and '*' not in h and h != '0.0.0.0']
        if allowed:
            effective_domain = allowed[0]
    if not effective_domain:
        effective_domain = 'localhost:8000'
    return f"{protocol}://{effective_domain}{path}"


def send_order_confirmation_email(
    order: Any,
    recipient_email: str | None = None,
    domain: str | None = None,
    protocol: str = 'https',
) -> int:
    to_email = recipient_email
    if not to_email and getattr(order, 'user', None) and order.user.email:
        to_email = order.user.email
    if not to_email:
        to_email = getattr(order, 'customer_email', None)

    if not to_email:
        return 0

    tracking_url = get_tracking_url(order, domain=domain, protocol=protocol)
    context = {
        'order': order,
        'tracking_url': tracking_url,
        'protocol': protocol,
        'domain': domain,
    }
    return send_templated_email(
        subject=f'Double Bite: Замовлення #{order.order_number} підтверджено!',
        template_prefix='emails/order_confirmation',
        context=context,
        recipient_list=[to_email],
    )


def send_order_status_update_email(
    order: Any,
    recipient_email: str | None = None,
    domain: str | None = None,
    protocol: str = 'https',
) -> int:
    to_email = recipient_email
    if not to_email and getattr(order, 'user', None) and order.user.email:
        to_email = order.user.email
    if not to_email:
        to_email = getattr(order, 'customer_email', None)

    if not to_email:
        return 0

    tracking_url = get_tracking_url(order, domain=domain, protocol=protocol)
    context = {
        'order': order,
        'tracking_url': tracking_url,
        'protocol': protocol,
        'domain': domain,
    }
    return send_templated_email(
        subject=f'Double Bite: Статус замовлення #{order.order_number} оновлено — {order.get_status_display()}',
        template_prefix='emails/status_update',
        context=context,
        recipient_list=[to_email],
    )
