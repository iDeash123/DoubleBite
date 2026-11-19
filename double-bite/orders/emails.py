from decimal import Decimal, InvalidOperation
from typing import Any

from config.emails import resolve_site_domain, resolve_site_protocol, send_templated_email
from django.urls import reverse

from .models import PaymentStatus


def _extract_field(obj: Any, key: str) -> Any:
    """Read an attribute or dictionary key from a Stripe object or plain dict."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _clean_email(value: Any) -> str:
    """Return a trimmed email string if non-empty, otherwise empty string."""
    if not value:
        return ''
    return value.strip() if isinstance(value, str) else str(value)


def _resolve_order_email(order: Any, *candidates: Any) -> str:
    """Resolve recipient email from explicit candidates, then order.customer_email, then order.user.email."""
    for candidate in candidates:
        cleaned = _clean_email(candidate)
        if cleaned:
            return cleaned

    customer_email = _clean_email(getattr(order, 'customer_email', None))
    if customer_email:
        return customer_email

    user = getattr(order, 'user', None)
    if user:
        user_email = _clean_email(getattr(user, 'email', None))
        if user_email:
            return user_email

    return ''


def get_tracking_url(order: Any, domain: str | None = None, protocol: str | None = None) -> str:
    path = reverse('orders:tracking', kwargs={'order_number': order.order_number})
    token = getattr(order, 'access_token', None)
    if token:
        path = f'{path}?t={token}'
    effective_domain = resolve_site_domain(domain)
    effective_protocol = resolve_site_protocol(protocol)
    return f'{effective_protocol}://{effective_domain}{path}'


def _build_order_email_context(
    order: Any,
    domain: str | None = None,
    protocol: str | None = None,
) -> dict[str, Any]:
    effective_domain = resolve_site_domain(domain)
    effective_protocol = resolve_site_protocol(protocol)
    tracking_url = get_tracking_url(
        order,
        domain=effective_domain,
        protocol=effective_protocol,
    )
    return {
        'order': order,
        'tracking_url': tracking_url,
        'protocol': effective_protocol,
        'domain': effective_domain,
    }


def send_order_confirmation_email(
    order: Any,
    recipient_email: str | None = None,
    domain: str | None = None,
    protocol: str | None = None,
) -> int:
    to_email = _resolve_order_email(order, recipient_email)
    if not to_email:
        return 0

    context = _build_order_email_context(order, domain=domain, protocol=protocol)
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
    protocol: str | None = None,
) -> int:
    to_email = _resolve_order_email(order, recipient_email)
    if not to_email:
        return 0

    context = _build_order_email_context(order, domain=domain, protocol=protocol)
    status_display = order.get_status_display()
    return send_templated_email(
        subject=f'Double Bite: Статус замовлення #{order.order_number} оновлено — {status_display}',
        template_prefix='emails/status_update',
        context=context,
        recipient_list=[to_email],
    )


def send_stripe_receipt_email(
    order: Any,
    session: Any = None,
    recipient_email: str | None = None,
    domain: str | None = None,
    protocol: str | None = None,
) -> int:
    cust_details = _extract_field(session, 'customer_details')
    to_email = _resolve_order_email(
        order,
        recipient_email,
        _extract_field(cust_details, 'email'),
        _extract_field(session, 'customer_email'),
    )
    if not to_email:
        return 0

    context = _build_order_email_context(order, domain=domain, protocol=protocol)
    return send_templated_email(
        subject=f'Double Bite: Квитанція про успішну оплату замовлення #{order.order_number}',
        template_prefix='emails/stripe_receipt',
        context=context,
        recipient_list=[to_email],
    )


def _parse_refund_amount(order: Any, charge: Any) -> Any:
    refund_amount = None
    if charge:
        amt = _extract_field(charge, 'amount_refunded')
        if amt is not None and not isinstance(amt, bool):
            try:
                raw_dec = Decimal(str(amt))
                if raw_dec.is_finite():
                    refund_amount = (raw_dec / Decimal(100)).quantize(Decimal('0.01'))
            except (InvalidOperation, ValueError, TypeError):
                refund_amount = None
    if refund_amount is None and hasattr(order, 'total_amount'):
        refund_amount = order.total_amount
    return refund_amount


def send_refund_notice_email(
    order: Any,
    charge: Any = None,
    recipient_email: str | None = None,
    domain: str | None = None,
    protocol: str | None = None,
) -> int:
    billing_details = _extract_field(charge, 'billing_details')
    to_email = _resolve_order_email(
        order,
        recipient_email,
        _extract_field(billing_details, 'email'),
        _extract_field(charge, 'receipt_email'),
    )
    if not to_email:
        return 0

    context = _build_order_email_context(order, domain=domain, protocol=protocol)
    context['refund_amount'] = _parse_refund_amount(order, charge)
    context['is_partial_refund'] = (
        getattr(order, 'payment_status', '') == PaymentStatus.PARTIALLY_REFUNDED
    )
    return send_templated_email(
        subject=f'Double Bite: Повернення коштів за замовлення #{order.order_number}',
        template_prefix='emails/refund_notice',
        context=context,
        recipient_list=[to_email],
    )
