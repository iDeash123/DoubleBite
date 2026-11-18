from typing import Any

from allauth.account.signals import user_signed_up
from config.emails import (
    resolve_site_domain,
    resolve_site_protocol,
    send_templated_email,
)
from config.transactions import schedule_on_commit
from django.core.exceptions import DisallowedHost
from django.dispatch import receiver
from django.http import HttpRequest


@receiver(user_signed_up, dispatch_uid='accounts.send_welcome_email_on_signup')
def send_welcome_email_on_signup(
    sender: Any,
    request: HttpRequest | None,
    user: Any,
    **kwargs: Any,
) -> None:
    """Schedule welcome email delivery after transaction commit for new signups."""
    if not user or not getattr(user, 'email', None):
        return

    if request is not None:
        protocol = 'https' if request.is_secure() else 'http'
        try:
            domain = request.get_host()
        except DisallowedHost:
            domain = resolve_site_domain()
    else:
        protocol = resolve_site_protocol()
        domain = resolve_site_domain()

    context = {
        'user': user,
        'customer_name': user.get_full_name() or user.email,
        'protocol': protocol,
        'domain': domain,
    }
    schedule_on_commit(
        send_templated_email,
        subject='Ласкаво просимо до Double Bite!',
        template_prefix='emails/welcome',
        context=context,
        recipient_list=[user.email],
    )
