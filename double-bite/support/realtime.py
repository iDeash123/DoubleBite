import asyncio
import contextlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import redis
import redis.asyncio as aioredis
from asgiref.sync import sync_to_async
from config.constants import (
    DEFAULT_SUPPORT_WORK_END_HOUR,
    DEFAULT_SUPPORT_WORK_START_HOUR,
)
from django.conf import settings
from django.db import close_old_connections, connections
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone
from redis.exceptions import RedisError

from support.models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    MessageRole,
    MessageSource,
    SupportTicket,
    TicketStatus,
)

logger = logging.getLogger('support')

DESK_QUEUE_CHANNEL = 'support:desk:queue'
DEFAULT_HEARTBEAT_INTERVAL = 15.0
DEFAULT_DB_POLL_INTERVAL = 1.5
DEFAULT_SSE_RETRY_MS = 3000

OPERATOR_PUBLIC_LABEL = 'Адміністратор ресторану'
CLIENT_AI_LABEL = 'Double Bite Консьєрж'
DESK_AI_LABEL = 'AI-консьєрж'
SYSTEM_LABEL = 'Система'

SESSION_EVENT_TYPES = frozenset({
    'message',
    'operator_joined',
    'status_changed',
    'session_resolved',
})
DESK_EVENT_TYPES = frozenset({
    'message',
    'operator_joined',
    'status_changed',
    'session_resolved',
    'queue_update',
})

_SYNC_CLIENT_LOCK = threading.Lock()
_SYNC_REDIS_CLIENTS: dict[str, redis.Redis] = {}


def session_channel(session_uuid: uuid.UUID | str) -> str:
    return f'support:session:{session_uuid}'


def _close_non_atomic_connections() -> None:
    if not any(conn.in_atomic_block for conn in connections.all(initialized_only=True)):
        close_old_connections()


def get_realtime_redis_url() -> str:
    """Resolve Redis URL for pub/sub without touching external Redis in default unit tests."""
    explicit = getattr(settings, 'SUPPORT_REALTIME_REDIS_URL', None)
    if explicit is not None:
        return str(explicit).strip() if explicit else ''

    settings_redis_url = getattr(settings, 'REDIS_URL', None)
    if settings_redis_url is not None:
        return str(settings_redis_url).strip() if settings_redis_url else ''

    default_cache = (getattr(settings, 'CACHES', None) or {}).get('default', {})
    backend = str(default_cache.get('BACKEND', '')).lower()
    if 'redis' in backend:
        location = default_cache.get('LOCATION', '')
        if isinstance(location, (list, tuple)):
            for candidate in location:
                cleaned = str(candidate or '').strip()
                if cleaned.startswith(('redis://', 'rediss://', 'unix://')):
                    return cleaned
        elif isinstance(location, str):
            cleaned = location.strip()
            if cleaned.startswith(('redis://', 'rediss://', 'unix://')):
                return cleaned

    if getattr(settings, 'IS_TESTING', False):
        return ''

    return os.getenv('REDIS_URL', '').strip()


def _get_sync_redis_client(redis_url: str) -> redis.Redis:
    with _SYNC_CLIENT_LOCK:
        client = _SYNC_REDIS_CLIENTS.get(redis_url)
        if client is None:
            client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
            _SYNC_REDIS_CLIENTS[redis_url] = client
        return client


def _create_async_redis_client(redis_url: str) -> aioredis.Redis:
    return aioredis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        health_check_interval=30,
    )


def reset_realtime_clients() -> None:
    with _SYNC_CLIENT_LOCK:
        clients = list(_SYNC_REDIS_CLIENTS.values())
        _SYNC_REDIS_CLIENTS.clear()
    for client in clients:
        with contextlib.suppress(Exception):
            client.close()


def _resolve_sender_kind(role: str) -> str:
    if role == MessageRole.OPERATOR:
        return 'operator'
    if role == MessageRole.ASSISTANT:
        return 'ai'
    if role == MessageRole.SYSTEM:
        return 'system'
    if role == MessageRole.USER:
        return 'user'
    return 'ai'


def _format_user_display_name(user: Any) -> str:
    if user is None:
        return OPERATOR_PUBLIC_LABEL
    get_full_name = getattr(user, 'get_full_name', None)
    full_name = str(get_full_name() or '').strip() if callable(get_full_name) else ''
    if full_name:
        return full_name
    first_name = str(getattr(user, 'first_name', '') or '').strip()
    last_name = str(getattr(user, 'last_name', '') or '').strip()
    combined = f'{first_name} {last_name}'.strip()
    if combined:
        return combined
    return str(getattr(user, 'email', '') or OPERATOR_PUBLIC_LABEL)


def _format_customer_name(session: ChatSession) -> str:
    if session.user_id is not None and session.user is not None:
        full_name = str(session.user.get_full_name() or '').strip()
        return full_name or session.user.email
    token_preview = session.guest_token[:8] if session.guest_token else 'anon'
    return f'Гість ({token_preview})'


def _working_hours_snapshot() -> dict[str, Any]:
    from zoneinfo import ZoneInfo

    tz_name = getattr(settings, 'TIME_ZONE', 'Europe/Kyiv') or 'Europe/Kyiv'
    tz = ZoneInfo(tz_name)
    current = timezone.now()
    if timezone.is_naive(current):
        local_now = current.replace(tzinfo=tz)
    else:
        local_now = current.astimezone(tz)
    start_hour = int(
        getattr(settings, 'SUPPORT_WORK_START_HOUR', DEFAULT_SUPPORT_WORK_START_HOUR)
    )
    end_hour = int(
        getattr(settings, 'SUPPORT_WORK_END_HOUR', DEFAULT_SUPPORT_WORK_END_HOUR)
    )
    label = f'{start_hour:02d}:00–{end_hour:02d}:00'
    notice = (
        f'Зараз неробочий час ({label}). Ваше звернення збережено — '
        f'залиште деталі, і адміністратор відповість з {start_hour:02d}:00.'
    )
    return {
        'is_working_hours': start_hour <= local_now.hour < end_hour,
        'working_hours_label': label,
        'off_hours_notice': notice,
    }


def _is_operator_connected_sync(session: ChatSession) -> bool:
    if session.status != ChatSessionStatus.ESCALATED:
        return False
    if session.assigned_operator_id is not None:
        return True
    return session.tickets.filter(status=TicketStatus.IN_PROGRESS).exists()


def build_message_event_data(
    msg: ChatMessage,
    *,
    session: ChatSession | None = None,
    customer_name: str | None = None,
    operator_name: str | None = None,
) -> dict[str, Any]:
    created_local = (
        timezone.localtime(msg.created_at)
        if timezone.is_aware(msg.created_at)
        else msg.created_at
    )
    created_str = created_local.strftime('%H:%M') if created_local else ''
    sender = _resolve_sender_kind(msg.role)
    resolved_customer = customer_name
    if resolved_customer is None and session is not None:
        resolved_customer = _format_customer_name(session)

    payload: dict[str, Any] = {
        'id': msg.id,
        'role': msg.role,
        'sender': sender,
        'source': msg.source,
        'content': msg.content,
        'created_at': created_str,
    }
    if msg.role == MessageRole.OPERATOR:
        resolved_op_name = operator_name
        if resolved_op_name is None:
            resolved_op_name = (
                _format_user_display_name(msg.sender_user)
                if msg.sender_user_id is not None and msg.sender_user is not None
                else OPERATOR_PUBLIC_LABEL
            )
        payload['sender_label'] = OPERATOR_PUBLIC_LABEL
        payload['operator_name'] = resolved_op_name
        payload['source_badge'] = (
            'TG' if msg.source == MessageSource.TELEGRAM else 'Web'
        )
    elif msg.role == MessageRole.USER:
        payload['sender_label'] = resolved_customer or 'Клієнт'
        payload['customer_name'] = resolved_customer or 'Клієнт'
    elif msg.role == MessageRole.SYSTEM:
        payload['sender_label'] = SYSTEM_LABEL
    else:
        payload['sender_label'] = DESK_AI_LABEL

    return payload


def build_status_event_data(
    session: ChatSession,
    *,
    operator: Any = None,
    ticket: SupportTicket | None = None,
    operator_connected: bool | None = None,
    reason: str = '',
    working_hours: dict[str, Any] | None = None,
) -> dict[str, Any]:
    wh = working_hours if working_hours is not None else _working_hours_snapshot()
    resolved_operator = (
        operator
        if operator is not None
        else (
            session.assigned_operator
            if session.assigned_operator_id is not None
            else None
        )
    )
    connected = (
        operator_connected
        if operator_connected is not None
        else (
            session.status == ChatSessionStatus.ESCALATED
            and (
                session.assigned_operator_id is not None
                or (ticket is not None and ticket.status == TicketStatus.IN_PROGRESS)
            )
        )
    )
    data: dict[str, Any] = {
        'status': session.status,
        'is_escalated': bool(session.is_escalated),
        'operator_connected': bool(connected),
        'assigned_operator_id': session.assigned_operator_id,
        'assigned_operator_name': (
            _format_user_display_name(resolved_operator)
            if resolved_operator is not None
            else None
        ),
        'ticket_id': ticket.id if ticket is not None else None,
        'ticket_status': ticket.status if ticket is not None else None,
        'is_working_hours': wh['is_working_hours'],
        'working_hours_label': wh['working_hours_label'],
        'off_hours_notice': wh['off_hours_notice'],
    }
    if reason:
        data['reason'] = reason
    return data


def sanitize_event_for_client(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip all operator PII / internal desk metadata before sending to customer SSE."""
    ev_type = str(payload.get('type') or '')
    session_uuid = payload.get('session_uuid')
    event_id = payload.get('event_id')
    raw_data = payload.get('data') if isinstance(payload.get('data'), dict) else {}

    if ev_type == 'message':
        role = raw_data.get('role', '')
        sender = raw_data.get('sender') or _resolve_sender_kind(str(role))
        if sender == 'operator':
            sender_label = OPERATOR_PUBLIC_LABEL
        elif sender == 'user':
            sender_label = 'Ви'
        elif sender == 'system':
            sender_label = SYSTEM_LABEL
        else:
            sender_label = CLIENT_AI_LABEL
        clean_data: dict[str, Any] = {
            'id': raw_data.get('id'),
            'role': role,
            'sender': sender,
            'sender_label': sender_label,
            'source': raw_data.get('source', MessageSource.WEB),
            'content': raw_data.get('content', ''),
            'created_at': raw_data.get('created_at', ''),
        }
    elif ev_type == 'operator_joined':
        clean_data = {
            'status': raw_data.get('status', ChatSessionStatus.ESCALATED),
            'is_escalated': True,
            'operator_connected': True,
        }
        for key in ('is_working_hours', 'working_hours_label', 'off_hours_notice'):
            if key in raw_data:
                clean_data[key] = raw_data[key]
    elif ev_type == 'session_resolved':
        clean_data = {
            'status': ChatSessionStatus.RESOLVED,
            'is_escalated': False,
            'operator_connected': False,
        }
        if raw_data.get('reason'):
            clean_data['reason'] = raw_data['reason']
    else:
        clean_data = {
            'status': raw_data.get('status', ChatSessionStatus.ACTIVE),
            'is_escalated': bool(raw_data.get('is_escalated', False)),
            'operator_connected': bool(raw_data.get('operator_connected', False)),
        }
        for key in (
            'ticket_id',
            'is_working_hours',
            'working_hours_label',
            'off_hours_notice',
            'reason',
        ):
            if key in raw_data and raw_data[key] is not None:
                clean_data[key] = raw_data[key]

    return {
        'type': ev_type,
        'session_uuid': str(session_uuid) if session_uuid is not None else None,
        'event_id': event_id,
        'data': clean_data,
    }


def format_event_for_desk(payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure desk SSE payload includes operator attribution and source badges."""
    ev_type = str(payload.get('type') or '')
    session_uuid = payload.get('session_uuid')
    event_id = payload.get('event_id')
    raw_data = dict(payload.get('data')) if isinstance(payload.get('data'), dict) else {}

    if ev_type == 'message':
        role = raw_data.get('role', '')
        sender = raw_data.get('sender') or _resolve_sender_kind(str(role))
        raw_data['sender'] = sender
        if sender == 'operator':
            raw_data.setdefault('sender_label', OPERATOR_PUBLIC_LABEL)
            raw_data.setdefault('operator_name', OPERATOR_PUBLIC_LABEL)
            if not raw_data.get('source_badge'):
                raw_data['source_badge'] = (
                    'TG' if raw_data.get('source') == MessageSource.TELEGRAM else 'Web'
                )
        elif sender == 'user':
            raw_data['sender_label'] = (
                raw_data.get('customer_name')
                or raw_data.get('sender_label')
                or 'Клієнт'
            )
        elif sender == 'system':
            raw_data['sender_label'] = SYSTEM_LABEL
        else:
            raw_data['sender_label'] = DESK_AI_LABEL

    return {
        'type': ev_type,
        'session_uuid': str(session_uuid) if session_uuid is not None else None,
        'event_id': event_id,
        'data': raw_data,
    }


def format_sse_event(payload: dict[str, Any]) -> str:
    ev_type = str(payload.get('type') or 'message').replace('\r', '').replace('\n', '')
    event_id = payload.get('event_id')
    lines: list[str] = []
    # Only emit SSE `id:` for positive integer message IDs so browser Last-Event-ID
    # remains strictly aligned with ChatMessage.id across reconnects.
    if (
        ev_type == 'message'
        and isinstance(event_id, int)
        and not isinstance(event_id, bool)
        and event_id > 0
    ):
        lines.append(f'id: {event_id}')
    lines.append(f'event: {ev_type}')
    serialized = json.dumps(payload, ensure_ascii=False)
    lines.append(f'data: {serialized}')
    return '\n'.join(lines) + '\n\n'


def _publish_to_channel(channel: str, payload: dict[str, Any]) -> bool:
    redis_url = get_realtime_redis_url()
    if not redis_url:
        return False
    raw = json.dumps(payload, ensure_ascii=False)
    try:
        client = _get_sync_redis_client(redis_url)
        client.publish(channel, raw)
        return True
    except (RedisError, OSError) as exc:
        logger.warning(
            'Redis publish failed for channel %s (%s): %s',
            channel,
            payload.get('type'),
            exc,
        )
        return False


def publish_session_event(
    session_uuid: uuid.UUID | str,
    event_type: str,
    data: dict[str, Any],
    *,
    event_id: int | None = None,
) -> bool:
    payload = {
        'type': event_type,
        'session_uuid': str(session_uuid),
        'event_id': event_id,
        'data': data,
    }
    return _publish_to_channel(session_channel(session_uuid), payload)


def publish_desk_event(
    event_type: str,
    data: dict[str, Any],
    *,
    session_uuid: uuid.UUID | str | None = None,
    event_id: int | None = None,
) -> bool:
    payload = {
        'type': event_type,
        'session_uuid': str(session_uuid) if session_uuid is not None else None,
        'event_id': event_id,
        'data': data,
    }
    return _publish_to_channel(DESK_QUEUE_CHANNEL, payload)


def _fetch_session_snapshot_sync(
    session_uuid: uuid.UUID,
    *,
    after_id: int | None,
    include_messages: bool,
) -> dict[str, Any] | None:
    _close_non_atomic_connections()
    try:
        session = (
            ChatSession.objects.select_related('user', 'assigned_operator')
            .filter(session_uuid=session_uuid)
            .first()
        )
        if session is None:
            return None

        visible_qs = session.messages.exclude(role=MessageRole.TOOL).select_related(
            'sender_user'
        )
        customer_name = _format_customer_name(session)
        messages_data: list[dict[str, Any]] = []
        if include_messages and after_id is not None:
            msg_objs = list(
                visible_qs.filter(id__gt=after_id).order_by('created_at', 'id')
            )
            messages_data = [
                build_message_event_data(
                    m, session=session, customer_name=customer_name
                )
                for m in msg_objs
            ]
            latest_msg_id = (
                max(m.id for m in msg_objs)
                if msg_objs
                else (
                    visible_qs.order_by('-id').values_list('id', flat=True).first()
                    or after_id
                )
            )
        else:
            latest_msg_id = (
                visible_qs.order_by('-id').values_list('id', flat=True).first() or 0
            )

        ticket = (
            session.tickets.filter(
                status__in=(TicketStatus.OPEN, TicketStatus.IN_PROGRESS)
            )
            .order_by('-created_at', '-id')
            .first()
        )
        operator_connected = _is_operator_connected_sync(session)
        status_data = build_status_event_data(
            session,
            ticket=ticket,
            operator_connected=operator_connected,
        )
        return {
            'session_uuid': str(session.session_uuid),
            'status': session.status,
            'is_escalated': bool(session.is_escalated),
            'operator_connected': operator_connected,
            'assigned_operator_id': session.assigned_operator_id,
            'status_data': status_data,
            'messages': messages_data,
            'latest_msg_id': int(latest_msg_id or 0),
        }
    finally:
        _close_non_atomic_connections()


def _fetch_desk_queue_fingerprint_sync() -> tuple[tuple[Any, ...], dict[str, int]]:
    _close_non_atomic_connections()
    try:
        agg = ChatSession.objects.aggregate(
            waiting=Count(
                'pk',
                filter=Q(
                    status=ChatSessionStatus.ESCALATED,
                    assigned_operator__isnull=True,
                ),
            ),
            in_progress=Count(
                'pk',
                filter=Q(
                    status=ChatSessionStatus.ESCALATED,
                    assigned_operator__isnull=False,
                ),
            ),
            ai=Count('pk', filter=Q(status=ChatSessionStatus.ACTIVE)),
            resolved=Count('pk', filter=Q(status=ChatSessionStatus.RESOLVED)),
            max_updated=Max('updated_at'),
            max_assigned=Max('assigned_at'),
            max_last_msg=Max('last_message_at'),
            sum_read=Sum('operator_last_read_id'),
        )
        msg_agg = ChatMessage.objects.exclude(role=MessageRole.TOOL).aggregate(
            max_msg_id=Max('id'),
        )
        tab_counts = {
            'waiting': int(agg.get('waiting') or 0),
            'in_progress': int(agg.get('in_progress') or 0),
            'ai': int(agg.get('ai') or 0),
            'resolved': int(agg.get('resolved') or 0),
        }
        fingerprint = (
            tab_counts['waiting'],
            tab_counts['in_progress'],
            tab_counts['ai'],
            tab_counts['resolved'],
            agg.get('max_updated'),
            agg.get('max_assigned'),
            agg.get('max_last_msg'),
            int(agg.get('sum_read') or 0),
            int(msg_agg.get('max_msg_id') or 0),
        )
        return fingerprint, tab_counts
    finally:
        _close_non_atomic_connections()


async def _close_redis_resources(
    pubsub: Any,
    redis_client: Any,
) -> None:
    if pubsub is not None:
        with contextlib.suppress(Exception):
            unsub = getattr(pubsub, 'unsubscribe', None)
            if callable(unsub):
                await unsub()
        with contextlib.suppress(Exception):
            aclose = getattr(pubsub, 'aclose', None)
            if callable(aclose):
                await aclose()
            else:
                close = getattr(pubsub, 'close', None)
                if callable(close):
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
    if redis_client is not None:
        with contextlib.suppress(Exception):
            aclose = getattr(redis_client, 'aclose', None)
            if callable(aclose):
                await aclose()
            else:
                close = getattr(redis_client, 'close', None)
                if callable(close):
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res


async def _open_redis_pubsub(
    channels: list[str],
) -> tuple[Any, Any]:
    redis_url = get_realtime_redis_url()
    if not redis_url:
        return None, None
    redis_client: Any = None
    pubsub: Any = None
    try:
        redis_client = _create_async_redis_client(redis_url)
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(*channels)
        return redis_client, pubsub
    except (RedisError, OSError, TimeoutError) as exc:
        logger.warning(
            'Redis subscribe unavailable for %s, falling back to DB polling: %s',
            channels,
            exc,
        )
        await _close_redis_resources(pubsub, redis_client)
        return None, None


def _decode_pubsub_payload(raw_message: Any) -> dict[str, Any] | None:
    if not isinstance(raw_message, dict):
        return None
    if raw_message.get('type') not in ('message', 'pmessage'):
        return None
    raw_data = raw_message.get('data')
    if isinstance(raw_data, bytes):
        try:
            raw_data = raw_data.decode('utf-8')
        except UnicodeDecodeError:
            return None
    if not isinstance(raw_data, str):
        return None
    try:
        parsed = json.loads(raw_data)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class _MessageDeduplicator:
    """Bounded message ID deduplicator that tolerates out-of-order concurrent commits."""

    def __init__(self, initial_cursor: int = 0, *, max_tracked: int = 2048) -> None:
        self.floor_id = max(0, int(initial_cursor))
        self.max_id = self.floor_id
        self._max_tracked = max_tracked
        self._seen: set[int] = set()

    def should_deliver(self, msg_id: int) -> bool:
        if msg_id <= 0:
            return True
        if msg_id <= self.floor_id or msg_id in self._seen:
            return False
        self._seen.add(msg_id)
        if msg_id > self.max_id:
            self.max_id = msg_id
        if len(self._seen) > self._max_tracked:
            sorted_ids = sorted(self._seen)
            trim_count = len(sorted_ids) // 2
            self.floor_id = max(self.floor_id, sorted_ids[trim_count - 1])
            self._seen = set(sorted_ids[trim_count:])
        return True


async def subscribe_client_session_events(
    session_uuid: uuid.UUID,
    *,
    after_id: int | None = None,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    poll_interval: float = DEFAULT_DB_POLL_INTERVAL,
) -> AsyncGenerator[str, None]:
    """Async SSE generator for a single client chat session (Redis Pub/Sub + DB fallback)."""
    channel = session_channel(session_uuid)
    session_uuid_str = str(session_uuid)
    redis_client: Any = None
    pubsub: Any = None

    try:
        # Subscribe BEFORE reading DB catch-up and BEFORE yielding `retry:` so
        # no event can be missed once EventSource.onopen fires on the client.
        redis_client, pubsub = await _open_redis_pubsub([channel])

        snapshot = await sync_to_async(
            _fetch_session_snapshot_sync, thread_sensitive=True
        )(
            session_uuid,
            after_id=after_id,
            include_messages=(after_id is not None),
        )
        yield f'retry: {DEFAULT_SSE_RETRY_MS}\n\n'
        last_activity = time.monotonic()
        if snapshot is None:
            resolved_payload = sanitize_event_for_client({
                'type': 'session_resolved',
                'session_uuid': session_uuid_str,
                'event_id': None,
                'data': {
                    'status': ChatSessionStatus.RESOLVED,
                    'is_escalated': False,
                    'operator_connected': False,
                    'reason': 'not_found',
                },
            })
            yield format_sse_event(resolved_payload)
            return

        last_msg_id = int(snapshot['latest_msg_id'])
        if after_id is not None:
            last_msg_id = max(last_msg_id, after_id)
        last_status = snapshot['status']
        last_is_escalated = snapshot['is_escalated']
        last_operator_connected = snapshot['operator_connected']

        for msg_data in snapshot['messages']:
            msg_id = int(msg_data.get('id') or 0)
            last_msg_id = max(last_msg_id, msg_id)
            ev = sanitize_event_for_client({
                'type': 'message',
                'session_uuid': session_uuid_str,
                'event_id': msg_id,
                'data': msg_data,
            })
            yield format_sse_event(ev)
            last_activity = time.monotonic()

        dedup = _MessageDeduplicator(last_msg_id)

        if last_status == ChatSessionStatus.RESOLVED:
            resolved_ev = sanitize_event_for_client({
                'type': 'session_resolved',
                'session_uuid': session_uuid_str,
                'event_id': None,
                'data': {
                    'status': ChatSessionStatus.RESOLVED,
                    'is_escalated': False,
                    'operator_connected': False,
                    'reason': 'resolved',
                },
            })
            yield format_sse_event(resolved_ev)
            return

        if after_id is not None:
            status_ev = sanitize_event_for_client({
                'type': 'status_changed',
                'session_uuid': session_uuid_str,
                'event_id': None,
                'data': snapshot['status_data'],
            })
            yield format_sse_event(status_ev)
            last_activity = time.monotonic()

        # Primary mode: Redis Pub/Sub
        redis_used = pubsub is not None
        while pubsub is not None:
            elapsed = time.monotonic() - last_activity
            remaining = max(0.01, heartbeat_interval - elapsed)
            wait_timeout = min(remaining, 1.0)
            try:
                raw_msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=wait_timeout,
                )
            except (RedisError, OSError, TimeoutError) as exc:
                logger.warning(
                    'Redis pubsub lost for session %s, switching to DB polling: %s',
                    session_uuid_str,
                    exc,
                )
                await _close_redis_resources(pubsub, redis_client)
                pubsub = None
                redis_client = None
                break

            if raw_msg is not None:
                payload = _decode_pubsub_payload(raw_msg)
                if (
                    payload is not None
                    and payload.get('session_uuid') == session_uuid_str
                    and payload.get('type') in SESSION_EVENT_TYPES
                ):
                    ev_type = str(payload['type'])
                    ev_data = (
                        payload.get('data')
                        if isinstance(payload.get('data'), dict)
                        else {}
                    )
                    if ev_type == 'message':
                        if ev_data.get('role') == MessageRole.TOOL:
                            continue
                        msg_id = int(ev_data.get('id') or 0)
                        if not dedup.should_deliver(msg_id):
                            continue
                    elif ev_type == 'operator_joined':
                        last_status = str(
                            ev_data.get('status') or ChatSessionStatus.ESCALATED
                        )
                        last_is_escalated = True
                        last_operator_connected = True
                    elif ev_type == 'status_changed':
                        if ev_data.get('status'):
                            last_status = str(ev_data['status'])
                        if 'is_escalated' in ev_data:
                            last_is_escalated = bool(ev_data['is_escalated'])
                        if 'operator_connected' in ev_data:
                            last_operator_connected = bool(
                                ev_data['operator_connected']
                            )
                    elif ev_type == 'session_resolved':
                        last_status = ChatSessionStatus.RESOLVED
                        last_is_escalated = False
                        last_operator_connected = False

                    sanitized = sanitize_event_for_client(payload)
                    yield format_sse_event(sanitized)
                    last_activity = time.monotonic()

                    if ev_type == 'session_resolved':
                        return

            if time.monotonic() - last_activity >= heartbeat_interval:
                yield ': ping\n\n'
                last_activity = time.monotonic()

        # Fallback mode: DB polling (no Redis or Redis disconnected mid-stream)
        poll_cursor = dedup.floor_id if redis_used else dedup.max_id
        while True:
            elapsed = time.monotonic() - last_activity
            sleep_for = min(poll_interval, max(0.01, heartbeat_interval - elapsed))
            await asyncio.sleep(sleep_for)

            polled = await sync_to_async(
                _fetch_session_snapshot_sync, thread_sensitive=True
            )(
                session_uuid,
                after_id=poll_cursor,
                include_messages=True,
            )
            if polled is None:
                resolved_ev = sanitize_event_for_client({
                    'type': 'session_resolved',
                    'session_uuid': session_uuid_str,
                    'event_id': None,
                    'data': {
                        'status': ChatSessionStatus.RESOLVED,
                        'is_escalated': False,
                        'operator_connected': False,
                        'reason': 'not_found',
                    },
                })
                yield format_sse_event(resolved_ev)
                return

            emitted = False
            for msg_data in polled['messages']:
                msg_id = int(msg_data.get('id') or 0)
                if not dedup.should_deliver(msg_id):
                    continue
                ev = sanitize_event_for_client({
                    'type': 'message',
                    'session_uuid': session_uuid_str,
                    'event_id': msg_id,
                    'data': msg_data,
                })
                yield format_sse_event(ev)
                last_activity = time.monotonic()
                emitted = True
            poll_cursor = dedup.max_id

            cur_status = polled['status']
            cur_escalated = polled['is_escalated']
            cur_connected = polled['operator_connected']

            if cur_status == ChatSessionStatus.RESOLVED and last_status != ChatSessionStatus.RESOLVED:
                resolved_ev = sanitize_event_for_client({
                    'type': 'session_resolved',
                    'session_uuid': session_uuid_str,
                    'event_id': None,
                    'data': {
                        'status': ChatSessionStatus.RESOLVED,
                        'is_escalated': False,
                        'operator_connected': False,
                        'reason': 'resolved',
                    },
                })
                yield format_sse_event(resolved_ev)
                return

            if (
                cur_status == ChatSessionStatus.ESCALATED
                and cur_connected
                and (not last_operator_connected or last_status != ChatSessionStatus.ESCALATED)
            ):
                joined_ev = sanitize_event_for_client({
                    'type': 'operator_joined',
                    'session_uuid': session_uuid_str,
                    'event_id': None,
                    'data': polled['status_data'],
                })
                yield format_sse_event(joined_ev)
                last_activity = time.monotonic()
                emitted = True
            elif (cur_status, cur_escalated, cur_connected) != (
                last_status,
                last_is_escalated,
                last_operator_connected,
            ):
                status_ev = sanitize_event_for_client({
                    'type': 'status_changed',
                    'session_uuid': session_uuid_str,
                    'event_id': None,
                    'data': polled['status_data'],
                })
                yield format_sse_event(status_ev)
                last_activity = time.monotonic()
                emitted = True

            last_status = cur_status
            last_is_escalated = cur_escalated
            last_operator_connected = cur_connected

            if not emitted and time.monotonic() - last_activity >= heartbeat_interval:
                yield ': ping\n\n'
                last_activity = time.monotonic()

    except asyncio.CancelledError:
        logger.info('Client SSE stream cancelled (session=%s)', session_uuid_str)
        raise
    finally:
        try:
            await _close_redis_resources(pubsub, redis_client)
        finally:
            await sync_to_async(
                _close_non_atomic_connections, thread_sensitive=True
            )()


async def subscribe_desk_events(
    *,
    session_uuid: uuid.UUID | None = None,
    after_id: int | None = None,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    poll_interval: float = DEFAULT_DB_POLL_INTERVAL,
) -> AsyncGenerator[str, None]:
    """Async SSE generator for operator desk queue updates + optional active session thread."""
    channels = [DESK_QUEUE_CHANNEL]
    session_uuid_str = str(session_uuid) if session_uuid is not None else None
    if session_uuid is not None:
        channels.append(session_channel(session_uuid))

    redis_client: Any = None
    pubsub: Any = None

    try:
        # Subscribe and capture initial DB snapshot BEFORE yielding `retry:` so
        # live pub/sub events are guaranteed to arrive as soon as the stream opens.
        redis_client, pubsub = await _open_redis_pubsub(channels)

        queue_fingerprint, _ = await sync_to_async(
            _fetch_desk_queue_fingerprint_sync, thread_sensitive=True
        )()

        last_msg_id = after_id if after_id is not None else 0
        last_status: str | None = None
        last_is_escalated = False
        last_assigned_op_id: int | None = None
        last_operator_connected = False
        initial_snapshot: dict[str, Any] | None = None

        if session_uuid is not None:
            initial_snapshot = await sync_to_async(
                _fetch_session_snapshot_sync, thread_sensitive=True
            )(
                session_uuid,
                after_id=after_id,
                include_messages=(after_id is not None),
            )
            if initial_snapshot is not None:
                last_msg_id = max(last_msg_id, int(initial_snapshot['latest_msg_id']))
                last_status = initial_snapshot['status']
                last_is_escalated = initial_snapshot['is_escalated']
                last_assigned_op_id = initial_snapshot['assigned_operator_id']
                last_operator_connected = initial_snapshot['operator_connected']

        yield f'retry: {DEFAULT_SSE_RETRY_MS}\n\n'
        last_activity = time.monotonic()

        if initial_snapshot is not None:
            for msg_data in initial_snapshot['messages']:
                msg_id = int(msg_data.get('id') or 0)
                last_msg_id = max(last_msg_id, msg_id)
                ev = format_event_for_desk({
                    'type': 'message',
                    'session_uuid': session_uuid_str,
                    'event_id': msg_id,
                    'data': msg_data,
                })
                yield format_sse_event(ev)
                last_activity = time.monotonic()

            if after_id is not None:
                if last_status == ChatSessionStatus.RESOLVED:
                    ev = format_event_for_desk({
                        'type': 'session_resolved',
                        'session_uuid': session_uuid_str,
                        'event_id': None,
                        'data': initial_snapshot['status_data'],
                    })
                else:
                    ev = format_event_for_desk({
                        'type': 'status_changed',
                        'session_uuid': session_uuid_str,
                        'event_id': None,
                        'data': initial_snapshot['status_data'],
                    })
                yield format_sse_event(ev)
                last_activity = time.monotonic()

        dedup = _MessageDeduplicator(last_msg_id)

        # Primary mode: Redis Pub/Sub
        redis_used = pubsub is not None
        while pubsub is not None:
            elapsed = time.monotonic() - last_activity
            remaining = max(0.01, heartbeat_interval - elapsed)
            wait_timeout = min(remaining, 1.0)
            try:
                raw_msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=wait_timeout,
                )
            except (RedisError, OSError, TimeoutError) as exc:
                logger.warning(
                    'Redis pubsub lost for desk SSE (session=%s), switching to DB polling: %s',
                    session_uuid_str,
                    exc,
                )
                await _close_redis_resources(pubsub, redis_client)
                pubsub = None
                redis_client = None
                break

            if raw_msg is not None:
                payload = _decode_pubsub_payload(raw_msg)
                if payload is not None and payload.get('type') in DESK_EVENT_TYPES:
                    ev_type = str(payload['type'])
                    ev_data = (
                        payload.get('data')
                        if isinstance(payload.get('data'), dict)
                        else {}
                    )
                    if ev_type == 'queue_update':
                        desk_ev = format_event_for_desk(payload)
                        yield format_sse_event(desk_ev)
                        last_activity = time.monotonic()
                    elif (
                        session_uuid_str is not None
                        and payload.get('session_uuid') == session_uuid_str
                    ):
                        if ev_type == 'message':
                            if ev_data.get('role') == MessageRole.TOOL:
                                continue
                            msg_id = int(ev_data.get('id') or 0)
                            if not dedup.should_deliver(msg_id):
                                continue
                        elif ev_type == 'operator_joined':
                            last_status = str(
                                ev_data.get('status') or ChatSessionStatus.ESCALATED
                            )
                            last_is_escalated = True
                            last_operator_connected = True
                            if 'assigned_operator_id' in ev_data:
                                last_assigned_op_id = ev_data['assigned_operator_id']
                        elif ev_type == 'status_changed':
                            if ev_data.get('status'):
                                last_status = str(ev_data['status'])
                            if 'is_escalated' in ev_data:
                                last_is_escalated = bool(ev_data['is_escalated'])
                            if 'operator_connected' in ev_data:
                                last_operator_connected = bool(
                                    ev_data['operator_connected']
                                )
                            if 'assigned_operator_id' in ev_data:
                                last_assigned_op_id = ev_data['assigned_operator_id']
                        elif ev_type == 'session_resolved':
                            last_status = ChatSessionStatus.RESOLVED
                            last_is_escalated = False
                            last_operator_connected = False
                            last_assigned_op_id = None

                        desk_ev = format_event_for_desk(payload)
                        yield format_sse_event(desk_ev)
                        last_activity = time.monotonic()

            if time.monotonic() - last_activity >= heartbeat_interval:
                yield ': ping\n\n'
                last_activity = time.monotonic()

        # Fallback mode: DB polling (no Redis or Redis disconnected mid-stream)
        poll_cursor = dedup.floor_id if redis_used else dedup.max_id
        while True:
            elapsed = time.monotonic() - last_activity
            sleep_for = min(poll_interval, max(0.01, heartbeat_interval - elapsed))
            await asyncio.sleep(sleep_for)

            emitted = False
            if session_uuid is not None and session_uuid_str is not None:
                polled = await sync_to_async(
                    _fetch_session_snapshot_sync, thread_sensitive=True
                )(
                    session_uuid,
                    after_id=poll_cursor,
                    include_messages=True,
                )
                if polled is not None:
                    for msg_data in polled['messages']:
                        msg_id = int(msg_data.get('id') or 0)
                        if not dedup.should_deliver(msg_id):
                            continue
                        ev = format_event_for_desk({
                            'type': 'message',
                            'session_uuid': session_uuid_str,
                            'event_id': msg_id,
                            'data': msg_data,
                        })
                        yield format_sse_event(ev)
                        last_activity = time.monotonic()
                        emitted = True
                    poll_cursor = dedup.max_id

                    cur_status = polled['status']
                    cur_escalated = polled['is_escalated']
                    cur_op_id = polled['assigned_operator_id']
                    cur_connected = polled['operator_connected']

                    if (
                        cur_status == ChatSessionStatus.RESOLVED
                        and last_status != ChatSessionStatus.RESOLVED
                    ):
                        ev = format_event_for_desk({
                            'type': 'session_resolved',
                            'session_uuid': session_uuid_str,
                            'event_id': None,
                            'data': polled['status_data'],
                        })
                        yield format_sse_event(ev)
                        last_activity = time.monotonic()
                        emitted = True
                    elif (
                        cur_status == ChatSessionStatus.ESCALATED
                        and cur_connected
                        and (
                            cur_op_id != last_assigned_op_id
                            or not last_operator_connected
                            or last_status != ChatSessionStatus.ESCALATED
                        )
                    ):
                        ev = format_event_for_desk({
                            'type': 'operator_joined',
                            'session_uuid': session_uuid_str,
                            'event_id': None,
                            'data': polled['status_data'],
                        })
                        yield format_sse_event(ev)
                        last_activity = time.monotonic()
                        emitted = True
                    elif (cur_status, cur_escalated, cur_op_id, cur_connected) != (
                        last_status,
                        last_is_escalated,
                        last_assigned_op_id,
                        last_operator_connected,
                    ):
                        ev = format_event_for_desk({
                            'type': 'status_changed',
                            'session_uuid': session_uuid_str,
                            'event_id': None,
                            'data': polled['status_data'],
                        })
                        yield format_sse_event(ev)
                        last_activity = time.monotonic()
                        emitted = True

                    last_status = cur_status
                    last_is_escalated = cur_escalated
                    last_assigned_op_id = cur_op_id
                    last_operator_connected = cur_connected

            new_fingerprint, tab_counts = await sync_to_async(
                _fetch_desk_queue_fingerprint_sync, thread_sensitive=True
            )()
            if new_fingerprint != queue_fingerprint:
                queue_fingerprint = new_fingerprint
                queue_ev = format_event_for_desk({
                    'type': 'queue_update',
                    'session_uuid': session_uuid_str,
                    'event_id': None,
                    'data': {
                        'reason': 'db_poll',
                        'tab_counts': tab_counts,
                    },
                })
                yield format_sse_event(queue_ev)
                last_activity = time.monotonic()
                emitted = True

            if not emitted and time.monotonic() - last_activity >= heartbeat_interval:
                yield ': ping\n\n'
                last_activity = time.monotonic()

    except asyncio.CancelledError:
        logger.info('Desk SSE stream cancelled (session=%s)', session_uuid_str)
        raise
    finally:
        try:
            await _close_redis_resources(pubsub, redis_client)
        finally:
            await sync_to_async(
                _close_non_atomic_connections, thread_sensitive=True
            )()
