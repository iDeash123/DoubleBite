import logging
from typing import Any

from config.emails import send_templated_email
from django.conf import settings
from django.urls import reverse

logger = logging.getLogger('support')


def get_admin_ticket_url(ticket: Any, domain: str | None = None, protocol: str = 'https') -> str:
    try:
        path = reverse('admin:support_supportticket_change', args=[ticket.id])
    except Exception:
        path = f'/admin/support/supportticket/{ticket.id}/change/'
    effective_domain = domain or getattr(settings, 'SITE_DOMAIN', None)
    if not effective_domain and getattr(settings, 'ALLOWED_HOSTS', None):
        allowed = [h for h in settings.ALLOWED_HOSTS if h and '*' not in h and h != '0.0.0.0']
        if allowed:
            effective_domain = allowed[0]
    if not effective_domain:
        effective_domain = 'localhost:8000'
    return f"{protocol}://{effective_domain}{path}"


def send_ticket_escalated_email(
    ticket: Any,
    recipient_email: str | None = None,
    domain: str | None = None,
    protocol: str = 'https',
) -> int:
    admin_email = recipient_email or getattr(settings, 'ADMIN_EMAIL', 'admin@doublebite.ua')
    if not admin_email:
        return 0

    customer_name = 'Гість'
    customer_email = 'Не вказано'
    if getattr(ticket, 'session', None) and getattr(ticket.session, 'user', None):
        user = ticket.session.user
        customer_name = getattr(user, 'first_name', '') or getattr(user, 'username', '') or getattr(user, 'email', '') or 'Гість'
        customer_email = getattr(user, 'email', None) or 'Не вказано'

    admin_ticket_url = get_admin_ticket_url(ticket, domain=domain, protocol=protocol)
    reason_display = ticket.get_reason_display() if hasattr(ticket, 'get_reason_display') else ticket.reason

    recent_messages = []
    if getattr(ticket, 'session', None) and hasattr(ticket.session, 'messages'):
        recent_messages = list(
            ticket.session.messages.filter(role__in=['user', 'assistant'])
            .order_by('-created_at')[:6]
        )
        recent_messages.reverse()

    context = {
        'ticket': ticket,
        'customer_name': customer_name,
        'customer_email': customer_email,
        'admin_ticket_url': admin_ticket_url,
        'recent_messages': recent_messages,
        'protocol': protocol,
        'domain': domain,
    }
    return send_templated_email(
        subject=f'Double Bite: [ТЕРМІНОВО] Ескалація тікету #{ticket.id} — {reason_display}',
        template_prefix='emails/ticket_escalated',
        context=context,
        recipient_list=[admin_email],
    )
