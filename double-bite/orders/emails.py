from decimal import Decimal
from typing import Any

from config.emails import send_templated_email
from django.conf import settings
from django.urls import reverse


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


def send_stripe_receipt_email(
    order: Any,
    session: Any = None,
    recipient_email: str | None = None,
    domain: str | None = None,
    protocol: str = 'https',
) -> int:
    to_email = recipient_email
    if not to_email and session:
        cust_details = getattr(session, 'customer_details', None) or (session.get('customer_details') if isinstance(session, dict) else None)
        if cust_details:
            to_email = getattr(cust_details, 'email', None) or (cust_details.get('email') if isinstance(cust_details, dict) else None)
        if not to_email:
            to_email = getattr(session, 'customer_email', None) or (session.get('customer_email') if isinstance(session, dict) else None)
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
        subject=f'Double Bite: Квитанція про успішну оплату замовлення #{order.order_number}',
        template_prefix='emails/stripe_receipt',
        context=context,
        recipient_list=[to_email],
    )


def send_refund_notice_email(
    order: Any,
    charge: Any = None,
    recipient_email: str | None = None,
    domain: str | None = None,
    protocol: str = 'https',
) -> int:
    to_email = recipient_email
    if not to_email and charge:
        billing_details = getattr(charge, 'billing_details', None) or (charge.get('billing_details') if isinstance(charge, dict) else None)
        if billing_details:
            to_email = getattr(billing_details, 'email', None) or (billing_details.get('email') if isinstance(billing_details, dict) else None)
        if not to_email:
            to_email = getattr(charge, 'receipt_email', None) or (charge.get('receipt_email') if isinstance(charge, dict) else None)
    if not to_email and getattr(order, 'user', None) and order.user.email:
        to_email = order.user.email
    if not to_email:
        to_email = getattr(order, 'customer_email', None)

    if not to_email:
        return 0

    refund_amount = None
    if charge:
        amt = getattr(charge, 'amount_refunded', None) or (charge.get('amount_refunded') if isinstance(charge, dict) else None)
        if amt is not None:
            try:
                refund_amount = Decimal(str(amt)) / Decimal(100)
            except Exception:
                refund_amount = None
    if refund_amount is None and hasattr(order, 'total_amount'):
        refund_amount = order.total_amount

    tracking_url = get_tracking_url(order, domain=domain, protocol=protocol)
    context = {
        'order': order,
        'refund_amount': refund_amount,
        'tracking_url': tracking_url,
        'protocol': protocol,
        'domain': domain,
    }
    return send_templated_email(
        subject=f'Double Bite: Повернення коштів за замовлення #{order.order_number}',
        template_prefix='emails/refund_notice',
        context=context,
        recipient_list=[to_email],
    )
