import asyncio
import contextlib
import json
import logging
import queue
import threading
from collections.abc import AsyncGenerator, Iterator
from typing import Any

from asgiref.sync import sync_to_async
from config.security import (
    consume_rate_limit,
    get_client_ip,
    rate_limit_429_response,
)
from config.transactions import schedule_on_commit
from django.db import close_old_connections, connections
from django.http import (
    HttpRequest,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from orders.middleware import get_guest_token

from support import services as support_services
from support.agent.client import MistralSupportAgent
from support.models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    MessageRole,
    MessageSource,
    TicketReason,
    TicketStatus,
)
from support.realtime import (
    build_message_event_data,
    publish_desk_event,
    publish_session_event,
    subscribe_client_session_events,
)
from support.services import (
    ChatSessionService,
    InvalidMessageError,
    InvalidSessionStateError,
    SessionNotFoundError,
    SupportDeskService,
)

logger = logging.getLogger('support')

_KIND_ITEM = 0
_KIND_ERR = 1
_KIND_DONE = 2
_INT64_MAX = 2**63 - 1
OPERATOR_HISTORY_PREFIX = '[Відповідь адміністратора ресторану]: '
_CLIENT_ESCALATION_REASONS = frozenset({
    TicketReason.HUMAN_REQUESTED,
    TicketReason.COMPLAINT,
    TicketReason.ORDER_ISSUE,
    TicketReason.BOT_CONFUSED,
})


def _close_non_atomic_connections() -> None:
    if not any(conn.in_atomic_block for conn in connections.all(initialized_only=True)):
        close_old_connections()


def _close_thread_connections() -> None:
    for conn in connections.all(initialized_only=True):
        if not conn.in_atomic_block:
            conn.close()


def _save_message(
    session_uuid: Any,
    role: str,
    content: str,
    source: str | None = None,
) -> ChatMessage:
    _close_non_atomic_connections()
    session = ChatSession.objects.select_related('user').get(
        session_uuid=session_uuid
    )
    resolved_source = source
    if resolved_source is None:
        if role == MessageRole.ASSISTANT:
            resolved_source = MessageSource.AI
        elif role in (MessageRole.SYSTEM, MessageRole.TOOL):
            resolved_source = MessageSource.SYSTEM
        else:
            resolved_source = MessageSource.WEB
    msg = ChatMessage.objects.create(
        session=session,
        role=role,
        source=resolved_source,
        content=content,
    )
    ChatSession.objects.filter(pk=session.pk).update(
        last_message_at=msg.created_at,
        updated_at=msg.created_at,
    )
    if role != MessageRole.TOOL:
        schedule_on_commit(
            publish_session_event,
            session.session_uuid,
            'message',
            build_message_event_data(msg, session=session),
            event_id=msg.id,
        )
        schedule_on_commit(
            publish_desk_event,
            'queue_update',
            {
                'reason': 'ai_chat_message',
                'session_uuid': str(session.session_uuid),
                'message_id': msg.id,
                'status': session.status,
            },
            session_uuid=session.session_uuid,
        )
    return msg


def _get_history(session_uuid: Any) -> list[dict[str, Any]]:
    session = ChatSession.objects.get(session_uuid=session_uuid)
    msgs = list(
        session.messages.filter(
            role__in=[
                MessageRole.USER,
                MessageRole.ASSISTANT,
                MessageRole.OPERATOR,
            ],
        ).order_by('-created_at', '-id')[:20]
    )
    msgs.reverse()
    history: list[dict[str, Any]] = []
    for m in msgs:
        content = m.content
        if m.role == MessageRole.OPERATOR:
            content = f'{OPERATOR_HISTORY_PREFIX}{content}'
        history.append({'role': m.role, 'content': content})
    return history


def _resolve_rate_limit_identity(
    request: HttpRequest,
    *,
    create_guest: bool = True,
) -> str:
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        return f'user:{user.id}'
    token = get_guest_token(request, create=create_guest)
    return f'guest:{token}' if token else ''


def _iter_async_gen_new_loop(async_gen: AsyncGenerator[Any, None]) -> Iterator[Any]:
    """Iterate an async generator on a dedicated local loop when none is running."""
    loop = asyncio.new_event_loop()
    try:
        while True:
            try:
                item = loop.run_until_complete(async_gen.__anext__())
            except StopAsyncIteration:
                break
            yield item
    finally:
        try:
            aclose = getattr(async_gen, 'aclose', None)
            if callable(aclose):
                loop.run_until_complete(aclose())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                try:
                    loop.run_until_complete(
                        sync_to_async(_close_non_atomic_connections, thread_sensitive=True)()
                    )
                finally:
                    loop.close()
                    _close_non_atomic_connections()


def _iter_async_gen_threaded(async_gen: AsyncGenerator[Any, None]) -> Iterator[Any]:
    """Bridge an async generator across threads when the caller already has a loop."""
    item_queue: queue.Queue[tuple[int, Any]] = queue.Queue(maxsize=16)
    stop_event = threading.Event()
    state: dict[str, Any] = {'loop': None, 'task': None, 'closing': False}

    def _put(entry: tuple[int, Any]) -> bool:
        while not stop_event.is_set():
            try:
                item_queue.put(entry, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _worker() -> None:
        loop = asyncio.new_event_loop()
        state['loop'] = loop

        async def _consume() -> None:
            finished_normally = False
            try:
                while not stop_event.is_set():
                    try:
                        item = await async_gen.__anext__()
                    except StopAsyncIteration:
                        finished_normally = True
                        break
                    if stop_event.is_set() or not _put((_KIND_ITEM, item)):
                        break
            except BaseException as exc:  # noqa: BLE001 - propagate any worker error to caller thread
                if not stop_event.is_set():
                    _put((_KIND_ERR, exc))
            finally:
                state['closing'] = True
                try:
                    aclose = getattr(async_gen, 'aclose', None)
                    if callable(aclose):
                        await aclose()
                except BaseException as close_exc:  # noqa: BLE001 - propagate close failure to caller thread
                    if not stop_event.is_set() and finished_normally:
                        _put((_KIND_ERR, close_exc))
                        finished_normally = False
                if finished_normally and not stop_event.is_set():
                    _put((_KIND_DONE, None))

        try:
            task = loop.create_task(_consume())
            state['task'] = task
            loop.run_until_complete(task)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                try:
                    loop.run_until_complete(
                        sync_to_async(_close_non_atomic_connections, thread_sensitive=True)()
                    )
                finally:
                    loop.close()
                    _close_thread_connections()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    try:
        while True:
            kind, payload = item_queue.get()
            if kind == _KIND_DONE:
                break
            if kind == _KIND_ERR:
                raise payload
            yield payload
    finally:
        stop_event.set()
        while not item_queue.empty():
            with contextlib.suppress(queue.Empty):
                item_queue.get_nowait()
        worker_loop = state['loop']
        worker_task = state['task']
        if worker_loop is not None and worker_task is not None and not state['closing']:
            def _cancel_if_active() -> None:
                if not state['closing']:
                    worker_task.cancel()

            with contextlib.suppress(RuntimeError):
                worker_loop.call_soon_threadsafe(_cancel_if_active)
        thread.join(timeout=2.0)


def _iter_async_gen_sync(async_gen: AsyncGenerator[Any, None]) -> Iterator[Any]:
    """Adapt an async SSE generator for synchronous WSGI responses."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        yield from _iter_async_gen_new_loop(async_gen)
        return
    yield from _iter_async_gen_threaded(async_gen)


def _resolve_message_sender(role: str) -> str:
    if role == MessageRole.OPERATOR:
        return 'operator'
    if role == MessageRole.ASSISTANT:
        return 'ai'
    if role == MessageRole.SYSTEM:
        return 'system'
    if role == MessageRole.USER:
        return 'user'
    return 'ai'


async def chat_stream_view(request: HttpRequest) -> HttpResponse:
    if request.method != 'POST':
        return HttpResponse('Method Not Allowed', status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'Невірний формат JSON.'}, status=400)

    user_text = str(data.get('message', '')).strip()
    if not user_text:
        return JsonResponse({'error': 'Повідомлення не може бути порожнім.'}, status=400)

    if len(user_text) > 2000:
        user_text = user_text[:2000]

    ip = get_client_ip(request)
    session_identity = await sync_to_async(
        _resolve_rate_limit_identity, thread_sensitive=True
    )(request)

    ip_limited, ip_retry_after = await sync_to_async(
        consume_rate_limit, thread_sensitive=True
    )('chat_stream_ip', ip, 10, 60)
    session_limited = False
    session_retry_after = 0
    if session_identity:
        session_limited, session_retry_after = await sync_to_async(
            consume_rate_limit, thread_sensitive=True
        )('chat_stream_session', session_identity, 10, 60)

    if ip_limited or session_limited:
        retry_after = max(ip_retry_after, session_retry_after)
        return rate_limit_429_response(
            request,
            retry_after,
            'Забагато запитів. Ліміт: 10 повідомлень на хвилину.',
            json_body=True,
        )

    session = await sync_to_async(
        ChatSessionService.get_or_create_active_session, thread_sensitive=True
    )(request)
    if session.is_escalated or session.status == ChatSessionStatus.ESCALATED:
        return JsonResponse(
            {
                'error': 'Запит передано оператору, очікуйте або почніть новий чат.',
                'is_escalated': True,
                'status': session.status,
            },
            status=409,
        )

    session_uuid = session.session_uuid

    await sync_to_async(_save_message, thread_sensitive=True)(
        session_uuid, MessageRole.USER, user_text
    )

    history = await sync_to_async(_get_history, thread_sensitive=True)(session_uuid)
    agent = MistralSupportAgent()

    async def event_generator() -> AsyncGenerator[str, None]:
        assistant_chunks: list[str] = []
        stream: Any = None
        stream_closed = False

        async def _close_stream() -> None:
            nonlocal stream_closed
            if stream_closed or stream is None:
                return
            stream_closed = True
            aclose = getattr(stream, 'aclose', None)
            if callable(aclose):
                await aclose()

        try:
            stream = agent.stream_chat_response(
                history, request=request, session=session
            )
            async for event in stream:
                if 'token' in event:
                    assistant_chunks.append(event['token'])
                line = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                yield line

            full_reply = ''.join(assistant_chunks).strip()
            if full_reply:
                await sync_to_async(_save_message, thread_sensitive=True)(
                    session_uuid, MessageRole.ASSISTANT, full_reply
                )
            yield f"data: {json.dumps({'done': True})}\n\n"
        except asyncio.CancelledError:
            logger.info('Chat stream client disconnected (session=%s)', session_uuid)
            await _close_stream()
            raise
        except Exception:
            logger.exception('Stream error')
            err_payload = json.dumps(
                {'error': 'Виникла помилка під час формування відповіді.'},
                ensure_ascii=False,
            )
            yield f'data: {err_payload}\n\n'
        finally:
            try:
                await _close_stream()
            finally:
                await sync_to_async(
                    _close_non_atomic_connections, thread_sensitive=True
                )()

    stream_content: Any = (
        event_generator()
        if hasattr(request, 'scope')
        else _iter_async_gen_sync(event_generator())
    )
    response = StreamingHttpResponse(
        stream_content,
        content_type='text/event-stream',
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    response['X-Chat-Session-UUID'] = str(session_uuid)
    return response


def _serialize_client_message(msg: ChatMessage) -> dict[str, Any]:
    created_at = (
        timezone.localtime(msg.created_at)
        if timezone.is_aware(msg.created_at)
        else msg.created_at
    )
    return {
        'id': msg.id,
        'role': msg.role,
        'sender': _resolve_message_sender(msg.role),
        'content': msg.content,
        'created_at': created_at.strftime('%H:%M'),
    }


def _is_operator_connected(session: ChatSession) -> bool:
    if session.status != ChatSessionStatus.ESCALATED:
        return False
    if session.assigned_operator_id is not None:
        return True
    return session.tickets.filter(status=TicketStatus.IN_PROGRESS).exists()


def _parse_after_id(raw_value: str | None) -> tuple[int | None, str | None]:
    if raw_value is None or raw_value == '':
        return None, None
    cleaned = raw_value.strip()
    if not cleaned.isascii() or not cleaned.isdigit():
        return None, 'Параметр after_id має бути цілим невідʼємним числом.'
    value = int(cleaned)
    if value > _INT64_MAX:
        return None, 'Параметр after_id перевищує допустиме значення.'
    return value, None


def _resolve_sse_after_id(request: HttpRequest) -> tuple[int | None, str | None]:
    query_after_id, error = _parse_after_id(request.GET.get('after_id'))
    if error is not None:
        return None, error

    header_after_id, header_error = _parse_after_id(
        request.headers.get('Last-Event-ID')
    )
    if header_error is not None:
        header_after_id = None

    if query_after_id is not None and header_after_id is not None:
        return max(query_after_id, header_after_id), None
    if header_after_id is not None:
        return header_after_id, None
    return query_after_id, None


def _resolve_client_session_for_events(request: HttpRequest) -> ChatSession | None:
    _close_non_atomic_connections()
    try:
        user = getattr(request, 'user', None)
        if (user is None or not user.is_authenticated) and not get_guest_token(
            request, create=False
        ):
            return None
        return ChatSessionService.get_latest_session(request)
    finally:
        _close_non_atomic_connections()


def _parse_json_body(
    request: HttpRequest,
    *,
    allow_empty: bool = False,
) -> tuple[dict[str, Any] | None, JsonResponse | None]:
    content_type = (request.content_type or '').lower()
    if allow_empty and content_type in (
        'application/x-www-form-urlencoded',
        'multipart/form-data',
    ):
        return {k: v for k, v in request.POST.items()}, None

    raw_body = request.body
    if allow_empty and (not raw_body or not raw_body.strip()):
        return {}, None
    try:
        parsed = json.loads(raw_body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({'error': 'Невірний формат JSON.'}, status=400)
    if not isinstance(parsed, dict):
        return None, JsonResponse({'error': 'Невірний формат JSON.'}, status=400)
    return parsed, None


@require_POST
def chat_escalate_view(request: HttpRequest) -> HttpResponse:
    data, error_response = _parse_json_body(request, allow_empty=True)
    if error_response is not None or data is None:
        return error_response or JsonResponse({'error': 'Невірний формат JSON.'}, status=400)

    raw_reason = data.get('reason')
    if raw_reason is not None and not isinstance(raw_reason, str):
        return JsonResponse({'error': 'Невірний формат причини звернення.'}, status=400)
    if isinstance(raw_reason, str) and '\x00' in raw_reason:
        return JsonResponse({'error': 'Причина містить неприпустимі символи.'}, status=400)
    normalized_reason = (
        raw_reason.strip()
        if isinstance(raw_reason, str) and raw_reason.strip() in _CLIENT_ESCALATION_REASONS
        else TicketReason.HUMAN_REQUESTED
    )

    raw_details = data.get('details')
    if raw_details is not None and not isinstance(raw_details, str):
        return JsonResponse({'error': 'Невірний формат опису звернення.'}, status=400)
    if isinstance(raw_details, str) and '\x00' in raw_details:
        return JsonResponse({'error': 'Опис містить неприпустимі символи.'}, status=400)
    details = (
        raw_details.strip()[:1000]
        if isinstance(raw_details, str) and raw_details.strip()
        else 'Клієнт запросив підключення адміністратора.'
    )

    raw_phone = data.get('customer_phone')
    if raw_phone is not None and not isinstance(raw_phone, str):
        return JsonResponse({'error': 'Невірний формат номера телефону.'}, status=400)
    if isinstance(raw_phone, str) and '\x00' in raw_phone:
        return JsonResponse({'error': 'Телефон містить неприпустимі символи.'}, status=400)
    customer_phone = raw_phone.strip()[:20] if isinstance(raw_phone, str) else ''

    ip = get_client_ip(request)
    session_identity = _resolve_rate_limit_identity(request, create_guest=True)
    ip_limited, ip_retry_after = consume_rate_limit('chat_escalate_ip', ip, 5, 60)
    session_limited = False
    session_retry_after = 0
    if session_identity:
        session_limited, session_retry_after = consume_rate_limit(
            'chat_escalate_session', session_identity, 5, 60
        )
    if ip_limited or session_limited:
        retry_after = max(ip_retry_after, session_retry_after)
        return rate_limit_429_response(
            request,
            retry_after,
            'Забагато запитів. Ліміт: 5 викликів оператора на хвилину.',
            json_body=True,
        )

    now = timezone.now()
    session = ChatSessionService.get_or_create_active_session(request)
    try:
        ticket = ChatSessionService.escalate_session(
            session=session,
            reason=normalized_reason,
            details=details,
            customer_phone=customer_phone,
            now=now,
        )
    except InvalidSessionStateError as exc:
        current = ChatSession.objects.filter(pk=session.pk).first()
        response = JsonResponse(
            {
                'error': str(exc),
                'is_escalated': bool(current and current.is_escalated),
                'status': current.status if current else ChatSessionStatus.RESOLVED,
            },
            status=409,
        )
        response['Cache-Control'] = 'no-store'
        return response

    working_hours = support_services.is_within_working_hours(now=now)
    working_hours_label = support_services.get_working_hours_label()
    off_hours_notice = support_services.format_off_hours_notice()
    msg_objs = list(
        session.messages.exclude(role=MessageRole.TOOL).order_by('created_at', 'id')
    )
    messages = [_serialize_client_message(m) for m in msg_objs]
    last_message_id = max((m.id for m in msg_objs), default=0)

    response = JsonResponse({
        'status': session.status,
        'session_uuid': str(session.session_uuid),
        'ticket_id': ticket.id,
        'is_escalated': session.is_escalated,
        'operator_connected': _is_operator_connected(session),
        'is_working_hours': working_hours,
        'working_hours_label': working_hours_label,
        'off_hours_notice': off_hours_notice,
        'last_message_id': last_message_id,
        'messages': messages,
    })
    response['Cache-Control'] = 'no-store'
    return response


@require_POST
def chat_message_view(request: HttpRequest) -> HttpResponse:
    data, error_response = _parse_json_body(request, allow_empty=False)
    if error_response is not None or data is None:
        return error_response or JsonResponse({'error': 'Невірний формат JSON.'}, status=400)

    try:
        cleaned_text = support_services.validate_message_content(data.get('message'))
    except InvalidMessageError as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    ip = get_client_ip(request)
    session_identity = _resolve_rate_limit_identity(request, create_guest=False)
    ip_limited, ip_retry_after = consume_rate_limit('chat_message_ip', ip, 20, 60)
    session_limited = False
    session_retry_after = 0
    if session_identity:
        session_limited, session_retry_after = consume_rate_limit(
            'chat_message_session', session_identity, 20, 60
        )
    if ip_limited or session_limited:
        retry_after = max(ip_retry_after, session_retry_after)
        return rate_limit_429_response(
            request,
            retry_after,
            'Забагато запитів. Ліміт: 20 повідомлень на хвилину.',
            json_body=True,
        )

    session = ChatSessionService.get_active_session(request)
    if session is None:
        latest = ChatSessionService.get_latest_session(request)
        if latest is not None and latest.status == ChatSessionStatus.RESOLVED:
            response = JsonResponse(
                {
                    'error': (
                        'Звернення завершено. Натисніть «Новий діалог», '
                        'щоб почати нову розмову.'
                    ),
                    'is_escalated': False,
                    'status': ChatSessionStatus.RESOLVED,
                },
                status=409,
            )
            response['Cache-Control'] = 'no-store'
            return response
        response = JsonResponse(
            {
                'error': 'Повідомлення оператору можна надсилати лише в ескальованій сесії.',
                'is_escalated': False,
                'status': ChatSessionStatus.ACTIVE,
            },
            status=409,
        )
        response['Cache-Control'] = 'no-store'
        return response

    if session.status != ChatSessionStatus.ESCALATED:
        response = JsonResponse(
            {
                'error': 'Повідомлення оператору можна надсилати лише в ескальованій сесії.',
                'is_escalated': False,
                'status': session.status,
            },
            status=409,
        )
        response['Cache-Control'] = 'no-store'
        return response

    if request.user.is_authenticated and session.user_id is None:
        session = ChatSessionService.attach_guest_session_to_user(request, session)

    try:
        msg = SupportDeskService.send_customer_message(
            session,
            cleaned_text,
            source=MessageSource.WEB,
        )
    except InvalidMessageError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except (InvalidSessionStateError, SessionNotFoundError) as exc:
        current = ChatSession.objects.filter(pk=session.pk).first()
        response = JsonResponse(
            {
                'error': str(exc),
                'is_escalated': bool(current and current.is_escalated),
                'status': current.status if current else ChatSessionStatus.RESOLVED,
            },
            status=409,
        )
        response['Cache-Control'] = 'no-store'
        return response

    response = JsonResponse(
        {
            'message': _serialize_client_message(msg),
            'is_working_hours': support_services.is_within_working_hours(),
            'working_hours_label': support_services.get_working_hours_label(),
            'off_hours_notice': support_services.format_off_hours_notice(),
        },
        status=201,
    )
    response['Cache-Control'] = 'no-store'
    return response


@require_POST
def chat_reset_view(request: HttpRequest) -> HttpResponse:
    ip = get_client_ip(request)
    limited, retry_after = consume_rate_limit('chat_reset_ip', ip, 10, 60)
    if limited:
        return rate_limit_429_response(
            request,
            retry_after,
            'Забагато запитів. Спробуйте пізніше.',
            json_body=True,
        )

    new_session = ChatSessionService.reset_active_session(request)
    response = JsonResponse({
        'status': 'ok',
        'session_uuid': str(new_session.session_uuid),
        'message': 'Сесію успішно скинуто.',
    })
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def chat_history_view(request: HttpRequest) -> JsonResponse:
    after_id, after_id_error = _parse_after_id(request.GET.get('after_id'))
    if after_id_error is not None:
        return JsonResponse({'error': after_id_error}, status=400)

    working_hours = support_services.is_within_working_hours()
    working_hours_label = support_services.get_working_hours_label()
    off_hours_notice = support_services.format_off_hours_notice()

    empty_payload = {
        'session_uuid': None,
        'status': ChatSessionStatus.ACTIVE,
        'is_escalated': False,
        'operator_connected': False,
        'is_working_hours': working_hours,
        'working_hours_label': working_hours_label,
        'off_hours_notice': off_hours_notice,
        'last_message_id': 0,
        'messages': [],
    }

    if not request.user.is_authenticated and not get_guest_token(request, create=False):
        response = JsonResponse(empty_payload)
        response['Cache-Control'] = 'no-store'
        return response

    session = ChatSessionService.get_latest_session(request)
    if not session:
        response = JsonResponse(empty_payload)
        response['Cache-Control'] = 'no-store'
        return response

    visible_qs = session.messages.exclude(role=MessageRole.TOOL)
    if after_id is not None:
        msg_objs = list(visible_qs.filter(id__gt=after_id).order_by('created_at', 'id'))
        if msg_objs:
            last_message_id = max(m.id for m in msg_objs)
        else:
            last_message_id = (
                visible_qs.order_by('-id').values_list('id', flat=True).first() or 0
            )
    else:
        msg_objs = list(visible_qs.order_by('created_at', 'id'))
        last_message_id = max((m.id for m in msg_objs), default=0)

    messages = [_serialize_client_message(m) for m in msg_objs]
    response = JsonResponse({
        'session_uuid': str(session.session_uuid),
        'status': session.status,
        'is_escalated': session.is_escalated,
        'operator_connected': _is_operator_connected(session),
        'is_working_hours': working_hours,
        'working_hours_label': working_hours_label,
        'off_hours_notice': off_hours_notice,
        'last_message_id': last_message_id,
        'messages': messages,
    })
    response['Cache-Control'] = 'no-store'
    return response


@require_http_methods(['GET'])
async def chat_events_view(request: HttpRequest) -> HttpResponse:
    after_id, after_id_error = _resolve_sse_after_id(request)
    if after_id_error is not None:
        err = JsonResponse({'error': after_id_error}, status=400)
        err['Cache-Control'] = 'no-store'
        return err

    session = await sync_to_async(
        _resolve_client_session_for_events, thread_sensitive=True
    )(request)
    if session is None:
        not_found = JsonResponse(
            {'error': 'Активну сесію чату не знайдено.'},
            status=404,
        )
        not_found['Cache-Control'] = 'no-store'
        return not_found

    if not hasattr(request, 'scope'):
        fallback = HttpResponse(
            'Потік подій доступний лише через ASGI.',
            status=503,
            content_type='text/plain; charset=utf-8',
        )
        fallback['Cache-Control'] = 'no-store'
        fallback['X-SSE-Fallback'] = 'polling'
        return fallback

    response = StreamingHttpResponse(
        subscribe_client_session_events(
            session.session_uuid,
            after_id=after_id,
        ),
        content_type='text/event-stream',
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response
