import json
import logging
from typing import Any, AsyncGenerator

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.http import (
    HttpRequest,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.views.decorators.http import require_GET, require_POST
from support.agent.client import MistralSupportAgent
from support.models import ChatMessage, ChatSession, MessageRole
from support.services import ChatSessionService

logger = logging.getLogger('support')


def _save_message(session_uuid: Any, role: str, content: str) -> ChatMessage:
    session = ChatSession.objects.get(session_uuid=session_uuid)
    return ChatMessage.objects.create(
        session=session,
        role=role,
        content=content,
    )


def _get_history(session_uuid: Any) -> list[dict[str, Any]]:
    session = ChatSession.objects.get(session_uuid=session_uuid)
    msgs = session.messages.order_by('created_at')[:20]
    return [{'role': m.role, 'content': m.content} for m in msgs]


async def chat_stream_view(request: HttpRequest) -> HttpResponse:
    if request.method != 'POST':
        return HttpResponse('Method Not Allowed', status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'error': 'Невірний формат JSON.'}, status=400)

    user_text = str(data.get('message', '')).strip()
    if not user_text:
        return JsonResponse({'error': 'Повідомлення не може бути порожнім.'}, status=400)

    if len(user_text) > 2000:
        user_text = user_text[:2000]

    session_id = request.session.session_key
    if not session_id:
        await sync_to_async(request.session.save, thread_sensitive=True)()
        session_id = request.session.session_key or 'unknown'

    cache_key = f"chat_rate:{session_id}"
    rate_count = await sync_to_async(cache.get, thread_sensitive=True)(cache_key, 0)
    if rate_count >= 10:
        return JsonResponse(
            {'error': 'Забагато запитів. Ліміт: 10 повідомлень на хвилину.'},
            status=429,
        )
    await sync_to_async(cache.set, thread_sensitive=True)(cache_key, rate_count + 1, timeout=60)

    session = await sync_to_async(
        ChatSessionService.get_or_create_active_session, thread_sensitive=True
    )(request)
    session_uuid = session.session_uuid

    await sync_to_async(_save_message, thread_sensitive=True)(
        session_uuid, MessageRole.USER, user_text
    )

    history = await sync_to_async(_get_history, thread_sensitive=True)(session_uuid)
    agent = MistralSupportAgent()

    async def event_generator() -> AsyncGenerator[str, None]:
        assistant_chunks: list[str] = []
        try:
            async for event in agent.stream_chat_response(
                history, request=request, session=session
            ):
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
        except Exception as exc:
            logger.error('Stream error: %s', exc, exc_info=True)
            yield f"data: {json.dumps({'error': 'Виникла помилка під час формування відповіді.'}, ensure_ascii=False)}\n\n"

    response = StreamingHttpResponse(
        event_generator(),
        content_type='text/event-stream',
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


@require_POST
def chat_reset_view(request: HttpRequest) -> JsonResponse:
    new_session = ChatSessionService.reset_active_session(request)
    return JsonResponse({
        'status': 'ok',
        'session_uuid': str(new_session.session_uuid),
        'message': 'Сесію успішно скинуто.',
    })


@require_GET
def chat_history_view(request: HttpRequest) -> JsonResponse:
    session = ChatSessionService.get_or_create_active_session(request)
    messages = [
        {
            'id': m.id,
            'role': m.role,
            'content': m.content,
            'created_at': m.created_at.strftime('%H:%M'),
        }
        for m in session.messages.exclude(role=MessageRole.TOOL).order_by('created_at')
    ]
    return JsonResponse({
        'session_uuid': str(session.session_uuid),
        'is_escalated': session.is_escalated,
        'status': session.status,
        'messages': messages,
    })
