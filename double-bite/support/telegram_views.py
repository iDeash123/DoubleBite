import json
import logging
import secrets
from typing import Any

from config.security import (
    consume_rate_limit,
    get_client_ip,
    rate_limit_429_response,
)
from django.contrib.auth import get_user_model
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from support.models import (
    ChatMessage,
    ChatSessionStatus,
    MessageSource,
    SupportTicket,
    TicketStatus,
)
from support.permissions import is_support_operator
from support.services import (
    MAX_SUPPORT_MESSAGE_LENGTH,
    InvalidMessageError,
    InvalidSessionStateError,
    SessionNotFoundError,
    SessionOwnershipConflictError,
    SupportDeskService,
    get_operator_display_name,
)
from support.telegram import (
    answer_telegram_callback_query,
    get_telegram_support_chat_id,
    get_telegram_webhook_secret,
    is_telegram_enabled,
    sync_ticket_card_updated_to_telegram,
)

logger = logging.getLogger('support')
User = get_user_model()
_INT32_MAX = 2**31 - 1
_VALID_CALLBACK_ACTIONS = frozenset({'claim', 'return_ai', 'resolve'})


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _resolve_telegram_operator(from_user: dict[str, Any]) -> tuple[Any | None, str]:
    first_name = str(from_user.get('first_name') or '').strip()
    last_name = str(from_user.get('last_name') or '').strip()
    username = str(from_user.get('username') or '').strip()
    tg_display = f'{first_name} {last_name}'.strip()
    if not tg_display and username:
        tg_display = f'@{username}'
    if not tg_display:
        tg_display = 'Адміністратор (Telegram)'

    tg_user_id = from_user.get('id')
    if _is_strict_int(tg_user_id) and tg_user_id > 0:
        user = User.objects.filter(telegram_id=tg_user_id, is_active=True).first()
        if user is not None and is_support_operator(user):
            return user, get_operator_display_name(user)

    return None, tg_display


def _parse_callback_data(raw_data: Any) -> tuple[str | None, int | None]:
    cleaned = str(raw_data or '').strip()
    if ':' not in cleaned:
        return None, None
    action, raw_ticket_id = cleaned.split(':', 1)
    if action not in _VALID_CALLBACK_ACTIONS:
        return None, None
    if not raw_ticket_id.isascii() or not raw_ticket_id.isdigit():
        return None, None
    ticket_id = int(raw_ticket_id)
    if not (1 <= ticket_id <= _INT32_MAX):
        return None, None
    return action, ticket_id


def _handle_callback_query(
    cb: dict[str, Any],
    *,
    support_chat_id: int,
) -> JsonResponse:
    callback_id = str(cb.get('id') or '').strip()
    from_user = cb.get('from') if isinstance(cb.get('from'), dict) else {}
    if from_user.get('is_bot') is True:
        answer_telegram_callback_query(callback_id)
        return JsonResponse({'ok': True, 'ignored': 'bot_callback'})

    cb_message = cb.get('message') if isinstance(cb.get('message'), dict) else {}
    cb_chat = cb_message.get('chat') if isinstance(cb_message.get('chat'), dict) else {}
    chat_id = cb_chat.get('id')
    if not _is_strict_int(chat_id) or chat_id != support_chat_id:
        return JsonResponse({'ok': False, 'error': 'Unexpected chat_id.'}, status=403)

    action, ticket_id = _parse_callback_data(cb.get('data'))
    if action is None or ticket_id is None:
        answer_telegram_callback_query(callback_id, 'Невідома дія.')
        return JsonResponse({'ok': True, 'ignored': 'invalid_callback_data'})

    ticket = (
        SupportTicket.objects.select_related('session', 'assigned_operator')
        .filter(pk=ticket_id)
        .first()
    )
    if ticket is None:
        answer_telegram_callback_query(
            callback_id,
            'Тікет не знайдено.',
            show_alert=True,
        )
        return JsonResponse({'ok': True, 'ignored': 'ticket_not_found'})

    if ticket.status == TicketStatus.RESOLVED:
        answer_telegram_callback_query(
            callback_id,
            'Тікет вже закрито.',
            show_alert=True,
        )
        sync_ticket_card_updated_to_telegram(ticket.id, force_closed=True)
        return JsonResponse({
            'ok': True,
            'status': 'invalid_state',
            'error': 'Тікет вже закрито.',
        })

    operator, actor_name = _resolve_telegram_operator(from_user)
    try:
        if action == 'claim':
            SupportDeskService.claim_session(
                ticket.session_id,
                operator,
                source=MessageSource.TELEGRAM,
                actor_name=actor_name,
                expected_ticket_id=ticket.id,
            )
            answer_telegram_callback_query(callback_id, 'Звернення взято в роботу')
        elif action == 'return_ai':
            SupportDeskService.return_to_ai(
                ticket.session_id,
                operator,
                source=MessageSource.TELEGRAM,
                actor_name=actor_name,
                expected_ticket_id=ticket.id,
            )
            answer_telegram_callback_query(callback_id, 'Чат повернуто AI-консьєржу')
        else:
            SupportDeskService.resolve_session(
                ticket.session_id,
                operator,
                source=MessageSource.TELEGRAM,
                actor_name=actor_name,
                expected_ticket_id=ticket.id,
            )
            answer_telegram_callback_query(callback_id, 'Звернення завершено')
    except SessionOwnershipConflictError as exc:
        answer_telegram_callback_query(callback_id, str(exc), show_alert=True)
        return JsonResponse({'ok': True, 'status': 'conflict', 'error': str(exc)})
    except (InvalidSessionStateError, SessionNotFoundError) as exc:
        answer_telegram_callback_query(callback_id, str(exc), show_alert=True)
        ticket.refresh_from_db(fields=['status'])
        if ticket.status == TicketStatus.RESOLVED:
            sync_ticket_card_updated_to_telegram(ticket.id, force_closed=True)
        return JsonResponse({'ok': True, 'status': 'invalid_state', 'error': str(exc)})

    return JsonResponse({
        'ok': True,
        'action': action,
        'ticket_id': ticket.id,
        'session_uuid': str(ticket.session_id),
    })


def _handle_topic_message(
    message: dict[str, Any],
    *,
    support_chat_id: int,
) -> JsonResponse:
    chat = message.get('chat') if isinstance(message.get('chat'), dict) else {}
    chat_id = chat.get('id')
    if not _is_strict_int(chat_id) or chat_id != support_chat_id:
        return JsonResponse({'ok': False, 'error': 'Unexpected chat_id.'}, status=403)

    from_user = message.get('from') if isinstance(message.get('from'), dict) else {}
    if from_user.get('is_bot') is True:
        return JsonResponse({'ok': True, 'ignored': 'bot_message'})

    thread_id = message.get('message_thread_id')
    if not _is_strict_int(thread_id) or thread_id <= 0:
        return JsonResponse({'ok': True, 'ignored': 'no_thread_id'})

    tg_message_id = message.get('message_id')
    if not _is_strict_int(tg_message_id) or tg_message_id <= 0:
        return JsonResponse({'ok': True, 'ignored': 'invalid_message_id'})

    raw_text = message.get('text')
    if not isinstance(raw_text, str):
        raw_text = message.get('caption')
    if not isinstance(raw_text, str):
        return JsonResponse({'ok': True, 'ignored': 'non_text_message'})

    cleaned_text = raw_text.replace('\x00', '').strip()
    if not cleaned_text:
        return JsonResponse({'ok': True, 'ignored': 'empty_message'})
    if len(cleaned_text) > MAX_SUPPORT_MESSAGE_LENGTH:
        cleaned_text = cleaned_text[: MAX_SUPPORT_MESSAGE_LENGTH - 1].rstrip() + '…'

    if ChatMessage.objects.filter(telegram_message_id=tg_message_id).exists():
        return JsonResponse({'ok': True, 'duplicate': True})

    ticket = (
        SupportTicket.objects.select_related('session')
        .filter(telegram_thread_id=thread_id)
        .order_by('-created_at', '-id')
        .first()
    )
    if ticket is None:
        return JsonResponse({'ok': True, 'ignored': 'unknown_thread'})
    if (
        ticket.status == TicketStatus.RESOLVED
        or ticket.session.status != ChatSessionStatus.ESCALATED
    ):
        return JsonResponse({'ok': True, 'ignored': 'ticket_closed'})

    operator, actor_name = _resolve_telegram_operator(from_user)
    try:
        saved_msg = SupportDeskService.send_operator_message(
            ticket.session_id,
            operator,
            cleaned_text,
            source=MessageSource.TELEGRAM,
            telegram_message_id=tg_message_id,
            actor_name=actor_name,
        )
    except InvalidMessageError:
        return JsonResponse({'ok': True, 'ignored': 'invalid_message'})
    except (InvalidSessionStateError, SessionNotFoundError):
        return JsonResponse({'ok': True, 'ignored': 'session_not_escalated'})

    return JsonResponse({
        'ok': True,
        'message_id': saved_msg.id,
        'session_uuid': str(ticket.session_id),
    })


@csrf_exempt
@require_POST
def telegram_webhook_view(request: HttpRequest) -> HttpResponse:
    ip = get_client_ip(request)
    limited, retry_after = consume_rate_limit('telegram_webhook_ip', ip, 120, 60)
    if limited:
        return rate_limit_429_response(
            request,
            retry_after,
            'Забагато запитів webhook.',
            json_body=True,
        )

    if not is_telegram_enabled():
        return JsonResponse(
            {'ok': False, 'error': 'Telegram integration is disabled.'},
            status=503,
        )

    expected_secret = get_telegram_webhook_secret()
    if not expected_secret:
        return JsonResponse(
            {'ok': False, 'error': 'Telegram webhook secret is not configured.'},
            status=503,
        )

    provided_secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not provided_secret or not secrets.compare_digest(provided_secret, expected_secret):
        return JsonResponse({'ok': False, 'error': 'Forbidden'}, status=403)

    support_chat_id = get_telegram_support_chat_id()
    if support_chat_id is None:
        return JsonResponse(
            {'ok': False, 'error': 'Telegram support chat ID is not configured.'},
            status=503,
        )

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON payload.'}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON payload.'}, status=400)

    callback_query = payload.get('callback_query')
    if isinstance(callback_query, dict):
        return _handle_callback_query(callback_query, support_chat_id=support_chat_id)

    message = payload.get('message')
    if isinstance(message, dict):
        return _handle_topic_message(message, support_chat_id=support_chat_id)

    return JsonResponse({'ok': True, 'ignored': 'unsupported_update'})
