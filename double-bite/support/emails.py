from typing import Any

from config.emails import resolve_site_domain, resolve_site_protocol, send_templated_email
from django.conf import settings
from django.urls import NoReverseMatch, reverse

from support.models import MessageRole


def get_admin_ticket_url(
    ticket: Any,
    domain: str | None = None,
    protocol: str | None = None,
) -> str:
    try:
        path = reverse('admin:support_supportticket_change', args=[ticket.id])
    except NoReverseMatch:
        admin_prefix = getattr(settings, 'DJANGO_ADMIN_URL', 'admin/').strip('/')
        path = f'/{admin_prefix}/support/supportticket/{ticket.id}/change/'
    effective_domain = resolve_site_domain(domain)
    effective_protocol = resolve_site_protocol(protocol)
    return f'{effective_protocol}://{effective_domain}{path}'


def get_desk_ticket_url(
    ticket: Any,
    domain: str | None = None,
    protocol: str | None = None,
) -> str:
    session = getattr(ticket, 'session', None)
    session_uuid = getattr(session, 'session_uuid', None) or getattr(
        ticket, 'session_id', None
    )
    if session_uuid:
        try:
            path = reverse('support:desk_session', args=[session_uuid])
        except NoReverseMatch:
            path = f'/support/desk/{session_uuid}/'
    else:
        try:
            path = reverse('support:desk_index')
        except NoReverseMatch:
            path = '/support/desk/'
    effective_domain = resolve_site_domain(domain)
    effective_protocol = resolve_site_protocol(protocol)
    return f'{effective_protocol}://{effective_domain}{path}'


def send_ticket_escalated_email(
    ticket: Any,
    recipient_email: str | None = None,
    domain: str | None = None,
    protocol: str | None = None,
) -> int:
    admin_email = recipient_email or getattr(settings, 'ADMIN_EMAIL', 'admin@doublebite.ua')
    if not admin_email:
        return 0

    customer_name = 'Гість'
    customer_email = 'Не вказано'
    if getattr(ticket, 'session', None) and getattr(ticket.session, 'user', None):
        user = ticket.session.user
        customer_name = (
            getattr(user, 'first_name', '')
            or getattr(user, 'username', '')
            or getattr(user, 'email', '')
            or 'Гість'
        )
        customer_email = getattr(user, 'email', None) or 'Не вказано'

    effective_domain = resolve_site_domain(domain)
    effective_protocol = resolve_site_protocol(protocol)
    admin_ticket_url = get_admin_ticket_url(
        ticket,
        domain=effective_domain,
        protocol=effective_protocol,
    )
    desk_session_url = get_desk_ticket_url(
        ticket,
        domain=effective_domain,
        protocol=effective_protocol,
    )
    reason_display = (
        ticket.get_reason_display()
        if hasattr(ticket, 'get_reason_display')
        else ticket.reason
    )

    recent_messages = []
    if getattr(ticket, 'session', None) and hasattr(ticket.session, 'messages'):
        recent_messages = list(
            ticket.session.messages.filter(
                role__in=[
                    MessageRole.USER,
                    MessageRole.ASSISTANT,
                    MessageRole.OPERATOR,
                ]
            ).order_by('-created_at', '-id')[:6]
        )
        recent_messages.reverse()

    context = {
        'ticket': ticket,
        'customer_name': customer_name,
        'customer_email': customer_email,
        'admin_ticket_url': admin_ticket_url,
        'desk_session_url': desk_session_url,
        'recent_messages': recent_messages,
        'protocol': effective_protocol,
        'domain': effective_domain,
    }
    return send_templated_email(
        subject=(
            f'Double Bite: [ТЕРМІНОВО] Ескалація тікету #{ticket.id} — {reason_display}'
        ),
        template_prefix='emails/ticket_escalated',
        context=context,
        recipient_list=[admin_email],
    )
