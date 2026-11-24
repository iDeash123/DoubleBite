import json
import uuid
from dataclasses import replace
from typing import Any

from config.security import consume_rate_limit, rate_limit_429_response
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from support.models import (
    ChatMessage,
    ChatSession,
    MessageRole,
    MessageSource,
    TicketStatus,
)
from support.permissions import support_operator_required
from support.selectors import (
    DeskMessageView,
    format_session_customer_name,
    get_desk_queue,
    get_desk_session_context,
    get_past_session_for_desk,
    normalize_desk_tab,
    resolve_default_tab_for_session,
    serialize_desk_message,
)
from support.services import (
    InvalidMessageError,
    InvalidSessionStateError,
    SessionNotFoundError,
    SessionOwnershipConflictError,
    SupportDeskService,
    get_operator_display_name,
)
from support.views import _parse_after_id, _parse_json_body


def _serialize_message_dict(msg_view: DeskMessageView) -> dict[str, Any]:
    return {
        'id': msg_view.id,
        'role': msg_view.role,
        'sender': msg_view.sender,
        'sender_label': msg_view.sender_label,
        'operator_name': msg_view.operator_name,
        'source': msg_view.source,
        'source_badge': msg_view.source_badge,
        'content': msg_view.content,
        'created_at': msg_view.created_at_str,
    }


def _serialize_action_result(
    session: ChatSession,
    *,
    current_operator: Any,
    ticket: Any = None,
    system_message: ChatMessage | None = None,
) -> dict[str, Any]:
    customer_name = format_session_customer_name(session)
    sys_msg_payload = None
    if system_message is not None:
        sys_msg_payload = _serialize_message_dict(
            serialize_desk_message(system_message, customer_name=customer_name)
        )
    return {
        'status': session.status,
        'session_uuid': str(session.session_uuid),
        'is_escalated': session.is_escalated,
        'assigned_operator_id': session.assigned_operator_id,
        'assigned_operator_name': (
            get_operator_display_name(session.assigned_operator)
            if session.assigned_operator is not None
            else None
        ),
        'is_owned_by_me': bool(
            current_operator is not None
            and session.assigned_operator_id == getattr(current_operator, 'id', None)
        ),
        'ticket_id': ticket.id if ticket is not None else None,
        'ticket_status': ticket.status if ticket is not None else None,
        'system_message': sys_msg_payload,
    }


def _parse_optional_uuid(raw: str | None) -> uuid.UUID | None:
    cleaned = (raw or '').strip()
    if not cleaned:
        return None
    try:
        return uuid.UUID(cleaned)
    except ValueError:
        return None


def _no_store_json(payload: dict[str, Any], *, status: int = 200) -> JsonResponse:
    response = JsonResponse(payload, status=status)
    response['Cache-Control'] = 'no-store'
    return response


@support_operator_required
@require_GET
def desk_index_view(request: HttpRequest) -> HttpResponse:
    tab = normalize_desk_tab(request.GET.get('tab'))
    query = (request.GET.get('q') or '').strip()
    queue_data = get_desk_queue(
        tab=tab,
        query=query,
        current_operator=request.user,
    )

    active_session: ChatSession | None = None
    thread_context: dict[str, Any] | None = None
    if queue_data['items']:
        active_session = queue_data['items'][0].session
        if active_session.assigned_operator_id == request.user.id:
            SupportDeskService.mark_session_read(active_session, request.user)
            queue_data['items'][0] = replace(
                queue_data['items'][0],
                is_active_selected=True,
                unread_count=0,
            )
        else:
            queue_data['items'][0] = replace(
                queue_data['items'][0],
                is_active_selected=True,
            )
        thread_context = get_desk_session_context(
            active_session,
            current_operator=request.user,
        )

    return render(
        request,
        'support/desk/index.html',
        {
            'queue': queue_data,
            'thread': thread_context,
            'active_session_uuid': (
                str(active_session.session_uuid) if active_session is not None else ''
            ),
        },
    )


@support_operator_required
@require_GET
def desk_session_view(request: HttpRequest, session_uuid: uuid.UUID) -> HttpResponse:
    session = get_object_or_404(
        ChatSession.objects.select_related('user', 'assigned_operator'),
        session_uuid=session_uuid,
    )
    if session.assigned_operator_id == request.user.id:
        SupportDeskService.mark_session_read(session, request.user)

    raw_tab = request.GET.get('tab')
    tab = (
        normalize_desk_tab(raw_tab)
        if raw_tab
        else resolve_default_tab_for_session(session)
    )
    query = (request.GET.get('q') or '').strip()

    queue_data = get_desk_queue(
        tab=tab,
        query=query,
        current_operator=request.user,
        active_session_uuid=session.session_uuid,
    )
    thread_context = get_desk_session_context(
        session,
        current_operator=request.user,
    )

    return render(
        request,
        'support/desk/index.html',
        {
            'queue': queue_data,
            'thread': thread_context,
            'active_session_uuid': str(session.session_uuid),
        },
    )


@support_operator_required
@require_GET
def desk_queue_partial_view(request: HttpRequest) -> HttpResponse:
    tab = normalize_desk_tab(request.GET.get('tab'))
    query = (request.GET.get('q') or '').strip()
    active_uuid = _parse_optional_uuid(request.GET.get('active'))

    queue_data = get_desk_queue(
        tab=tab,
        query=query,
        current_operator=request.user,
        active_session_uuid=active_uuid,
    )
    return render(
        request,
        'support/desk/_queue.html',
        {
            'queue': queue_data,
            'active_session_uuid': str(active_uuid) if active_uuid else '',
        },
    )


@support_operator_required
@require_GET
def desk_thread_partial_view(
    request: HttpRequest,
    session_uuid: uuid.UUID,
) -> HttpResponse:
    session = get_object_or_404(
        ChatSession.objects.select_related('user', 'assigned_operator'),
        session_uuid=session_uuid,
    )
    if session.assigned_operator_id == request.user.id:
        SupportDeskService.mark_session_read(session, request.user)

    thread_context = get_desk_session_context(
        session,
        current_operator=request.user,
    )
    return render(
        request,
        'support/desk/_thread.html',
        {
            'thread': thread_context,
        },
    )


@support_operator_required
@require_GET
def desk_past_history_partial_view(
    request: HttpRequest,
    past_session_uuid: uuid.UUID,
) -> HttpResponse:
    current_uuid = _parse_optional_uuid(request.GET.get('session_uuid'))
    if current_uuid is None:
        raise Http404('Поточну сесію не знайдено.')

    past_data = get_past_session_for_desk(
        past_session_uuid,
        current_session_uuid=current_uuid,
    )
    if past_data is None:
        raise Http404('Попереднє звернення не знайдено.')

    return render(
        request,
        'support/desk/_past_session_messages.html',
        past_data,
    )


@support_operator_required
@require_GET
def desk_messages_api_view(
    request: HttpRequest,
    session_uuid: uuid.UUID,
) -> JsonResponse:
    after_id, after_id_error = _parse_after_id(request.GET.get('after_id'))
    if after_id_error is not None:
        return _no_store_json({'error': after_id_error}, status=400)

    session = (
        ChatSession.objects.select_related('user', 'assigned_operator')
        .filter(session_uuid=session_uuid)
        .first()
    )
    if session is None:
        return _no_store_json({'error': 'Сесію чату не знайдено.'}, status=404)

    if session.assigned_operator_id == request.user.id:
        SupportDeskService.mark_session_read(session, request.user)

    visible_qs = session.messages.exclude(role=MessageRole.TOOL).select_related(
        'sender_user'
    )
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

    customer_name = format_session_customer_name(session)
    serialized = [
        _serialize_message_dict(serialize_desk_message(m, customer_name=customer_name))
        for m in msg_objs
    ]
    ticket = (
        session.tickets.filter(
            status__in=(TicketStatus.OPEN, TicketStatus.IN_PROGRESS)
        )
        .order_by('-created_at', '-id')
        .first()
        or session.tickets.order_by('-created_at', '-id').first()
    )
    ticket_payload = (
        {
            'id': ticket.id,
            'status': ticket.status,
            'status_display': ticket.get_status_display(),
            'reason': ticket.reason,
            'reason_display': ticket.get_reason_display(),
        }
        if ticket is not None
        else None
    )

    return _no_store_json({
        'session_uuid': str(session.session_uuid),
        'status': session.status,
        'is_escalated': session.is_escalated,
        'assigned_operator_id': session.assigned_operator_id,
        'assigned_operator_name': (
            get_operator_display_name(session.assigned_operator)
            if session.assigned_operator is not None
            else None
        ),
        'is_owned_by_me': session.assigned_operator_id == request.user.id,
        'ticket': ticket_payload,
        'last_message_id': last_message_id,
        'messages': serialized,
    })


@support_operator_required
@require_POST
def desk_claim_view(
    request: HttpRequest,
    session_uuid: uuid.UUID,
) -> JsonResponse:
    try:
        result = SupportDeskService.claim_session(session_uuid, request.user)
    except SessionNotFoundError as exc:
        return _no_store_json({'error': str(exc)}, status=404)
    except SessionOwnershipConflictError as exc:
        return _no_store_json(
            {
                'error': str(exc),
                'owner_id': exc.owner.id if exc.owner else None,
                'owner_name': (
                    get_operator_display_name(exc.owner) if exc.owner else None
                ),
            },
            status=409,
        )
    except InvalidSessionStateError as exc:
        return _no_store_json({'error': str(exc)}, status=409)

    return _no_store_json(
        _serialize_action_result(
            result.session,
            current_operator=request.user,
            ticket=result.ticket,
            system_message=result.system_message,
        )
    )


@support_operator_required
@require_POST
def desk_takeover_view(
    request: HttpRequest,
    session_uuid: uuid.UUID,
) -> JsonResponse:
    data, error_response = _parse_json_body(request, allow_empty=True)
    if error_response is not None or data is None:
        return error_response or _no_store_json(
            {'error': 'Невірний формат JSON.'}, status=400
        )

    raw_details = data.get('details', '')
    if raw_details is not None and not isinstance(raw_details, str):
        return _no_store_json({'error': 'Невірний формат коментаря.'}, status=400)
    if isinstance(raw_details, str) and '\x00' in raw_details:
        return _no_store_json({'error': 'Коментар містить неприпустимі символи.'}, status=400)
    details = raw_details.strip()[:1000] if isinstance(raw_details, str) else ''

    try:
        result = SupportDeskService.takeover_session(
            session_uuid,
            request.user,
            allow_proactive=True,
            details=details,
        )
    except SessionNotFoundError as exc:
        return _no_store_json({'error': str(exc)}, status=404)
    except InvalidSessionStateError as exc:
        return _no_store_json({'error': str(exc)}, status=409)

    return _no_store_json(
        _serialize_action_result(
            result.session,
            current_operator=request.user,
            ticket=result.ticket,
            system_message=result.system_message,
        )
    )


@support_operator_required
@require_POST
def desk_reply_view(
    request: HttpRequest,
    session_uuid: uuid.UUID,
) -> HttpResponse:
    limited, retry_after = consume_rate_limit(
        'desk_reply_user',
        f'user:{request.user.id}',
        60,
        60,
    )
    if limited:
        return rate_limit_429_response(
            request,
            retry_after,
            'Забагато повідомлень. Ліміт: 60 відповідей на хвилину.',
            json_body=True,
        )

    content_type = (request.content_type or '').lower()
    if content_type in (
        'application/x-www-form-urlencoded',
        'multipart/form-data',
    ):
        raw_message = request.POST.get('message')
    else:
        raw_body = request.body
        if not raw_body or not raw_body.strip():
            return _no_store_json({'error': 'Повідомлення не може бути порожнім.'}, status=400)
        try:
            parsed = json.loads(raw_body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _no_store_json({'error': 'Невірний формат JSON.'}, status=400)
        if not isinstance(parsed, dict):
            return _no_store_json({'error': 'Невірний формат JSON.'}, status=400)
        raw_message = parsed.get('message')

    try:
        msg = SupportDeskService.send_operator_message(
            session_uuid,
            request.user,
            raw_message,
            source=MessageSource.WEB,
        )
    except InvalidMessageError as exc:
        return _no_store_json({'error': str(exc)}, status=400)
    except SessionNotFoundError as exc:
        return _no_store_json({'error': str(exc)}, status=404)
    except SessionOwnershipConflictError as exc:
        return _no_store_json(
            {
                'error': str(exc),
                'owner_id': exc.owner.id if exc.owner else None,
                'owner_name': (
                    get_operator_display_name(exc.owner) if exc.owner else None
                ),
            },
            status=409,
        )
    except InvalidSessionStateError as exc:
        return _no_store_json({'error': str(exc)}, status=409)

    customer_name = format_session_customer_name(msg.session)
    return _no_store_json(
        {
            'status': 'ok',
            'session_uuid': str(msg.session_id),
            'message': _serialize_message_dict(
                serialize_desk_message(msg, customer_name=customer_name)
            ),
        },
        status=201,
    )


@support_operator_required
@require_POST
def desk_return_ai_view(
    request: HttpRequest,
    session_uuid: uuid.UUID,
) -> JsonResponse:
    try:
        result = SupportDeskService.return_to_ai(
            session_uuid,
            request.user,
            source=MessageSource.WEB,
        )
    except SessionNotFoundError as exc:
        return _no_store_json({'error': str(exc)}, status=404)
    except InvalidSessionStateError as exc:
        return _no_store_json({'error': str(exc)}, status=409)

    return _no_store_json(
        _serialize_action_result(
            result.session,
            current_operator=request.user,
            ticket=result.ticket,
            system_message=result.system_message,
        )
    )


@support_operator_required
@require_POST
def desk_resolve_view(
    request: HttpRequest,
    session_uuid: uuid.UUID,
) -> JsonResponse:
    try:
        result = SupportDeskService.resolve_session(
            session_uuid,
            request.user,
            source=MessageSource.WEB,
        )
    except SessionNotFoundError as exc:
        return _no_store_json({'error': str(exc)}, status=404)
    except InvalidSessionStateError as exc:
        return _no_store_json({'error': str(exc)}, status=409)

    return _no_store_json(
        _serialize_action_result(
            result.session,
            current_operator=request.user,
            ticket=result.ticket,
            system_message=result.system_message,
        )
    )
