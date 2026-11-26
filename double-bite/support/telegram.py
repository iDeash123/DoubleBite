import html
import ipaddress
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.db import DatabaseError, IntegrityError

from support.emails import get_desk_ticket_url
from support.models import (
    ChatMessage,
    ChatSessionStatus,
    MessageRole,
    MessageSource,
    SupportTicket,
    TicketStatus,
)
from support.selectors import format_session_customer_name
from support.services import (
    get_operator_display_name,
    get_support_working_hours,
    get_working_hours_label,
    is_within_working_hours,
)

logger = logging.getLogger('support')

TELEGRAM_API_BASE_URL = 'https://api.telegram.org'
DEFAULT_TELEGRAM_TIMEOUT = 4.0
TELEGRAM_TOPIC_NAME_LIMIT = 128
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_EXCERPT_LIMIT = 8


def get_telegram_bot_token() -> str:
    return str(getattr(settings, 'TELEGRAM_BOT_TOKEN', '') or '').strip()


def get_telegram_support_chat_id() -> int | None:
    raw = getattr(settings, 'TELEGRAM_SUPPORT_CHAT_ID', '')
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def get_telegram_webhook_secret() -> str:
    return str(getattr(settings, 'TELEGRAM_WEBHOOK_SECRET', '') or '').strip()


def is_telegram_enabled() -> bool:
    return bool(
        getattr(settings, 'TELEGRAM_BOT_ENABLED', False)
        and get_telegram_bot_token()
        and get_telegram_support_chat_id() is not None
    )


def call_telegram_api(
    method: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
) -> Any:
    token = get_telegram_bot_token()
    if not token:
        return None

    url = f'{TELEGRAM_API_BASE_URL}/bot{token}/{method}'
    try:
        response = httpx.post(url, json=payload or {}, timeout=timeout)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        logger.warning(
            'Telegram API %s request failed: %s',
            method,
            exc or type(exc).__name__,
        )
        return None

    try:
        data = response.json()
    except ValueError:
        logger.warning(
            'Telegram API %s returned non-JSON response (status=%s)',
            method,
            response.status_code,
        )
        return None

    if response.status_code >= 400 or not isinstance(data, dict) or not data.get('ok'):
        description = (
            data.get('description')
            if isinstance(data, dict)
            else f'HTTP {response.status_code}'
        )
        logger.warning(
            'Telegram API %s error (status=%s): %s',
            method,
            response.status_code,
            description,
        )
        return None

    return data.get('result')


def format_telegram_topic_name(ticket: SupportTicket) -> str:
    customer_name = ' '.join(format_session_customer_name(ticket.session).split())
    reason_display = ' '.join(str(ticket.get_reason_display() or '').split())
    raw_name = f'#{ticket.id} · {customer_name} · {reason_display}'
    if len(raw_name) <= TELEGRAM_TOPIC_NAME_LIMIT:
        return raw_name
    return raw_name[: TELEGRAM_TOPIC_NAME_LIMIT - 1].rstrip() + '…'


def _truncate_text(text: str, limit: int) -> str:
    cleaned = str(text or '').strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + '…'


def _escape_truncated(raw_text: str, max_escaped_len: int) -> str:
    if max_escaped_len <= 0:
        return ''
    candidate = str(raw_text or '').strip()
    escaped = html.escape(candidate, quote=False)
    if len(escaped) <= max_escaped_len:
        return escaped

    # Shrink raw text via binary search so we never slice through an HTML entity.
    low, high = 0, len(candidate)
    best = ''
    while low <= high:
        mid = (low + high) // 2
        trimmed = candidate[:mid].rstrip()
        if trimmed and mid < len(candidate):
            trimmed += '…'
        probe = html.escape(trimmed, quote=False)
        if len(probe) <= max_escaped_len:
            best = probe
            low = mid + 1
        else:
            high = mid - 1
    return best


def _format_telegram_html_message(
    prefix_html: str,
    raw_content: str,
    *,
    max_len: int = TELEGRAM_MESSAGE_LIMIT,
) -> str:
    available = max(1, max_len - len(prefix_html))
    return f'{prefix_html}{_escape_truncated(raw_content, available)}'


def _is_telegram_button_url_allowed(url: str) -> bool:
    if not url:
        return False
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').strip().lower().rstrip('.')
    if not host or host in ('localhost', 'testserver'):
        return False
    if host.endswith(('.localhost', '.local', '.internal')):
        return False
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        return '.' in host
    return not (
        ip_obj.is_loopback
        or ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_unspecified
        or ip_obj.is_reserved
        or ip_obj.is_multicast
    )


def build_ticket_control_keyboard(
    ticket: SupportTicket,
    *,
    force_closed: bool = False,
) -> dict[str, Any]:
    session = ticket.session
    if (
        force_closed
        or ticket.status == TicketStatus.RESOLVED
        or session.status != ChatSessionStatus.ESCALATED
    ):
        return {'inline_keyboard': []}

    desk_url = get_desk_ticket_url(ticket)
    include_url_button = _is_telegram_button_url_allowed(desk_url)
    is_claimed = (
        ticket.status == TicketStatus.IN_PROGRESS
        or session.assigned_operator_id is not None
    )

    if not is_claimed:
        second_row: list[dict[str, str]] = [
            {'text': '✅ Завершити', 'callback_data': f'resolve:{ticket.id}'},
        ]
        if include_url_button:
            second_row.append({'text': '🖥 Відкрити в пульті', 'url': desk_url})
        rows: list[list[dict[str, str]]] = [
            [
                {'text': '✋ Взяти в роботу', 'callback_data': f'claim:{ticket.id}'},
                {'text': '🤖 Повернути AI', 'callback_data': f'return_ai:{ticket.id}'},
            ],
            second_row,
        ]
    else:
        rows = [
            [
                {'text': '🤖 Повернути AI', 'callback_data': f'return_ai:{ticket.id}'},
                {'text': '✅ Завершити', 'callback_data': f'resolve:{ticket.id}'},
            ],
        ]
        if include_url_button:
            rows.append([{'text': '🖥 Відкрити в пульті', 'url': desk_url}])
    return {'inline_keyboard': rows}


def build_ticket_card_text(
    ticket: SupportTicket,
    *,
    outcome: str = '',
    actor_name: str = '',
) -> str:
    session = ticket.session
    user = session.user
    customer_name = format_session_customer_name(session)
    reason_display = ticket.get_reason_display()

    if outcome == 'returned_to_ai':
        status_line = '🤖 Повернуто AI-консьєржу'
    elif outcome == 'client_reset':
        status_line = '🔄 Клієнт почав новий діалог'
    elif outcome in ('resolved', 'admin_resolved'):
        status_line = '✅ Завершено'
    elif ticket.status == TicketStatus.RESOLVED:
        status_line = (
            '🤖 Повернуто AI-консьєржу'
            if session.status == ChatSessionStatus.ACTIVE
            else '✅ Завершено'
        )
    elif ticket.status == TicketStatus.IN_PROGRESS or session.assigned_operator_id is not None:
        owner = ticket.assigned_operator or session.assigned_operator
        owner_label = (
            get_operator_display_name(owner)
            if owner is not None
            else (actor_name or 'Адміністратор (Telegram)')
        )
        status_line = f'🟡 В роботі · {owner_label}'
    else:
        status_line = '🟠 Очікує на адміністратора'

    if outcome and actor_name:
        status_line = f'{status_line} ({actor_name})'

    customer_parts = [_escape_truncated(customer_name, 200)]
    if user is not None and user.email:
        customer_parts.append(_escape_truncated(user.email, 200))
    else:
        customer_parts.append('Гість')

    phone = (
        ticket.customer_phone
        or (user.phone if user is not None and user.phone else '')
    )

    start_hour, _ = get_support_working_hours()
    wh_label = get_working_hours_label()
    if is_within_working_hours():
        wh_text = f'Робочий час ({wh_label})'
    else:
        wh_text = (
            f'Поза графіком ({wh_label}) — клієнт попереджений про відповідь з {start_hour:02d}:00'
        )

    lines = [
        f'<b>Звернення #{ticket.id}</b> · {_escape_truncated(reason_display, 160)}',
        f'Статус: <b>{_escape_truncated(status_line, 240)}</b>',
        f'Клієнт: {" · ".join(customer_parts)}',
    ]
    if phone:
        lines.append(f'Телефон: <code>{_escape_truncated(phone, 64)}</code>')
    if ticket.order is not None:
        order = ticket.order
        lines.append(
            'Замовлення: '
            f'<code>{_escape_truncated(order.order_number, 64)}</code> '
            f'({_escape_truncated(order.get_status_display(), 80)}, '
            f'{order.total_amount} грн)'
        )
    if ticket.details:
        details_budget = max(
            120,
            min(800, TELEGRAM_MESSAGE_LIMIT - len('\n'.join(lines)) - 512),
        )
        safe_details = _escape_truncated(ticket.details, details_budget)
        if safe_details:
            lines.append(f'Деталі: {safe_details}')
    lines.append(f'Графік: {_escape_truncated(wh_text, 240)}')

    desk_url = get_desk_ticket_url(ticket)
    if not _is_telegram_button_url_allowed(desk_url):
        lines.append(f'Пульт: <code>{_escape_truncated(desk_url, 256)}</code>')

    recent_msgs = list(
        session.messages.filter(
            role__in=[
                MessageRole.USER,
                MessageRole.ASSISTANT,
                MessageRole.OPERATOR,
            ]
        )
        .select_related('sender_user')
        .order_by('-created_at', '-id')[:TELEGRAM_EXCERPT_LIMIT]
    )
    recent_msgs.reverse()

    header_block = '\n'.join(lines)
    if not recent_msgs:
        return header_block

    excerpt_lines = ['', '<b>Останні повідомлення:</b>']
    for msg in recent_msgs:
        if msg.role == MessageRole.USER:
            speaker = 'Клієнт'
        elif msg.role == MessageRole.OPERATOR:
            speaker = (
                get_operator_display_name(msg.sender_user)
                if msg.sender_user is not None
                else 'Адміністратор'
            )
        else:
            speaker = 'AI'
        snippet = _escape_truncated(' '.join((msg.content or '').split()), 240)
        entry = (
            f'• <b>{_escape_truncated(speaker, 120)}:</b> '
            f'{snippet}'
        )
        candidate = '\n'.join([header_block, *excerpt_lines, entry])
        if len(candidate) > TELEGRAM_MESSAGE_LIMIT - 20:
            break
        excerpt_lines.append(entry)

    if len(excerpt_lines) > 2:
        return '\n'.join([header_block, *excerpt_lines])
    return header_block


def _find_ticket_for_session(session_id: Any) -> SupportTicket | None:
    return (
        SupportTicket.objects.select_related(
            'session',
            'session__user',
            'session__assigned_operator',
            'order',
            'assigned_operator',
        )
        .filter(session_id=session_id, telegram_thread_id__isnull=False)
        .order_by('-created_at', '-id')
        .first()
    )


def _save_outbound_telegram_message_id(msg_id: int, tg_message_id: Any) -> None:
    if (
        not isinstance(tg_message_id, int)
        or isinstance(tg_message_id, bool)
        or tg_message_id <= 0
    ):
        return
    try:
        ChatMessage.objects.filter(
            pk=msg_id,
            telegram_message_id__isnull=True,
        ).update(telegram_message_id=tg_message_id)
    except (IntegrityError, DatabaseError) as exc:
        logger.warning(
            'Failed to store telegram_message_id=%s for ChatMessage %s: %s',
            tg_message_id,
            msg_id,
            exc,
        )


def sync_ticket_escalated_to_telegram(ticket_id: int) -> bool:
    if not is_telegram_enabled():
        return False
    chat_id = get_telegram_support_chat_id()
    if chat_id is None:
        return False

    ticket = (
        SupportTicket.objects.select_related(
            'session',
            'session__user',
            'session__assigned_operator',
            'order',
            'assigned_operator',
        )
        .filter(pk=ticket_id)
        .first()
    )
    if (
        ticket is None
        or ticket.telegram_thread_id is not None
        or ticket.status == TicketStatus.RESOLVED
    ):
        return False

    topic_result = call_telegram_api(
        'createForumTopic',
        {
            'chat_id': chat_id,
            'name': format_telegram_topic_name(ticket),
        },
    )
    if not isinstance(topic_result, dict):
        return False
    thread_id = topic_result.get('message_thread_id')
    if not isinstance(thread_id, int) or isinstance(thread_id, bool) or thread_id <= 0:
        return False

    updated_rows = SupportTicket.objects.filter(
        pk=ticket.id,
        telegram_thread_id__isnull=True,
    ).update(telegram_thread_id=thread_id)
    if updated_rows == 0:
        return False
    ticket.telegram_thread_id = thread_id

    card_text = build_ticket_card_text(ticket)
    keyboard = build_ticket_control_keyboard(ticket)
    msg_result = call_telegram_api(
        'sendMessage',
        {
            'chat_id': chat_id,
            'message_thread_id': thread_id,
            'text': card_text,
            'parse_mode': 'HTML',
            'reply_markup': keyboard,
            'disable_web_page_preview': True,
        },
    )
    if isinstance(msg_result, dict):
        control_msg_id = msg_result.get('message_id')
        if (
            isinstance(control_msg_id, int)
            and not isinstance(control_msg_id, bool)
            and control_msg_id > 0
        ):
            SupportTicket.objects.filter(pk=ticket.id).update(
                telegram_control_message_id=control_msg_id
            )
            call_telegram_api(
                'pinChatMessage',
                {
                    'chat_id': chat_id,
                    'message_id': control_msg_id,
                    'disable_notification': True,
                },
            )
    return True


def sync_customer_message_to_telegram(message_id: int) -> bool:
    if not is_telegram_enabled():
        return False
    chat_id = get_telegram_support_chat_id()
    if chat_id is None:
        return False

    msg = (
        ChatMessage.objects.select_related('session', 'session__user')
        .filter(pk=message_id)
        .first()
    )
    if msg is None or msg.role != MessageRole.USER:
        return False

    ticket = _find_ticket_for_session(msg.session_id)
    if (
        ticket is None
        or ticket.telegram_thread_id is None
        or ticket.status == TicketStatus.RESOLVED
        or ticket.session.status != ChatSessionStatus.ESCALATED
    ):
        return False

    customer_name = _escape_truncated(
        format_session_customer_name(msg.session),
        160,
    )
    prefix = f'👤 <b>{customer_name}:</b>\n'
    text = _format_telegram_html_message(prefix, msg.content)
    result = call_telegram_api(
        'sendMessage',
        {
            'chat_id': chat_id,
            'message_thread_id': ticket.telegram_thread_id,
            'text': text,
            'parse_mode': 'HTML',
        },
    )
    if isinstance(result, dict):
        _save_outbound_telegram_message_id(msg.id, result.get('message_id'))
        return True
    return False


def sync_operator_message_to_telegram(message_id: int) -> bool:
    if not is_telegram_enabled():
        return False
    chat_id = get_telegram_support_chat_id()
    if chat_id is None:
        return False

    msg = (
        ChatMessage.objects.select_related('session', 'sender_user')
        .filter(pk=message_id)
        .first()
    )
    if (
        msg is None
        or msg.role != MessageRole.OPERATOR
        or msg.source != MessageSource.WEB
    ):
        return False

    ticket = _find_ticket_for_session(msg.session_id)
    if (
        ticket is None
        or ticket.telegram_thread_id is None
        or ticket.status == TicketStatus.RESOLVED
        or ticket.session.status != ChatSessionStatus.ESCALATED
    ):
        return False

    operator_name = _escape_truncated(
        get_operator_display_name(msg.sender_user),
        160,
    )
    prefix = f'💬 <b>[Веб-пульт · {operator_name}]:</b>\n'
    text = _format_telegram_html_message(prefix, msg.content)
    result = call_telegram_api(
        'sendMessage',
        {
            'chat_id': chat_id,
            'message_thread_id': ticket.telegram_thread_id,
            'text': text,
            'parse_mode': 'HTML',
        },
    )
    if isinstance(result, dict):
        _save_outbound_telegram_message_id(msg.id, result.get('message_id'))
        return True
    return False


def sync_ticket_card_updated_to_telegram(
    ticket_id: int,
    *,
    actor_name: str = '',
    force_closed: bool = False,
    outcome: str = '',
) -> bool:
    if not is_telegram_enabled():
        return False
    chat_id = get_telegram_support_chat_id()
    if chat_id is None:
        return False

    ticket = (
        SupportTicket.objects.select_related(
            'session',
            'session__user',
            'session__assigned_operator',
            'order',
            'assigned_operator',
        )
        .filter(pk=ticket_id)
        .first()
    )
    if (
        ticket is None
        or ticket.telegram_thread_id is None
        or ticket.telegram_control_message_id is None
    ):
        return False

    card_text = build_ticket_card_text(
        ticket,
        outcome=outcome,
        actor_name=actor_name,
    )
    keyboard = build_ticket_control_keyboard(
        ticket,
        force_closed=force_closed,
    )
    result = call_telegram_api(
        'editMessageText',
        {
            'chat_id': chat_id,
            'message_id': ticket.telegram_control_message_id,
            'text': card_text,
            'parse_mode': 'HTML',
            'reply_markup': keyboard,
            'disable_web_page_preview': True,
        },
    )
    return result is not None


def sync_ticket_claimed_to_telegram(
    ticket_id: int,
    *,
    operator_name: str = '',
    is_takeover: bool = False,
    notify_thread: bool = True,
) -> bool:
    if not is_telegram_enabled():
        return False
    chat_id = get_telegram_support_chat_id()
    if chat_id is None:
        return False

    ticket = (
        SupportTicket.objects.select_related(
            'session',
            'session__user',
            'session__assigned_operator',
            'order',
            'assigned_operator',
        )
        .filter(pk=ticket_id)
        .first()
    )
    if ticket is None or ticket.telegram_thread_id is None:
        return False

    resolved_actor = operator_name or (
        get_operator_display_name(ticket.assigned_operator)
        if ticket.assigned_operator is not None
        else 'Адміністратор (Telegram)'
    )
    sync_ticket_card_updated_to_telegram(ticket.id, actor_name=resolved_actor)

    if notify_thread:
        safe_actor = html.escape(resolved_actor, quote=False)
        notice = (
            f'🔄 Діалог перехопив: <b>{safe_actor}</b>'
            if is_takeover
            else f'✋ Діалог взяв у роботу: <b>{safe_actor}</b>'
        )
        call_telegram_api(
            'sendMessage',
            {
                'chat_id': chat_id,
                'message_thread_id': ticket.telegram_thread_id,
                'text': notice,
                'parse_mode': 'HTML',
            },
        )
    return True


def sync_ticket_closed_to_telegram(
    ticket_id: int,
    *,
    outcome: str = 'resolved',
    actor_name: str = '',
) -> bool:
    if not is_telegram_enabled():
        return False
    chat_id = get_telegram_support_chat_id()
    if chat_id is None:
        return False

    ticket = (
        SupportTicket.objects.select_related(
            'session',
            'session__user',
            'session__assigned_operator',
            'order',
            'assigned_operator',
        )
        .filter(pk=ticket_id)
        .first()
    )
    if ticket is None or ticket.telegram_thread_id is None:
        return False

    sync_ticket_card_updated_to_telegram(
        ticket.id,
        actor_name=actor_name,
        force_closed=True,
        outcome=outcome,
    )

    safe_actor = html.escape(actor_name, quote=False) if actor_name else ''
    actor_suffix = f' ({safe_actor})' if safe_actor else ''
    if outcome == 'returned_to_ai':
        final_text = f'🤖 Діалог повернуто AI-консьєржу{actor_suffix}. Тему закрито.'
    elif outcome == 'client_reset':
        final_text = '🔄 Клієнт почав новий діалог. Тему закрито.'
    else:
        final_text = f'✅ Звернення завершено{actor_suffix}. Тему закрито.'

    call_telegram_api(
        'sendMessage',
        {
            'chat_id': chat_id,
            'message_thread_id': ticket.telegram_thread_id,
            'text': final_text,
            'parse_mode': 'HTML',
        },
    )
    call_telegram_api(
        'closeForumTopic',
        {
            'chat_id': chat_id,
            'message_thread_id': ticket.telegram_thread_id,
        },
    )
    return True


def answer_telegram_callback_query(
    callback_query_id: str,
    text: str = '',
    *,
    show_alert: bool = False,
) -> bool:
    if not is_telegram_enabled() or not callback_query_id:
        return False
    payload: dict[str, Any] = {
        'callback_query_id': callback_query_id,
        'show_alert': show_alert,
    }
    if text:
        payload['text'] = text[:200]
    result = call_telegram_api('answerCallbackQuery', payload)
    return result is not None
