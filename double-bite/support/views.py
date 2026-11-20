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
from django.db import close_old_connections, connections
from django.http import (
    HttpRequest,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.views.decorators.http import require_GET, require_POST
from orders.middleware import get_guest_token

from support.agent.client import MistralSupportAgent
from support.models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    MessageRole,
)
from support.services import ChatSessionService

logger = logging.getLogger('support')

_KIND_ITEM = 0
_KIND_ERR = 1
_KIND_DONE = 2


def _close_non_atomic_connections() -> None:
    if not any(conn.in_atomic_block for conn in connections.all(initialized_only=True)):
        close_old_connections()


def _close_thread_connections() -> None:
    for conn in connections.all(initialized_only=True):
        if not conn.in_atomic_block:
            conn.close()


def _save_message(session_uuid: Any, role: str, content: str) -> ChatMessage:
    _close_non_atomic_connections()
    session = ChatSession.objects.get(session_uuid=session_uuid)
    return ChatMessage.objects.create(
        session=session,
        role=role,
        content=content,
    )


def _get_history(session_uuid: Any) -> list[dict[str, Any]]:
    session = ChatSession.objects.get(session_uuid=session_uuid)
    msgs = list(
        session.messages.filter(
            role__in=[MessageRole.USER, MessageRole.ASSISTANT],
        ).order_by('-created_at', '-id')[:20]
    )
    msgs.reverse()
    return [{'role': m.role, 'content': m.content} for m in msgs]


def _resolve_rate_limit_identity(request: HttpRequest) -> str:
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        return f'user:{user.id}'
    token = get_guest_token(request, create=True)
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
    return JsonResponse({
        'status': 'ok',
        'session_uuid': str(new_session.session_uuid),
        'message': 'Сесію успішно скинуто.',
    })


@require_GET
def chat_history_view(request: HttpRequest) -> JsonResponse:
    if not request.user.is_authenticated and not get_guest_token(request, create=False):
        return JsonResponse({
            'session_uuid': None,
            'is_escalated': False,
            'status': 'ACTIVE',
            'messages': [],
        })

    session = ChatSessionService.get_active_session(request)
    if not session:
        return JsonResponse({
            'session_uuid': None,
            'is_escalated': False,
            'status': 'ACTIVE',
            'messages': [],
        })

    messages = [
        {
            'id': m.id,
            'role': m.role,
            'sender': _resolve_message_sender(m.role),
            'content': m.content,
            'created_at': m.created_at.strftime('%H:%M'),
        }
        for m in session.messages.exclude(role=MessageRole.TOOL).order_by('created_at', 'id')
    ]
    return JsonResponse({
        'session_uuid': str(session.session_uuid),
        'is_escalated': session.is_escalated,
        'status': session.status,
        'messages': messages,
    })
