import html
import logging
import re
from collections.abc import Sequence
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from config.constants import DEFAULT_RESTAURANT_ADDRESS, DEFAULT_RESTAURANT_HOTLINE

logger = logging.getLogger(__name__)

_STYLE_SCRIPT_RE = re.compile(r'<(style|script)\b[^>]*>.*?</\1>', re.IGNORECASE | re.DOTALL)
_HTML_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)
_MULTI_BLANK_RE = re.compile(r'\n{3,}')


def _html_to_plain_text(html_content: str) -> str:
    cleaned = _STYLE_SCRIPT_RE.sub('', html_content)
    cleaned = _HTML_COMMENT_RE.sub('', cleaned)
    text = html.unescape(strip_tags(cleaned))
    lines = [line.strip() for line in text.splitlines()]
    return _MULTI_BLANK_RE.sub('\n\n', '\n'.join(lines)).strip()


def send_templated_email(
    subject: str,
    template_prefix: str,
    context: dict[str, Any] | None = None,
    recipient_list: Sequence[str] | str | None = None,
    from_email: str | None = None,
    fail_silently: bool = True,
) -> int:
    if not recipient_list:
        return 0

    if isinstance(recipient_list, str):
        recipients = [recipient_list]
    else:
        recipients = [r for r in recipient_list if r]

    if not recipients:
        return 0

    ctx = dict(context) if context else {}
    ctx.setdefault('restaurant_address', getattr(settings, 'RESTAURANT_ADDRESS', DEFAULT_RESTAURANT_ADDRESS))
    ctx.setdefault('restaurant_hotline', getattr(settings, 'RESTAURANT_HOTLINE', DEFAULT_RESTAURANT_HOTLINE))
    sender = from_email or getattr(settings, 'DEFAULT_FROM_EMAIL', 'Double Bite <noreply@doublebite.ua>')

    prefix = template_prefix
    if prefix.endswith('.html'):
        prefix = prefix[:-5]
    elif prefix.endswith('.txt'):
        prefix = prefix[:-4]

    html_template = f'{prefix}.html'
    txt_template = f'{prefix}.txt'

    html_content = None
    txt_content = None

    try:
        html_content = render_to_string(html_template, ctx)
    except TemplateDoesNotExist:
        pass

    try:
        txt_content = render_to_string(txt_template, ctx)
    except TemplateDoesNotExist:
        pass

    if not html_content and not txt_content:
        logger.warning('Neither HTML nor TXT template found for %s', prefix)
        return 0

    if not txt_content and html_content:
        txt_content = _html_to_plain_text(html_content)

    body = txt_content or ''

    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=sender,
            to=recipients,
        )
        if html_content:
            message.attach_alternative(html_content, 'text/html')
        return message.send(fail_silently=fail_silently)
    except Exception as e:
        logger.error('Failed to send email to %s: %s', recipients, e)
        if not fail_silently:
            raise
        return 0
