import asyncio
import json
import re
import shutil
import subprocess
import uuid
from collections import defaultdict
from typing import Any
from unittest.mock import patch

import redis.exceptions
from accounts.models import Role
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.db import transaction
from django.template.loader import render_to_string
from django.test import (
    AsyncClient,
    Client,
    RequestFactory,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse

from support import realtime
from support.models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    MessageRole,
    MessageSource,
    SupportTicket,
    TicketReason,
    TicketStatus,
)
from support.services import ChatSessionService, SupportDeskService

User = get_user_model()


def parse_sse_frame(raw_frame: str | bytes) -> dict[str, Any]:
    text = raw_frame.decode('utf-8') if isinstance(raw_frame, bytes) else raw_frame
    stripped = text.strip()
    if stripped.startswith(':'):
        return {'kind': 'comment', 'raw': stripped}
    if stripped.startswith('retry:'):
        return {'kind': 'retry', 'retry': int(stripped.split(':', 1)[1].strip())}
    event_id: str | None = None
    event_type: str | None = None
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith('id:'):
            event_id = line.split(':', 1)[1].strip()
        elif line.startswith('event:'):
            event_type = line.split(':', 1)[1].strip()
        elif line.startswith('data:'):
            data_lines.append(line.split(':', 1)[1].strip())
    raw_data = '\n'.join(data_lines)
    payload = json.loads(raw_data) if raw_data else None
    return {
        'kind': 'event',
        'id': event_id,
        'event': event_type,
        'payload': payload,
    }


async def next_sse_frame(stream: Any, *, timeout: float = 3.0) -> dict[str, Any]:
    raw = await asyncio.wait_for(stream.__anext__(), timeout=timeout)
    return parse_sse_frame(raw)


async def collect_event_frames(
    stream: Any,
    count: int,
    *,
    timeout: float = 3.0,
    include_comments: bool = False,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while len(collected) < count:
        remaining = max(0.05, deadline - asyncio.get_running_loop().time())
        frame = await next_sse_frame(stream, timeout=remaining)
        if frame['kind'] == 'retry':
            continue
        if frame['kind'] == 'comment' and not include_comments:
            continue
        collected.append(frame)
    return collected


class FakeAsyncPubSub:
    def __init__(
        self,
        broker: 'FakeRedisBroker',
        *,
        fail_subscribe: Exception | None = None,
        fail_after_messages: int | None = None,
        fail_exception: Exception | None = None,
    ) -> None:
        self.broker = broker
        self.fail_subscribe = fail_subscribe
        self.fail_after_messages = fail_after_messages
        self.fail_exception = fail_exception
        self.channels: list[str] = []
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.delivered_count = 0
        self.unsubscribed = False
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        if self.fail_subscribe is not None:
            raise self.fail_subscribe
        self.loop = asyncio.get_running_loop()
        for ch in channels:
            self.channels.append(ch)
            self.broker.register(ch, self)

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool = True,
        timeout: float = 1.0,
    ) -> dict[str, Any] | None:
        if (
            self.fail_after_messages is not None
            and self.delivered_count >= self.fail_after_messages
        ):
            raise (
                self.fail_exception
                or redis.exceptions.ConnectionError('Simulated Redis drop')
            )
        try:
            msg = await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except TimeoutError:
            return None
        self.delivered_count += 1
        return msg

    async def unsubscribe(self) -> None:
        self.unsubscribed = True
        for ch in list(self.channels):
            self.broker.unregister(ch, self)
        self.channels.clear()

    async def aclose(self) -> None:
        self.closed = True


class FakeAsyncRedisClient:
    def __init__(
        self,
        broker: 'FakeRedisBroker',
        *,
        fail_subscribe: Exception | None = None,
        fail_after_messages: int | None = None,
        fail_exception: Exception | None = None,
    ) -> None:
        self.broker = broker
        self.fail_subscribe = fail_subscribe
        self.fail_after_messages = fail_after_messages
        self.fail_exception = fail_exception
        self.closed = False
        self.created_pubsubs: list[FakeAsyncPubSub] = []

    def pubsub(self) -> FakeAsyncPubSub:
        ps = FakeAsyncPubSub(
            self.broker,
            fail_subscribe=self.fail_subscribe,
            fail_after_messages=self.fail_after_messages,
            fail_exception=self.fail_exception,
        )
        self.created_pubsubs.append(ps)
        return ps

    async def aclose(self) -> None:
        self.closed = True


class FakeSyncRedisClient:
    def __init__(self, broker: 'FakeRedisBroker') -> None:
        self.broker = broker

    def publish(self, channel: str, data: str) -> int:
        return self.broker.publish(channel, data)

    def close(self) -> None:
        return None


class FakeRedisBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[FakeAsyncPubSub]] = defaultdict(list)
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.created_async_clients: list[FakeAsyncRedisClient] = []

    def register(self, channel: str, pubsub: FakeAsyncPubSub) -> None:
        self._subscribers[channel].append(pubsub)

    def unregister(self, channel: str, pubsub: FakeAsyncPubSub) -> None:
        if channel in self._subscribers and pubsub in self._subscribers[channel]:
            self._subscribers[channel].remove(pubsub)

    def publish(self, channel: str, raw_data: str) -> int:
        parsed = json.loads(raw_data)
        self.published.append((channel, parsed))
        subs = list(self._subscribers.get(channel, []))
        delivered = 0
        for ps in subs:
            if ps.closed or ps.unsubscribed or ps.loop is None:
                continue
            item = {'type': 'message', 'channel': channel, 'data': raw_data}
            try:
                ps.loop.call_soon_threadsafe(ps.queue.put_nowait, item)
                delivered += 1
            except RuntimeError:
                continue
        return delivered

    def sync_client(self, _redis_url: str) -> FakeSyncRedisClient:
        return FakeSyncRedisClient(self)

    def async_client(
        self,
        _redis_url: str,
        *,
        fail_subscribe: Exception | None = None,
        fail_after_messages: int | None = None,
        fail_exception: Exception | None = None,
    ) -> FakeAsyncRedisClient:
        client = FakeAsyncRedisClient(
            self,
            fail_subscribe=fail_subscribe,
            fail_after_messages=fail_after_messages,
            fail_exception=fail_exception,
        )
        self.created_async_clients.append(client)
        return client


class RealtimeHelpersUnitTest(TestCase):
    def tearDown(self):
        realtime.reset_realtime_clients()

    def test_get_realtime_redis_url_resolution_order(self):
        with override_settings(SUPPORT_REALTIME_REDIS_URL='redis://explicit:6379/1'):
            self.assertEqual(
                realtime.get_realtime_redis_url(),
                'redis://explicit:6379/1',
            )
        with override_settings(SUPPORT_REALTIME_REDIS_URL=''):
            self.assertEqual(realtime.get_realtime_redis_url(), '')
        with override_settings(
            CACHES={
                'default': {
                    'BACKEND': 'django.core.cache.backends.redis.RedisCache',
                    'LOCATION': 'redis://cache-host:6379/2',
                }
            }
        ):
            self.assertEqual(
                realtime.get_realtime_redis_url(),
                'redis://cache-host:6379/2',
            )
        # Default LocMemCache during testing returns empty string
        self.assertEqual(realtime.get_realtime_redis_url(), '')

    def test_sanitize_event_for_client_strips_operator_pii(self):
        raw_message_event = {
            'type': 'message',
            'session_uuid': str(uuid.uuid4()),
            'event_id': 99,
            'data': {
                'id': 99,
                'role': MessageRole.OPERATOR,
                'sender': 'operator',
                'sender_label': 'Адміністратор ресторану',
                'operator_name': 'Олена Коваль',
                'source': MessageSource.WEB,
                'source_badge': 'Web',
                'content': 'Добрий день!',
                'created_at': '14:29',
            },
        }
        client_ev = realtime.sanitize_event_for_client(raw_message_event)
        self.assertEqual(client_ev['data']['sender_label'], 'Адміністратор ресторану')
        self.assertNotIn('operator_name', client_ev['data'])
        self.assertNotIn('source_badge', client_ev['data'])

        desk_ev = realtime.format_event_for_desk(raw_message_event)
        self.assertEqual(desk_ev['data']['operator_name'], 'Олена Коваль')
        self.assertEqual(desk_ev['data']['source_badge'], 'Web')

        joined_event = {
            'type': 'operator_joined',
            'session_uuid': str(uuid.uuid4()),
            'event_id': None,
            'data': {
                'status': ChatSessionStatus.ESCALATED,
                'is_escalated': True,
                'operator_connected': True,
                'assigned_operator_id': 7,
                'assigned_operator_name': 'Олена Коваль',
                'is_working_hours': True,
            },
        }
        client_joined = realtime.sanitize_event_for_client(joined_event)
        self.assertTrue(client_joined['data']['operator_connected'])
        self.assertNotIn('assigned_operator_id', client_joined['data'])
        self.assertNotIn('assigned_operator_name', client_joined['data'])

    def test_format_sse_event_emits_id_only_for_messages(self):
        msg_frame = realtime.format_sse_event({
            'type': 'message',
            'session_uuid': 'abc',
            'event_id': 42,
            'data': {'id': 42, 'content': 'Привіт\nрядок 2'},
        })
        self.assertIn('id: 42\n', msg_frame)
        self.assertIn('event: message\n', msg_frame)
        self.assertTrue(msg_frame.endswith('\n\n'))
        parsed = parse_sse_frame(msg_frame)
        self.assertEqual(parsed['id'], '42')
        self.assertEqual(parsed['payload']['data']['content'], 'Привіт\nрядок 2')

        status_frame = realtime.format_sse_event({
            'type': 'status_changed',
            'session_uuid': 'abc',
            'event_id': 42,
            'data': {'status': 'ACTIVE'},
        })
        self.assertNotIn('id:', status_frame)
        self.assertIn('event: status_changed\n', status_frame)

    def test_publish_fails_silently_when_redis_errors(self):
        class FailingRedis:
            def publish(self, channel: str, data: str) -> None:
                raise redis.exceptions.ConnectionError('Redis unreachable')

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://broken:6379/0'),
            patch('support.realtime._get_sync_redis_client', return_value=FailingRedis()),
        ):
            ok_session = realtime.publish_session_event(
                uuid.uuid4(), 'message', {'id': 1, 'content': 'test'}, event_id=1
            )
            ok_desk = realtime.publish_desk_event('queue_update', {'reason': 'test'})
            self.assertFalse(ok_session)
            self.assertFalse(ok_desk)


class RealtimeSseEndpointsValidationAndRbacTest(TestCase):
    def setUp(self):
        cache.clear()
        self.sync_client = Client()
        self.async_client = AsyncClient()
        self.customer = User.objects.create_user(
            email='sse_customer@example.com',
            password='password123',
            first_name='Анна',
            last_name='Шевченко',
            role=Role.CUSTOMER,
        )
        self.courier = User.objects.create_user(
            email='sse_courier@example.com',
            password='password123',
            role=Role.COURIER,
        )
        self.operator = User.objects.create_user(
            email='sse_operator@example.com',
            password='password123',
            first_name='Олена',
            last_name='Коваль',
            role=Role.RESTAURANT_ADMIN,
        )
        self.superuser = User.objects.create_superuser(
            email='sse_super@example.com',
            password='password123',
        )

    def test_url_names_methods_rbac_and_wsgi_fallback(self):
        self.assertEqual(reverse('support:chat_events'), '/support/chat/events/')
        self.assertEqual(reverse('support:desk_events'), '/support/desk/events/')

        # Methods
        self.assertEqual(self.sync_client.post('/support/chat/events/').status_code, 405)
        self.assertEqual(self.sync_client.head('/support/chat/events/').status_code, 405)

        # Desk RBAC
        res_anon = self.sync_client.get('/support/desk/events/')
        self.assertEqual(res_anon.status_code, 302)
        self.assertIn('/accounts/login/', res_anon['Location'])

        self.sync_client.force_login(self.customer)
        self.assertEqual(self.sync_client.get('/support/desk/events/').status_code, 403)

        self.sync_client.force_login(self.courier)
        self.assertEqual(self.sync_client.get('/support/desk/events/').status_code, 403)

        # Operator methods & WSGI 503 fallback
        self.sync_client.force_login(self.operator)
        self.assertEqual(self.sync_client.post('/support/desk/events/').status_code, 405)
        self.assertEqual(self.sync_client.head('/support/desk/events/').status_code, 405)
        res_desk_wsgi = self.sync_client.get('/support/desk/events/')
        self.assertEqual(res_desk_wsgi.status_code, 503)
        self.assertEqual(res_desk_wsgi['X-SSE-Fallback'], 'polling')

        # Client validation & WSGI 503 fallback
        self.sync_client.force_login(self.customer)
        self.assertEqual(
            self.sync_client.get('/support/chat/events/?after_id=abc').status_code,
            400,
        )
        self.assertEqual(
            self.sync_client.get('/support/chat/events/?after_id=-5').status_code,
            400,
        )
        self.assertEqual(self.sync_client.get('/support/chat/events/').status_code, 404)

        ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        res_client_wsgi = self.sync_client.get('/support/chat/events/')
        self.assertEqual(res_client_wsgi.status_code, 503)
        self.assertEqual(res_client_wsgi['X-SSE-Fallback'], 'polling')

    async def test_async_validation_and_superuser_access(self):
        await self.async_client.aforce_login(self.operator)
        res_bad_after = await self.async_client.get('/support/desk/events/?after_id=bad')
        self.assertEqual(res_bad_after.status_code, 400)

        res_bad_uuid = await self.async_client.get(
            '/support/desk/events/?session_uuid=not-a-uuid'
        )
        self.assertEqual(res_bad_uuid.status_code, 400)

        res_missing = await self.async_client.get(
            f'/support/desk/events/?session_uuid={uuid.uuid4()}'
        )
        self.assertEqual(res_missing.status_code, 404)

        # Superuser can open SSE with malformed Last-Event-ID safely ignored
        await self.async_client.aforce_login(self.superuser)
        res_ok = await self.async_client.get(
            '/support/desk/events/',
            headers={'Last-Event-ID': 'not-numeric'},
        )
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(res_ok['Content-Type'], 'text/event-stream')
        self.assertEqual(res_ok['Cache-Control'], 'no-cache')
        self.assertEqual(res_ok['X-Accel-Buffering'], 'no')
        try:
            first = await next_sse_frame(res_ok.streaming_content)
            self.assertEqual(first['kind'], 'retry')
        finally:
            await res_ok.streaming_content.aclose()


class RealtimeSsePubSubAndFallbackTest(TransactionTestCase):
    def setUp(self):
        cache.clear()
        realtime.reset_realtime_clients()
        self.customer_a = User.objects.create_user(
            email='sse_live_a@example.com',
            password='password123',
            first_name='Анна',
            last_name='Шевченко',
            role=Role.CUSTOMER,
        )
        self.customer_b = User.objects.create_user(
            email='sse_live_b@example.com',
            password='password123',
            first_name='Богдан',
            last_name='Мельник',
            role=Role.CUSTOMER,
        )
        self.operator = User.objects.create_user(
            email='sse_live_op@example.com',
            password='password123',
            first_name='Олена',
            last_name='Коваль',
            role=Role.RESTAURANT_ADMIN,
        )

    def tearDown(self):
        realtime.reset_realtime_clients()

    async def test_client_sse_redis_pubsub_full_lifecycle_and_pii_stripping(self):
        broker = FakeRedisBroker()
        session = await sync_to_async(
            lambda: ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
            ),
            thread_sensitive=True,
        )()
        await sync_to_async(
            lambda: SupportTicket.objects.create(
                session=session,
                reason=TicketReason.HUMAN_REQUESTED,
                details='Потрібна допомога',
                status=TicketStatus.OPEN,
            ),
            thread_sensitive=True,
        )()

        client_a = AsyncClient()
        await client_a.aforce_login(self.customer_a)

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=broker.async_client,
            ),
        ):
            response = await client_a.get('/support/chat/events/')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'text/event-stream')
            stream = response.streaming_content
            try:
                first = await next_sse_frame(stream)
                self.assertEqual(first['kind'], 'retry')

                # Wait until pubsub subscribed by triggering claim
                await sync_to_async(
                    SupportDeskService.claim_session, thread_sensitive=True
                )(session.session_uuid, self.operator)

                claim_events = await collect_event_frames(stream, 2)
                self.assertEqual(claim_events[0]['event'], 'message')
                self.assertIn(
                    'До діалогу підключився адміністратор',
                    claim_events[0]['payload']['data']['content'],
                )
                self.assertIsNotNone(claim_events[0]['id'])

                self.assertEqual(claim_events[1]['event'], 'operator_joined')
                joined_data = claim_events[1]['payload']['data']
                self.assertTrue(joined_data['operator_connected'])
                self.assertNotIn('operator_name', joined_data)
                self.assertNotIn('assigned_operator_name', joined_data)
                self.assertNotIn('assigned_operator_id', joined_data)

                # Operator sends reply -> client receives message WITHOUT operator_name
                reply_msg = await sync_to_async(
                    SupportDeskService.send_operator_message, thread_sensitive=True
                )(
                    session.session_uuid,
                    self.operator,
                    'Добрий день! Вже уточнюю статус вашого замовлення.',
                    source=MessageSource.WEB,
                )
                reply_frames = await collect_event_frames(stream, 1)
                self.assertEqual(reply_frames[0]['event'], 'message')
                self.assertEqual(reply_frames[0]['id'], str(reply_msg.id))
                msg_data = reply_frames[0]['payload']['data']
                self.assertEqual(msg_data['sender'], 'operator')
                self.assertEqual(msg_data['sender_label'], 'Адміністратор ресторану')
                self.assertNotIn('operator_name', msg_data)
                self.assertNotIn('source_badge', msg_data)

                # Operator returns to AI -> message + status_changed(ACTIVE)
                await sync_to_async(
                    SupportDeskService.return_to_ai, thread_sensitive=True
                )(session.session_uuid, self.operator)
                return_frames = await collect_event_frames(stream, 2)
                self.assertEqual(return_frames[0]['event'], 'message')
                self.assertEqual(return_frames[1]['event'], 'status_changed')
                self.assertEqual(return_frames[1]['payload']['data']['status'], 'ACTIVE')
                self.assertFalse(return_frames[1]['payload']['data']['is_escalated'])

                # Re-escalate and resolve -> message + session_resolved -> stream ends
                await sync_to_async(
                    ChatSessionService.escalate_session, thread_sensitive=True
                )(session.session_uuid, TicketReason.ORDER_ISSUE, 'Нове питання')
                esc_frames = await collect_event_frames(stream, 2)
                self.assertEqual(esc_frames[0]['event'], 'message')
                self.assertEqual(esc_frames[1]['event'], 'status_changed')
                self.assertEqual(esc_frames[1]['payload']['data']['status'], 'ESCALATED')

                await sync_to_async(
                    SupportDeskService.resolve_session, thread_sensitive=True
                )(session.session_uuid, self.operator)
                res_frames = await collect_event_frames(stream, 2)
                self.assertEqual(res_frames[0]['event'], 'message')
                self.assertEqual(res_frames[1]['event'], 'session_resolved')
                self.assertEqual(res_frames[1]['payload']['data']['status'], 'RESOLVED')

                # Client SSE closes cleanly after session_resolved
                with self.assertRaises(StopAsyncIteration):
                    await asyncio.wait_for(stream.__anext__(), timeout=2.0)
            finally:
                await stream.aclose()

    async def test_desk_sse_redis_pubsub_receives_messages_operator_name_and_queue_updates(self):
        broker = FakeRedisBroker()
        session_a = await sync_to_async(
            lambda: ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
            ),
            thread_sensitive=True,
        )()
        session_b = await sync_to_async(
            lambda: ChatSession.objects.create(
                user=self.customer_b,
                status=ChatSessionStatus.ACTIVE,
                is_escalated=False,
            ),
            thread_sensitive=True,
        )()
        await sync_to_async(
            lambda: SupportTicket.objects.create(
                session=session_a,
                reason=TicketReason.COMPLAINT,
                status=TicketStatus.OPEN,
            ),
            thread_sensitive=True,
        )()

        desk_client = AsyncClient()
        await desk_client.aforce_login(self.operator)

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=broker.async_client,
            ),
        ):
            response = await desk_client.get(
                f'/support/desk/events/?session_uuid={session_a.session_uuid}'
            )
            self.assertEqual(response.status_code, 200)
            stream = response.streaming_content
            try:
                first = await next_sse_frame(stream)
                self.assertEqual(first['kind'], 'retry')

                # Customer sends message -> desk receives message + queue_update
                await sync_to_async(
                    SupportDeskService.send_customer_message, thread_sensitive=True
                )(session_a.session_uuid, 'Де моє замовлення?')
                cust_frames = await collect_event_frames(stream, 2)
                event_types = {f['event'] for f in cust_frames}
                self.assertEqual(event_types, {'message', 'queue_update'})
                msg_frame = next(f for f in cust_frames if f['event'] == 'message')
                self.assertEqual(msg_frame['payload']['data']['content'], 'Де моє замовлення?')
                self.assertEqual(msg_frame['payload']['data']['sender_label'], 'Анна Шевченко')

                # Operator claims & replies -> desk receives operator_name + source_badge
                await sync_to_async(
                    SupportDeskService.claim_session, thread_sensitive=True
                )(session_a.session_uuid, self.operator)
                claim_frames = await collect_event_frames(stream, 3)
                self.assertEqual(
                    {f['event'] for f in claim_frames},
                    {'message', 'operator_joined', 'queue_update'},
                )
                joined = next(f for f in claim_frames if f['event'] == 'operator_joined')
                self.assertEqual(joined['payload']['data']['assigned_operator_name'], 'Олена Коваль')

                await sync_to_async(
                    SupportDeskService.send_operator_message, thread_sensitive=True
                )(
                    session_a.session_uuid,
                    self.operator,
                    'Перевіряю з курʼєром!',
                    source=MessageSource.WEB,
                )
                op_frames = await collect_event_frames(stream, 2)
                op_msg = next(f for f in op_frames if f['event'] == 'message')
                self.assertEqual(op_msg['payload']['data']['operator_name'], 'Олена Коваль')
                self.assertEqual(op_msg['payload']['data']['source_badge'], 'Web')

                # Resolve session_a -> desk receives session_resolved AND stays open!
                await sync_to_async(
                    SupportDeskService.resolve_session, thread_sensitive=True
                )(session_a.session_uuid, self.operator)
                resolve_frames = await collect_event_frames(stream, 3)
                self.assertEqual(
                    {f['event'] for f in resolve_frames},
                    {'message', 'session_resolved', 'queue_update'},
                )

                # Another session escalates -> desk STILL receives queue_update (not session_b's message!)
                await sync_to_async(
                    ChatSessionService.escalate_session, thread_sensitive=True
                )(session_b.session_uuid, TicketReason.HUMAN_REQUESTED, 'Інший клієнт')
                other_frames = await collect_event_frames(stream, 1)
                self.assertEqual(other_frames[0]['event'], 'queue_update')
                self.assertEqual(
                    other_frames[0]['payload']['session_uuid'],
                    str(session_b.session_uuid),
                )
            finally:
                await stream.aclose()

    async def test_client_isolation_and_idor_protection(self):
        broker = FakeRedisBroker()

        def setup_sessions():
            s_a = ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
                assigned_operator=self.operator,
            )
            s_b = ChatSession.objects.create(
                user=self.customer_b,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
                assigned_operator=self.operator,
            )
            return s_a, s_b

        session_a, session_b = await sync_to_async(setup_sessions, thread_sensitive=True)()

        client_a = AsyncClient()
        await client_a.aforce_login(self.customer_a)

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=broker.async_client,
            ),
        ):
            # Client A tries IDOR by passing Client B's session_uuid in query string
            response = await client_a.get(
                f'/support/chat/events/?session_uuid={session_b.session_uuid}'
            )
            self.assertEqual(response.status_code, 200)
            stream = response.streaming_content
            try:
                first = await next_sse_frame(stream)
                self.assertEqual(first['kind'], 'retry')

                # Operator sends secret message to Client B, then message to Client A
                await sync_to_async(
                    SupportDeskService.send_operator_message, thread_sensitive=True
                )(session_b.session_uuid, self.operator, 'СЕКРЕТ ДЛЯ КЛІЄНТА Б')
                await sync_to_async(
                    SupportDeskService.send_operator_message, thread_sensitive=True
                )(session_a.session_uuid, self.operator, 'Повідомлення для клієнта А')

                frames = await collect_event_frames(stream, 1)
                self.assertEqual(frames[0]['event'], 'message')
                self.assertEqual(
                    frames[0]['payload']['data']['content'],
                    'Повідомлення для клієнта А',
                )
                self.assertEqual(
                    frames[0]['payload']['session_uuid'],
                    str(session_a.session_uuid),
                )
            finally:
                await stream.aclose()

    async def test_guest_isolation_between_two_guests(self):
        broker = FakeRedisBroker()
        guest_1 = AsyncClient()
        guest_2 = AsyncClient()

        res1 = await guest_1.post(
            '/support/chat/escalate/',
            data=json.dumps({}),
            content_type='application/json',
        )
        res2 = await guest_2.post(
            '/support/chat/escalate/',
            data=json.dumps({}),
            content_type='application/json',
        )
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res2.status_code, 200)
        s1_uuid = res1.json()['session_uuid']
        s2_uuid = res2.json()['session_uuid']

        await sync_to_async(SupportDeskService.claim_session, thread_sensitive=True)(
            s1_uuid, self.operator
        )
        await sync_to_async(SupportDeskService.claim_session, thread_sensitive=True)(
            s2_uuid, self.operator
        )

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=broker.async_client,
            ),
        ):
            response = await guest_1.get('/support/chat/events/')
            self.assertEqual(response.status_code, 200)
            stream = response.streaming_content
            try:
                await next_sse_frame(stream)  # retry

                await sync_to_async(
                    SupportDeskService.send_operator_message, thread_sensitive=True
                )(s2_uuid, self.operator, 'Тільки для Гостя 2')
                await sync_to_async(
                    SupportDeskService.send_operator_message, thread_sensitive=True
                )(s1_uuid, self.operator, 'Тільки для Гостя 1')

                frames = await collect_event_frames(stream, 1)
                self.assertEqual(
                    frames[0]['payload']['data']['content'],
                    'Тільки для Гостя 1',
                )
            finally:
                await stream.aclose()

    async def test_catchup_after_id_last_event_id_and_deduplication(self):
        broker = FakeRedisBroker()

        def setup_messages():
            session = ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
                assigned_operator=self.operator,
            )
            m1 = ChatMessage.objects.create(
                session=session,
                role=MessageRole.USER,
                content='Перше',
            )
            m_tool = ChatMessage.objects.create(
                session=session,
                role=MessageRole.TOOL,
                content='{"internal": true}',
            )
            m2 = ChatMessage.objects.create(
                session=session,
                role=MessageRole.OPERATOR,
                sender_user=self.operator,
                content='Друге',
            )
            m3 = ChatMessage.objects.create(
                session=session,
                role=MessageRole.OPERATOR,
                sender_user=self.operator,
                content='Третє',
            )
            return session, m1, m_tool, m2, m3

        session, m1, _m_tool, m2, m3 = await sync_to_async(
            setup_messages, thread_sensitive=True
        )()

        client_a = AsyncClient()
        await client_a.aforce_login(self.customer_a)

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=broker.async_client,
            ),
        ):
            # Pass ?after_id=m1.id AND Last-Event-ID=m2.id -> max(m1, m2) = m2 -> ONLY m3!
            response = await client_a.get(
                f'/support/chat/events/?after_id={m1.id}',
                headers={'Last-Event-ID': str(m2.id)},
            )
            self.assertEqual(response.status_code, 200)
            stream = response.streaming_content
            try:
                await next_sse_frame(stream)  # retry
                catchup = await collect_event_frames(stream, 2)
                self.assertEqual(catchup[0]['event'], 'message')
                self.assertEqual(catchup[0]['id'], str(m3.id))
                self.assertEqual(catchup[0]['payload']['data']['content'], 'Третє')
                self.assertNotIn('operator_name', catchup[0]['payload']['data'])
                self.assertEqual(catchup[1]['event'], 'status_changed')
                self.assertTrue(catchup[1]['payload']['data']['operator_connected'])

                # Duplicate m3 over Redis pub/sub MUST be ignored, while new m4 IS delivered
                realtime.publish_session_event(
                    session.session_uuid,
                    'message',
                    realtime.build_message_event_data(m3, session=session),
                    event_id=m3.id,
                )
                m4 = await sync_to_async(
                    SupportDeskService.send_operator_message, thread_sensitive=True
                )(session.session_uuid, self.operator, 'Четверте')

                live = await collect_event_frames(stream, 1)
                self.assertEqual(live[0]['event'], 'message')
                self.assertEqual(live[0]['id'], str(m4.id))
                self.assertEqual(live[0]['payload']['data']['content'], 'Четверте')
            finally:
                await stream.aclose()

    async def test_client_and_desk_sse_db_polling_fallback_without_redis_and_heartbeat(self):
        session = await sync_to_async(
            lambda: ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
            ),
            thread_sensitive=True,
        )()
        await sync_to_async(
            lambda: SupportTicket.objects.create(
                session=session,
                reason=TicketReason.ORDER_ISSUE,
                status=TicketStatus.OPEN,
            ),
            thread_sensitive=True,
        )()

        client_a = AsyncClient()
        await client_a.aforce_login(self.customer_a)
        desk_client = AsyncClient()
        await desk_client.aforce_login(self.operator)

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL=''),
            patch('support.realtime.DEFAULT_DB_POLL_INTERVAL', 0.03),
            patch('support.realtime.DEFAULT_HEARTBEAT_INTERVAL', 0.08),
            patch(
                'support.views.subscribe_client_session_events',
                side_effect=lambda sid, after_id=None: realtime.subscribe_client_session_events(
                    sid,
                    after_id=after_id,
                    heartbeat_interval=0.08,
                    poll_interval=0.03,
                ),
            ),
            patch(
                'support.desk_views.subscribe_desk_events',
                side_effect=lambda session_uuid=None, after_id=None: realtime.subscribe_desk_events(
                    session_uuid=session_uuid,
                    after_id=after_id,
                    heartbeat_interval=0.08,
                    poll_interval=0.03,
                ),
            ),
        ):
            res_client = await client_a.get('/support/chat/events/')
            res_desk = await desk_client.get(
                f'/support/desk/events/?session_uuid={session.session_uuid}'
            )
            self.assertEqual(res_client.status_code, 200)
            self.assertEqual(res_desk.status_code, 200)

            c_stream = res_client.streaming_content
            d_stream = res_desk.streaming_content
            try:
                await next_sse_frame(c_stream)  # retry
                await next_sse_frame(d_stream)  # retry

                # Wait for heartbeat `: ping` when idle
                c_ping = await collect_event_frames(c_stream, 1, include_comments=True)
                self.assertEqual(c_ping[0]['kind'], 'comment')
                self.assertEqual(c_ping[0]['raw'], ': ping')

                d_ping = await collect_event_frames(d_stream, 1, include_comments=True)
                self.assertEqual(d_ping[0]['kind'], 'comment')
                self.assertEqual(d_ping[0]['raw'], ': ping')

                # Claim -> both streams pick up message + operator_joined (+ queue_update on desk)
                await sync_to_async(
                    SupportDeskService.claim_session, thread_sensitive=True
                )(session.session_uuid, self.operator)

                c_claim = await collect_event_frames(c_stream, 2)
                self.assertEqual(
                    [f['event'] for f in c_claim],
                    ['message', 'operator_joined'],
                )
                self.assertNotIn('operator_name', c_claim[1]['payload']['data'])

                d_claim = await collect_event_frames(d_stream, 3)
                self.assertEqual(
                    {f['event'] for f in d_claim},
                    {'message', 'operator_joined', 'queue_update'},
                )

                # Operator reply -> both receive message (desk has operator_name, client does not)
                await sync_to_async(
                    SupportDeskService.send_operator_message, thread_sensitive=True
                )(session.session_uuid, self.operator, 'Відповідь без Redis')

                c_msg = await collect_event_frames(c_stream, 1)
                self.assertEqual(c_msg[0]['payload']['data']['content'], 'Відповідь без Redis')
                self.assertNotIn('operator_name', c_msg[0]['payload']['data'])

                d_msg_and_q = await collect_event_frames(d_stream, 2)
                d_msg = next(f for f in d_msg_and_q if f['event'] == 'message')
                self.assertEqual(d_msg['payload']['data']['operator_name'], 'Олена Коваль')

                # Return to AI -> status_changed
                await sync_to_async(
                    SupportDeskService.return_to_ai, thread_sensitive=True
                )(session.session_uuid, self.operator)
                c_ret = await collect_event_frames(c_stream, 2)
                self.assertEqual(c_ret[1]['event'], 'status_changed')
                self.assertEqual(c_ret[1]['payload']['data']['status'], 'ACTIVE')

                # Resolve -> session_resolved
                await sync_to_async(
                    SupportDeskService.resolve_session, thread_sensitive=True
                )(session.session_uuid, self.operator)
                c_res = await collect_event_frames(c_stream, 2)
                self.assertEqual(c_res[1]['event'], 'session_resolved')
            finally:
                await c_stream.aclose()
                await d_stream.aclose()

    async def test_midstream_redis_disconnect_falls_back_to_db_polling(self):
        broker = FakeRedisBroker()
        session = await sync_to_async(
            lambda: ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
                assigned_operator=self.operator,
            ),
            thread_sensitive=True,
        )()

        def dropping_async_client(url: str) -> FakeAsyncRedisClient:
            return broker.async_client(
                url,
                fail_after_messages=1,
                fail_exception=redis.exceptions.ConnectionError('Redis restarted'),
            )

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=dropping_async_client,
            ),
        ):
            gen = realtime.subscribe_client_session_events(
                session.session_uuid,
                heartbeat_interval=5.0,
                poll_interval=0.03,
            )
            try:
                await next_sse_frame(gen)  # retry

                # Message 1 delivered via Redis Pub/Sub
                await sync_to_async(
                    SupportDeskService.send_operator_message, thread_sensitive=True
                )(session.session_uuid, self.operator, 'Через Redis')
                f1 = await collect_event_frames(gen, 1)
                self.assertEqual(f1[0]['payload']['data']['content'], 'Через Redis')

                # Message 2 written to DB after Redis drops -> seamlessly delivered via DB fallback!
                await sync_to_async(
                    SupportDeskService.send_operator_message, thread_sensitive=True
                )(session.session_uuid, self.operator, 'Після падіння Redis')
                f2 = await collect_event_frames(gen, 1)
                self.assertEqual(
                    f2[0]['payload']['data']['content'],
                    'Після падіння Redis',
                )

                # Broken Redis resources were closed cleanly
                self.assertEqual(len(broker.created_async_clients), 1)
                self.assertTrue(broker.created_async_clients[0].closed)
                self.assertTrue(broker.created_async_clients[0].created_pubsubs[0].closed)
            finally:
                await gen.aclose()

    async def test_cancelled_error_closes_redis_pubsub_and_db_connections(self):
        broker = FakeRedisBroker()
        session = await sync_to_async(
            lambda: ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
            ),
            thread_sensitive=True,
        )()

        close_db_calls = 0
        original_close = realtime._close_non_atomic_connections

        def tracked_close() -> None:
            nonlocal close_db_calls
            close_db_calls += 1
            original_close()

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=broker.async_client,
            ),
            patch(
                'support.realtime._close_non_atomic_connections',
                side_effect=tracked_close,
            ),
        ):
            # Test client SSE cancellation
            client_gen = realtime.subscribe_client_session_events(
                session.session_uuid,
                heartbeat_interval=10.0,
                poll_interval=0.5,
            )
            first = await next_sse_frame(client_gen)
            self.assertEqual(first['kind'], 'retry')

            async def consume_client():
                async for _ in client_gen:
                    pass

            task_c = asyncio.create_task(consume_client())
            await asyncio.sleep(0.05)
            close_before = close_db_calls
            task_c.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task_c

            self.assertEqual(len(broker.created_async_clients), 1)
            c_redis = broker.created_async_clients[0]
            c_pubsub = c_redis.created_pubsubs[0]
            self.assertTrue(c_pubsub.unsubscribed)
            self.assertTrue(c_pubsub.closed)
            self.assertTrue(c_redis.closed)
            self.assertGreater(close_db_calls, close_before)

            # Test desk SSE cancellation
            desk_gen = realtime.subscribe_desk_events(
                session_uuid=session.session_uuid,
                heartbeat_interval=10.0,
                poll_interval=0.5,
            )
            first_d = await next_sse_frame(desk_gen)
            self.assertEqual(first_d['kind'], 'retry')

            async def consume_desk():
                async for _ in desk_gen:
                    pass

            task_d = asyncio.create_task(consume_desk())
            await asyncio.sleep(0.05)
            close_before_d = close_db_calls
            task_d.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task_d

            self.assertEqual(len(broker.created_async_clients), 2)
            d_redis = broker.created_async_clients[1]
            d_pubsub = d_redis.created_pubsubs[0]
            self.assertTrue(d_pubsub.unsubscribed)
            self.assertTrue(d_pubsub.closed)
            self.assertTrue(d_redis.closed)
            self.assertGreater(close_db_calls, close_before_d)

    async def test_transaction_rollback_does_not_publish_events(self):
        broker = FakeRedisBroker()
        session = await sync_to_async(
            lambda: ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
            ),
            thread_sensitive=True,
        )()

        def failing_tx():
            with transaction.atomic():
                SupportDeskService.send_customer_message(
                    session.session_uuid,
                    'Не має опублікуватися',
                )
                raise RuntimeError('Force rollback')

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
        ):
            with self.assertRaises(RuntimeError):
                await sync_to_async(failing_tx, thread_sensitive=True)()
            self.assertEqual(broker.published, [])

    async def test_reset_session_publishes_session_resolved_and_queue_update(self):
        broker = FakeRedisBroker()
        client_a = AsyncClient()
        await client_a.aforce_login(self.customer_a)
        res_esc = await client_a.post(
            '/support/chat/escalate/',
            data=json.dumps({}),
            content_type='application/json',
        )
        self.assertEqual(res_esc.status_code, 200)

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=broker.async_client,
            ),
        ):
            res_sse = await client_a.get('/support/chat/events/')
            self.assertEqual(res_sse.status_code, 200)
            stream = res_sse.streaming_content
            try:
                await next_sse_frame(stream)  # retry

                res_reset = await client_a.post('/support/chat/reset/')
                self.assertEqual(res_reset.status_code, 200)

                frames = await collect_event_frames(stream, 1)
                self.assertEqual(frames[0]['event'], 'session_resolved')
                self.assertEqual(frames[0]['payload']['data']['reason'], 'reset')
                self.assertTrue(
                    any(
                        ch == realtime.DESK_QUEUE_CHANNEL
                        and payload.get('type') == 'queue_update'
                        and payload.get('data', {}).get('reason') == 'reset'
                        for ch, payload in broker.published
                    )
                )
            finally:
                await stream.aclose()

    async def test_out_of_order_concurrent_commits_are_both_delivered_without_duplicates(self):
        broker = FakeRedisBroker()
        session = await sync_to_async(
            lambda: ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
                assigned_operator=self.operator,
            ),
            thread_sensitive=True,
        )()

        def create_pair():
            m_earlier = ChatMessage.objects.create(
                session=session,
                role=MessageRole.OPERATOR,
                sender_user=self.operator,
                content='Раніше вставлене (id менший)',
            )
            m_later = ChatMessage.objects.create(
                session=session,
                role=MessageRole.USER,
                content='Пізніше вставлене (id більший)',
            )
            return m_earlier, m_later

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=broker.async_client,
            ),
        ):
            gen = realtime.subscribe_client_session_events(
                session.session_uuid,
                heartbeat_interval=5.0,
                poll_interval=0.05,
            )
            try:
                await next_sse_frame(gen)  # retry

                m_earlier, m_later = await sync_to_async(
                    create_pair, thread_sensitive=True
                )()

                # Simulate out-of-order Redis publish: higher id arrives BEFORE lower id,
                # followed by a duplicate of higher id.
                realtime.publish_session_event(
                    session.session_uuid,
                    'message',
                    realtime.build_message_event_data(m_later, session=session),
                    event_id=m_later.id,
                )
                realtime.publish_session_event(
                    session.session_uuid,
                    'message',
                    realtime.build_message_event_data(m_earlier, session=session),
                    event_id=m_earlier.id,
                )
                realtime.publish_session_event(
                    session.session_uuid,
                    'message',
                    realtime.build_message_event_data(m_later, session=session),
                    event_id=m_later.id,
                )

                frames = await collect_event_frames(gen, 2)
                delivered_ids = [int(f['id']) for f in frames]
                self.assertEqual(delivered_ids, [m_later.id, m_earlier.id])

                # Confirm duplicate m_later was dropped (nothing more queued)
                with self.assertRaises(TimeoutError):
                    await next_sse_frame(gen, timeout=0.15)
            finally:
                await gen.aclose()

    async def test_connect_time_redis_failure_falls_back_to_db_polling_and_closes_client(self):
        broker = FakeRedisBroker()
        session = await sync_to_async(
            lambda: ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
                assigned_operator=self.operator,
            ),
            thread_sensitive=True,
        )()

        def failing_connect_client(url: str) -> FakeAsyncRedisClient:
            return broker.async_client(
                url,
                fail_subscribe=redis.exceptions.ConnectionError('Redis down on connect'),
            )

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=failing_connect_client,
            ),
        ):
            gen = realtime.subscribe_client_session_events(
                session.session_uuid,
                heartbeat_interval=5.0,
                poll_interval=0.03,
            )
            try:
                await next_sse_frame(gen)  # retry

                self.assertEqual(len(broker.created_async_clients), 1)
                self.assertTrue(broker.created_async_clients[0].closed)
                self.assertTrue(broker.created_async_clients[0].created_pubsubs[0].closed)

                await sync_to_async(
                    SupportDeskService.send_operator_message, thread_sensitive=True
                )(session.session_uuid, self.operator, 'Доставлено через DB fallback')

                frames = await collect_event_frames(gen, 1)
                self.assertEqual(
                    frames[0]['payload']['data']['content'],
                    'Доставлено через DB fallback',
                )
            finally:
                await gen.aclose()

    async def test_ai_chat_save_message_publishes_to_desk_sse_and_skips_tool_messages(self):
        from support.views import _save_message

        broker = FakeRedisBroker()
        session = await sync_to_async(
            lambda: ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ACTIVE,
                is_escalated=False,
            ),
            thread_sensitive=True,
        )()

        desk_client = AsyncClient()
        await desk_client.aforce_login(self.operator)

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=broker.async_client,
            ),
        ):
            response = await desk_client.get(
                f'/support/desk/events/?session_uuid={session.session_uuid}'
            )
            self.assertEqual(response.status_code, 200)
            stream = response.streaming_content
            try:
                await next_sse_frame(stream)  # retry

                # Tool messages must NEVER be published
                await sync_to_async(_save_message, thread_sensitive=True)(
                    session.session_uuid,
                    MessageRole.TOOL,
                    '{"tool": "secret"}',
                )
                self.assertEqual(broker.published, [])

                # Assistant reply publishes message + queue_update for desk
                await sync_to_async(_save_message, thread_sensitive=True)(
                    session.session_uuid,
                    MessageRole.ASSISTANT,
                    'Раджу спробувати піцу Маргарита!',
                )
                frames = await collect_event_frames(stream, 2)
                self.assertEqual({f['event'] for f in frames}, {'message', 'queue_update'})
                ai_msg = next(f for f in frames if f['event'] == 'message')
                self.assertEqual(ai_msg['payload']['data']['sender'], 'ai')
                self.assertEqual(
                    ai_msg['payload']['data']['content'],
                    'Раджу спробувати піцу Маргарита!',
                )
            finally:
                await stream.aclose()

    async def test_connect_and_midstream_errors_skip_unsubscribe_and_cancel_during_subscribe_closes_client(self):
        broker = FakeRedisBroker()
        session = await sync_to_async(
            lambda: ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
                assigned_operator=self.operator,
            ),
            thread_sensitive=True,
        )()

        # 1. Connect failure must NOT call unsubscribe() (avoids 2s reconnect stall)
        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=lambda url: broker.async_client(
                    url,
                    fail_subscribe=redis.exceptions.ConnectionError('Down on connect'),
                ),
            ),
        ):
            client_res, pubsub_res = await realtime._open_redis_pubsub(
                [realtime.session_channel(session.session_uuid)]
            )
            self.assertIsNone(client_res)
            self.assertIsNone(pubsub_res)
            c0 = broker.created_async_clients[-1]
            self.assertTrue(c0.closed)
            self.assertTrue(c0.created_pubsubs[0].closed)
            self.assertFalse(c0.created_pubsubs[0].unsubscribed)

        # 2. Mid-stream Redis disconnect must NOT call unsubscribe()
        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=lambda url: broker.async_client(
                    url,
                    fail_after_messages=0,
                    fail_exception=redis.exceptions.ConnectionError('Mid-stream drop'),
                ),
            ),
        ):
            gen = realtime.subscribe_client_session_events(
                session.session_uuid,
                heartbeat_interval=5.0,
                poll_interval=0.03,
            )
            try:
                await next_sse_frame(gen)  # retry
                await sync_to_async(
                    SupportDeskService.send_operator_message, thread_sensitive=True
                )(session.session_uuid, self.operator, 'Через DB після обриву')
                frames = await collect_event_frames(gen, 1)
                self.assertEqual(
                    frames[0]['payload']['data']['content'],
                    'Через DB після обриву',
                )
                c1 = broker.created_async_clients[-1]
                self.assertTrue(c1.closed)
                self.assertTrue(c1.created_pubsubs[0].closed)
                self.assertFalse(c1.created_pubsubs[0].unsubscribed)
            finally:
                await gen.aclose()

        # 3. CancelledError raised while awaiting subscribe() must close client/pubsub (no leak)
        started_subscribe = asyncio.Event()

        class HangingPubSub(FakeAsyncPubSub):
            async def subscribe(self, *channels: str) -> None:
                started_subscribe.set()
                await asyncio.sleep(10.0)

        class HangingRedisClient(FakeAsyncRedisClient):
            def pubsub(self) -> HangingPubSub:
                ps = HangingPubSub(self.broker)
                self.created_pubsubs.append(ps)
                return ps

        hanging_client = HangingRedisClient(broker)
        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch(
                'support.realtime._create_async_redis_client',
                return_value=hanging_client,
            ),
        ):
            task = asyncio.create_task(
                realtime._open_redis_pubsub([realtime.session_channel(session.session_uuid)])
            )
            await asyncio.wait_for(started_subscribe.wait(), timeout=2.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(hanging_client.closed)
            self.assertEqual(len(hanging_client.created_pubsubs), 1)
            self.assertTrue(hanging_client.created_pubsubs[0].closed)
            self.assertFalse(hanging_client.created_pubsubs[0].unsubscribed)

    async def test_out_of_order_concurrent_commit_across_catchup_is_delivered(self):
        broker = FakeRedisBroker()

        def setup_messages():
            session = ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
                assigned_operator=self.operator,
            )
            m1 = ChatMessage.objects.create(
                session=session,
                role=MessageRole.USER,
                content='До перепідключення',
            )
            m2 = ChatMessage.objects.create(
                session=session,
                role=MessageRole.OPERATOR,
                sender_user=self.operator,
                content='Конкурентний коміт (id менший)',
            )
            m3 = ChatMessage.objects.create(
                session=session,
                role=MessageRole.USER,
                content='Видимий у catch-up (id більший)',
            )
            return session, m1, m2, m3

        session, m1, m2, m3 = await sync_to_async(setup_messages, thread_sensitive=True)()
        m2_payload = await sync_to_async(
            lambda: realtime.build_message_event_data(m2, session=session),
            thread_sensitive=True,
        )()
        m3_payload = await sync_to_async(
            lambda: realtime.build_message_event_data(m3, session=session),
            thread_sensitive=True,
        )()

        # Simulate DB catchup seeing ONLY m3 (while m2's transaction commits slightly later)
        original_snapshot = realtime._fetch_session_snapshot_sync

        def snapshot_missing_m2(sid, *, after_id, include_messages):
            snap = original_snapshot(sid, after_id=after_id, include_messages=include_messages)
            if snap is not None:
                snap['messages'] = [m for m in snap['messages'] if m['id'] != m2.id]
            return snap

        with (
            override_settings(SUPPORT_REALTIME_REDIS_URL='redis://fake-broker:6379/0'),
            patch('support.realtime._get_sync_redis_client', side_effect=broker.sync_client),
            patch(
                'support.realtime._create_async_redis_client',
                side_effect=broker.async_client,
            ),
            patch(
                'support.realtime._fetch_session_snapshot_sync',
                side_effect=snapshot_missing_m2,
            ),
        ):
            gen = realtime.subscribe_client_session_events(
                session.session_uuid,
                after_id=m1.id,
                heartbeat_interval=5.0,
                poll_interval=0.05,
            )
            try:
                await next_sse_frame(gen)  # retry
                catchup = await collect_event_frames(gen, 2)
                self.assertEqual(catchup[0]['event'], 'message')
                self.assertEqual(catchup[0]['id'], str(m3.id))
                self.assertEqual(catchup[1]['event'], 'status_changed')

                # Duplicate m3 MUST be dropped, while delayed m2 (m1.id < m2.id < m3.id) MUST be delivered!
                realtime.publish_session_event(
                    session.session_uuid, 'message', m3_payload, event_id=m3.id
                )
                realtime.publish_session_event(
                    session.session_uuid, 'message', m2_payload, event_id=m2.id
                )
                live = await collect_event_frames(gen, 1)
                self.assertEqual(live[0]['event'], 'message')
                self.assertEqual(live[0]['id'], str(m2.id))
                self.assertEqual(
                    live[0]['payload']['data']['content'],
                    'Конкурентний коміт (id менший)',
                )
            finally:
                await gen.aclose()

    async def test_db_polling_fallback_emits_reset_reason_on_multi_tab_reset(self):
        client_a = AsyncClient()
        await client_a.aforce_login(self.customer_a)
        res_esc = await client_a.post(
            '/support/chat/escalate/',
            data=json.dumps({}),
            content_type='application/json',
        )
        self.assertEqual(res_esc.status_code, 200)
        old_uuid = uuid.UUID(res_esc.json()['session_uuid'])

        with override_settings(SUPPORT_REALTIME_REDIS_URL=''):
            gen = realtime.subscribe_client_session_events(
                old_uuid,
                heartbeat_interval=5.0,
                poll_interval=0.03,
            )
            try:
                await next_sse_frame(gen)  # retry
                res_reset = await client_a.post('/support/chat/reset/')
                self.assertEqual(res_reset.status_code, 200)

                frames = await collect_event_frames(gen, 1)
                self.assertEqual(frames[0]['event'], 'session_resolved')
                self.assertEqual(frames[0]['payload']['data']['reason'], 'reset')
            finally:
                await gen.aclose()

    async def test_real_redis_asyncio_tcp_resp3_wire_protocol_and_fast_drop_fallback(self):
        """Exercise real redis.Redis + redis.asyncio 8.1.0 TCP RESP3 wire framing and abrupt disconnect."""
        import time as _time

        def encode_resp(val: Any, *, push: bool = False) -> bytes:
            if isinstance(val, int):
                return f':{val}\r\n'.encode()
            if isinstance(val, str):
                b = val.encode('utf-8')
                return f'${len(b)}\r\n'.encode() + b + b'\r\n'
            if isinstance(val, list):
                prefix = '>' if push else '*'
                return f'{prefix}{len(val)}\r\n'.encode() + b''.join(
                    encode_resp(x) for x in val
                )
            if isinstance(val, dict):
                return f'%{len(val)}\r\n'.encode() + b''.join(
                    encode_resp(k) + encode_resp(v) for k, v in val.items()
                )
            raise TypeError(type(val))

        async def read_cmd(reader: asyncio.StreamReader) -> list[str] | None:
            line = await reader.readline()
            if not line:
                return None
            count = int(line[1:].strip())
            parts: list[str] = []
            for _ in range(count):
                hdr = await reader.readline()
                length = int(hdr[1:].strip())
                raw = await reader.readexactly(length)
                await reader.readexactly(2)
                parts.append(raw.decode('utf-8'))
            return parts

        subs: dict[str, set[asyncio.StreamWriter]] = defaultdict(set)
        writers: set[asyncio.StreamWriter] = set()

        async def handle_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            writers.add(writer)
            my_channels: set[str] = set()
            try:
                while True:
                    cmd = await read_cmd(reader)
                    if cmd is None:
                        break
                    op = cmd[0].upper()
                    if op == 'HELLO':
                        proto = int(cmd[1]) if len(cmd) > 1 else 2
                        writer.write(
                            encode_resp({
                                'server': 'redis',
                                'version': '7.2.0',
                                'proto': proto,
                                'id': 1,
                                'mode': 'standalone',
                                'role': 'master',
                                'modules': [],
                            })
                        )
                    elif op == 'PING':
                        if my_channels:
                            arg = cmd[1] if len(cmd) > 1 else ''
                            writer.write(encode_resp(['pong', arg]))
                        else:
                            writer.write(b'+PONG\r\n')
                    elif op == 'SUBSCRIBE':
                        for ch in cmd[1:]:
                            my_channels.add(ch)
                            subs[ch].add(writer)
                            writer.write(
                                encode_resp(['subscribe', ch, len(my_channels)], push=True)
                            )
                    elif op == 'UNSUBSCRIBE':
                        targets = cmd[1:] or list(my_channels)
                        for ch in targets:
                            my_channels.discard(ch)
                            subs[ch].discard(writer)
                            writer.write(
                                encode_resp(
                                    ['unsubscribe', ch, len(my_channels)], push=True
                                )
                            )
                    elif op == 'PUBLISH':
                        ch, payload = cmd[1], cmd[2]
                        receivers = list(subs.get(ch, ()))
                        for w in receivers:
                            w.write(encode_resp(['message', ch, payload], push=True))
                            await w.drain()
                        writer.write(encode_resp(len(receivers)))
                    else:
                        writer.write(b'+OK\r\n')
                    await writer.drain()
            except (OSError, asyncio.IncompleteReadError, ValueError):
                return
            finally:
                for ch in my_channels:
                    subs[ch].discard(writer)
                writers.discard(writer)
                writer.close()

        server = await asyncio.start_server(handle_client, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        redis_url = f'redis://127.0.0.1:{port}/0'

        session = await sync_to_async(
            lambda: ChatSession.objects.create(
                user=self.customer_a,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
                assigned_operator=self.operator,
            ),
            thread_sensitive=True,
        )()

        try:
            with override_settings(SUPPORT_REALTIME_REDIS_URL=redis_url):
                gen = realtime.subscribe_client_session_events(
                    session.session_uuid,
                    heartbeat_interval=5.0,
                    poll_interval=0.03,
                )
                try:
                    retry_frame = await next_sse_frame(gen)
                    self.assertEqual(retry_frame['kind'], 'retry')

                    # 1. Real sync Redis PUBLISH -> real redis.asyncio PubSub -> SSE frame
                    await sync_to_async(
                        SupportDeskService.send_operator_message, thread_sensitive=True
                    )(session.session_uuid, self.operator, 'Реальний TCP Redis RESP3')
                    f1 = await collect_event_frames(gen, 1)
                    self.assertEqual(
                        f1[0]['payload']['data']['content'],
                        'Реальний TCP Redis RESP3',
                    )
                    self.assertNotIn('operator_name', f1[0]['payload']['data'])

                    # 2. Abruptly kill the TCP Redis server mid-stream -> verify seamless DB fallback
                    # without an extra 2s reconnect stall in _close_redis_resources!
                    server.close()
                    for w in list(writers):
                        w.close()
                    await server.wait_closed()

                    await sync_to_async(
                        SupportDeskService.send_operator_message, thread_sensitive=True
                    )(session.session_uuid, self.operator, 'Після падіння TCP Redis')
                    t_drop = _time.monotonic()
                    f2 = await collect_event_frames(gen, 1, timeout=4.5)
                    elapsed = _time.monotonic() - t_drop
                    self.assertEqual(
                        f2[0]['payload']['data']['content'],
                        'Після падіння TCP Redis',
                    )
                    # Without unsubscribe=False, _close_redis_resources stalls an extra +2.0s (~4.1s total)
                    self.assertLess(elapsed, 3.2)
                finally:
                    await gen.aclose()
        finally:
            server.close()
            for w in list(writers):
                w.close()
            realtime.reset_realtime_clients()


class FrontendEventSourceJsBehaviorTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.node_bin = shutil.which('node')
        factory = RequestFactory()
        req = factory.get('/')
        req.user = AnonymousUser()
        cls.widget_html = render_to_string(
            'support/chat_widget.html',
            {'cart_items_count': 0, 'csp_nonce': 'test-nonce'},
            request=req,
        )
        widget_match = re.search(
            r'id="ai-chat-widget"\s+x-data="(\{[\s\S]*?\n {2}\})"',
            cls.widget_html,
        )
        if not widget_match:
            raise ValueError('Could not extract x-data expression from chat_widget.html')
        cls.widget_xdata = widget_match.group(1)

        desk_src = render_to_string(
            'support/desk/index.html',
            {
                'queue': {
                    'tab': 'waiting',
                    'query': '',
                    'counts': {'waiting': 0, 'in_progress': 0, 'ai': 0, 'resolved': 0},
                    'items': [],
                },
                'thread': None,
                'active_session_uuid': '',
                'csp_nonce': 'test-nonce',
            },
            request=req,
        )
        desk_match = re.search(
            r'function supportDeskController\(initial\) \{[\s\S]*?\n\}',
            desk_src,
        )
        if not desk_match:
            raise ValueError('Could not extract supportDeskController from desk/index.html')
        cls.desk_controller_src = desk_match.group(0)

    def _run_node_script(self, script: str) -> dict[str, Any]:
        if not self.node_bin:
            self.fail('Node.js executable is required to verify SSE client JS behavior.')
        proc = subprocess.run(  # noqa: S603
            [self.node_bin, '-'],
            input=script,
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
        )
        return json.loads(proc.stdout)

    def test_chat_widget_eventsource_realtime_dedup_and_polling_fallback(self):
        script = f"""
        const vm = require('vm');
        const timers = new Map();
        let nextTimerId = 1;
        const listeners = {{}};
        const doc = {{
          hidden: false,
          cookie: 'csrftoken=test-csrf',
          querySelector: () => null,
          getElementById: () => null,
          addEventListener: (ev, fn) => {{ (listeners[ev] ||= []).push(fn); }},
          removeEventListener: (ev, fn) => {{
            listeners[ev] = (listeners[ev] || []).filter(f => f !== fn);
          }},
          dispatchVisibility: (hiddenVal) => {{
            doc.hidden = hiddenVal;
            for (const fn of (listeners.visibilitychange || [])) fn();
          }},
        }};

        const instances = [];
        class FakeEventSource {{
          static CONNECTING = 0;
          static OPEN = 1;
          static CLOSED = 2;
          constructor(url) {{
            this.url = url;
            this.readyState = FakeEventSource.CONNECTING;
            this.closed = false;
            this.handlers = {{}};
            this.onopen = null;
            this.onerror = null;
            instances.push(this);
          }}
          addEventListener(type, fn) {{
            (this.handlers[type] ||= []).push(fn);
          }}
          emitOpen() {{
            this.readyState = FakeEventSource.OPEN;
            if (this.onopen) this.onopen({{}});
          }}
          emit(type, payload) {{
            for (const fn of (this.handlers[type] || [])) {{
              fn({{ data: JSON.stringify(payload) }});
            }}
          }}
          emitError(readyState = FakeEventSource.CONNECTING) {{
            this.readyState = readyState;
            if (this.onerror) this.onerror({{}});
          }}
          close() {{
            this.closed = true;
            this.readyState = FakeEventSource.CLOSED;
          }}
        }}

        let resolvePostMsg = null;
        const fetchImpl = (url) => {{
          if (url === '/support/chat/message/') {{
            return new Promise((resolve) => {{
              resolvePostMsg = () => resolve({{
                ok: true,
                status: 201,
                json: async () => ({{
                  message: {{ id: 55, role: 'user', sender: 'user', content: 'Тест гонки', created_at: '14:30' }},
                  is_working_hours: true,
                }}),
              }});
            }});
          }}
          if (url === '/support/chat/history/') {{
            return Promise.resolve({{
              ok: true,
              status: 200,
              json: async () => ({{
                session_uuid: '22222222-2222-2222-2222-222222222222',
                status: 'ACTIVE',
                is_escalated: false,
                operator_connected: false,
                is_working_hours: true,
                working_hours_label: '10:00–22:00',
                last_message_id: 0,
                messages: [],
              }}),
            }});
          }}
          throw new Error('Unexpected fetch: ' + url);
        }};

        const sandbox = {{
          document: doc,
          window: {{ htmx: null, EventSource: FakeEventSource }},
          EventSource: FakeEventSource,
          fetch: fetchImpl,
          AbortController,
          TextDecoder,
          encodeURIComponent,
          decodeURIComponent,
          JSON,
          Math,
          Array,
          Boolean,
          String,
          Number,
          Date,
          setTimeout: (fn, ms) => {{
            const id = nextTimerId++;
            timers.set(id, {{ fn, ms }});
            return id;
          }},
          clearTimeout: (id) => {{ timers.delete(id); }},
        }};

        (async () => {{
          const widget = vm.runInNewContext('(' + {json.dumps(self.widget_xdata)} + ')', sandbox);
          widget.$nextTick = (cb) => cb();
          widget.$refs = {{ msgContainer: {{ scrollTop: 0, scrollHeight: 100 }} }};

          widget.open = true;
          widget.sessionUuid = '11111111-1111-1111-1111-111111111111';
          widget.status = 'ESCALATED';
          widget.isEscalated = true;
          widget.lastMessageId = 50;

          widget.startRealtime(false);
          const es1 = instances[0];
          es1.emitOpen();
          const timersWhileSseOpen = timers.size;

          // Operator joins and replies via SSE
          es1.emit('operator_joined', {{
            type: 'operator_joined',
            session_uuid: widget.sessionUuid,
            data: {{ status: 'ESCALATED', is_escalated: true, operator_connected: true }},
          }});
          es1.emit('message', {{
            type: 'message',
            session_uuid: widget.sessionUuid,
            event_id: 51,
            data: {{
              id: 51,
              role: 'operator',
              sender: 'operator',
              sender_label: 'Адміністратор ресторану',
              content: 'Вітаю!',
              created_at: '14:29',
            }},
          }});

          // Race condition: SSE delivers user message id=55 BEFORE POST /chat/message/ resolves
          widget.input = 'Тест гонки';
          const sendPromise = widget.sendMessage();
          es1.emit('message', {{
            type: 'message',
            session_uuid: widget.sessionUuid,
            event_id: 55,
            data: {{ id: 55, role: 'user', sender: 'user', content: 'Тест гонки', created_at: '14:30' }},
          }});
          resolvePostMsg();
          await sendPromise;
          const countMsg55 = widget.messages.filter(m => m.id === 55 || m.content === 'Тест гонки').length;

          // Transient SSE drop -> fallback polling arms immediately, then stops when SSE re-opens!
          es1.emitError(FakeEventSource.CONNECTING);
          const pollingArmedOnTransientError = Array.from(timers.values()).some(t => t.ms === 3000);
          es1.emitOpen();
          const timersAfterRecovery = timers.size;

          // WSGI 503 / CLOSED -> closes EventSource, arms polling + backoff reconnect timer
          es1.emitError(FakeEventSource.CLOSED);
          const closedAfter503 = es1.closed;
          const pollingArmedOn503 = Array.from(timers.values()).some(t => t.ms === 3000);
          const reconnectScheduled = Array.from(timers.values()).some(t => t.ms === 10000);

          // Trigger reconnect timer -> creates es2 with updated after_id=55
          const reconnectEntry = Array.from(timers.entries()).find(([, v]) => v.ms === 10000);
          timers.delete(reconnectEntry[0]);
          reconnectEntry[1].fn();
          const es2 = instances[1];
          es2.emitOpen();

          // session_resolved -> closes SSE cleanly & stops polling
          es2.emit('session_resolved', {{
            type: 'session_resolved',
            session_uuid: widget.sessionUuid,
            data: {{ status: 'RESOLVED', is_escalated: false, operator_connected: false, reason: 'resolved' }},
          }});

          process.stdout.write(JSON.stringify({{
            es1Url: es1.url,
            es2Url: es2.url,
            timersWhileSseOpen,
            operatorConnectedBeforeResolve: true,
            countMsg55,
            pollingArmedOnTransientError,
            timersAfterRecovery,
            closedAfter503,
            pollingArmedOn503,
            reconnectScheduled,
            es2ClosedAfterResolve: es2.closed,
            finalStatus: widget.status,
            finalTimers: timers.size,
            msgIds: widget.messages.map(m => m.id),
          }}));
        }})().catch(err => {{ console.error(err); process.exit(1); }});
        """
        result = self._run_node_script(script)
        self.assertEqual(result['es1Url'], '/support/chat/events/?after_id=50')
        self.assertEqual(result['es2Url'], '/support/chat/events/?after_id=55')
        self.assertEqual(result['timersWhileSseOpen'], 0)
        self.assertEqual(result['countMsg55'], 1)
        self.assertTrue(result['pollingArmedOnTransientError'])
        self.assertEqual(result['timersAfterRecovery'], 0)
        self.assertTrue(result['closedAfter503'])
        self.assertTrue(result['pollingArmedOn503'])
        self.assertTrue(result['reconnectScheduled'])
        self.assertTrue(result['es2ClosedAfterResolve'])
        self.assertEqual(result['finalStatus'], 'RESOLVED')
        self.assertEqual(result['finalTimers'], 0)
        self.assertEqual(result['msgIds'], [51, 55])

    def test_desk_controller_eventsource_queue_debounce_session_switch_and_fallback(self):
        script = f"""
        const vm = require('vm');
        const timers = new Map();
        let nextTimerId = 1;
        const docListeners = {{}};
        const bodyListeners = {{}};
        const htmxCalls = [];
        const appendedNodes = [];

        function makeEl(tag) {{
          return {{
            tagName: tag,
            className: '',
            textContent: '',
            attrs: {{}},
            children: [],
            setAttribute(k, v) {{ this.attrs[k] = v; }},
            getAttribute(k) {{ return this.attrs[k] || null; }},
            appendChild(child) {{ this.children.push(child); return child; }},
            insertBefore(child) {{ this.children.push(child); return child; }},
            remove() {{}},
          }};
        }}

        const msgContainer = {{
          scrollTop: 0,
          scrollHeight: 500,
          querySelector(sel) {{
            const m = sel.match(/data-message-id="(\\d+)"/);
            if (m) {{
              return appendedNodes.find(n => n.getAttribute('data-message-id') === m[1]) || null;
            }}
            return null;
          }},
          querySelectorAll() {{ return appendedNodes; }},
          appendChild(node) {{ appendedNodes.push(node); return node; }},
          insertBefore(node) {{ appendedNodes.push(node); return node; }},
        }};

        let threadRoot = {{
          dataset: {{
            sessionUuid: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            sessionStatus: 'ESCALATED',
            assignedOperatorId: '7',
            lastMessageId: '10',
          }},
        }};

        const doc = {{
          hidden: false,
          cookie: 'csrftoken=desk-csrf',
          body: {{
            addEventListener: (ev, fn) => {{ (bodyListeners[ev] ||= []).push(fn); }},
            removeEventListener: (ev, fn) => {{
              bodyListeners[ev] = (bodyListeners[ev] || []).filter(f => f !== fn);
            }},
          }},
          getElementById: (id) => {{
            if (id === 'desk-thread-root') return threadRoot;
            if (id === 'desk-messages-list') return msgContainer;
            return null;
          }},
          querySelector: () => null,
          createElement: (tag) => makeEl(tag),
          createElementNS: (_ns, tag) => makeEl(tag),
          createTextNode: (txt) => ({{ textContent: txt }}),
          addEventListener: (ev, fn) => {{ (docListeners[ev] ||= []).push(fn); }},
          removeEventListener: (ev, fn) => {{
            docListeners[ev] = (docListeners[ev] || []).filter(f => f !== fn);
          }},
        }};

        const instances = [];
        class FakeEventSource {{
          static CONNECTING = 0;
          static OPEN = 1;
          static CLOSED = 2;
          constructor(url) {{
            this.url = url;
            this.readyState = FakeEventSource.CONNECTING;
            this.closed = false;
            this.handlers = {{}};
            this.onopen = null;
            this.onerror = null;
            instances.push(this);
          }}
          addEventListener(type, fn) {{
            (this.handlers[type] ||= []).push(fn);
          }}
          emitOpen() {{
            this.readyState = FakeEventSource.OPEN;
            if (this.onopen) this.onopen({{}});
          }}
          emit(type, payload) {{
            for (const fn of (this.handlers[type] || [])) {{
              fn({{ data: JSON.stringify(payload) }});
            }}
          }}
          emitError(readyState = FakeEventSource.CLOSED) {{
            this.readyState = readyState;
            if (this.onerror) this.onerror({{}});
          }}
          close() {{
            this.closed = true;
            this.readyState = FakeEventSource.CLOSED;
          }}
        }}

        const sandbox = {{
          document: doc,
          window: {{
            innerWidth: 1280,
            history: {{ replaceState: () => {{}} }},
            htmx: {{
              ajax: (method, url, opts) => htmxCalls.push({{ method, url, target: opts.target }}),
            }},
            EventSource: FakeEventSource,
          }},
          EventSource: FakeEventSource,
          URLSearchParams,
          encodeURIComponent,
          decodeURIComponent,
          JSON,
          Math,
          Array,
          Boolean,
          String,
          Number,
          setTimeout: (fn, ms) => {{
            const id = nextTimerId++;
            timers.set(id, {{ fn, ms }});
            return id;
          }},
          clearTimeout: (id) => {{ timers.delete(id); }},
        }};

        (async () => {{
          vm.runInNewContext({json.dumps(self.desk_controller_src)}, sandbox);
          const desk = sandbox.supportDeskController({{
            activeTab: 'in_progress',
            searchQuery: '',
            activeSessionUuid: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            lastMessageId: 10,
            sessionStatus: 'ESCALATED',
            assignedOperatorId: '7',
          }});
          desk.$nextTick = (cb) => cb();
          desk.init();

          const es1 = instances[0];
          es1.emitOpen();
          const timersAfterOpen = timers.size;

          // Operator message via SSE appends bubble displaying operator_name + source_badge
          es1.emit('message', {{
            type: 'message',
            session_uuid: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            event_id: 11,
            data: {{
              id: 11,
              role: 'operator',
              sender: 'operator',
              sender_label: 'Адміністратор ресторану',
              operator_name: 'Олена Коваль',
              source: 'WEB',
              source_badge: 'Web',
              content: 'Відповідь з SSE',
              created_at: '14:31',
            }},
          }});

          // Burst of 3 queue_update events collapses into 1 debounced queue refresh
          es1.emit('queue_update', {{ type: 'queue_update', data: {{ reason: 'customer_message' }} }});
          es1.emit('queue_update', {{ type: 'queue_update', data: {{ reason: 'claimed' }} }});
          es1.emit('queue_update', {{ type: 'queue_update', data: {{ reason: 'resolved' }} }});
          const queueTimers = Array.from(timers.entries()).filter(([, v]) => v.ms === 250);
          const queueRefreshesBeforeTimer = htmxCalls.filter(c => c.target === '#desk-queue').length;
          for (const [id, entry] of queueTimers) {{
            timers.delete(id);
            entry.fn();
          }}
          const queueRefreshesAfterTimer = htmxCalls.filter(c => c.target === '#desk-queue').length;

          // Switch to another session -> closes es1, afterSwap connects es2 with new session_uuid
          desk.selectSession('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb');
          const es1ClosedOnSwitch = es1.closed;
          threadRoot = {{
            dataset: {{
              sessionUuid: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
              sessionStatus: 'ESCALATED',
              assignedOperatorId: '',
              lastMessageId: '25',
            }},
          }};
          for (const fn of (bodyListeners['htmx:afterSwap'] || [])) {{
            fn({{ detail: {{ target: {{ id: 'desk-workspace' }} }} }});
          }}
          const es2 = instances[1];
          es2.emitOpen();

          // SSE error (CLOSED / WSGI 503) -> activates fallback polling (4000ms)
          es2.emitError(FakeEventSource.CLOSED);
          const fallbackPollArmed = Array.from(timers.values()).some(t => t.ms === 4000);

          // Destroy cleans up all timers and EventSource
          desk.destroy();

          function collectText(node) {{
            if (!node) return '';
            let out = node.textContent || '';
            for (const ch of (node.children || [])) out += ' ' + collectText(ch);
            return out;
          }}

          process.stdout.write(JSON.stringify({{
            es1Url: es1.url,
            es2Url: es2.url,
            timersAfterOpen,
            lastMessageIdAfterSse: desk.lastMessageId,
            appendedCount: appendedNodes.length,
            appendedText: collectText(appendedNodes[0]),
            debouncedTimerCount: queueTimers.length,
            queueRefreshesBeforeTimer,
            queueRefreshesAfterTimer,
            es1ClosedOnSwitch,
            fallbackPollArmed,
            timersAfterDestroy: timers.size,
          }}));
        }})().catch(err => {{ console.error(err); process.exit(1); }});
        """
        result = self._run_node_script(script)
        self.assertEqual(
            result['es1Url'],
            '/support/desk/events/?session_uuid=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa&after_id=10',
        )
        self.assertEqual(
            result['es2Url'],
            '/support/desk/events/?session_uuid=bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb&after_id=25',
        )
        self.assertEqual(result['timersAfterOpen'], 0)
        self.assertEqual(result['appendedCount'], 1)
        self.assertIn('Олена Коваль', result['appendedText'])
        self.assertIn('Web', result['appendedText'])
        self.assertIn('Відповідь з SSE', result['appendedText'])
        self.assertEqual(result['debouncedTimerCount'], 1)
        self.assertEqual(result['queueRefreshesBeforeTimer'], 0)
        self.assertEqual(result['queueRefreshesAfterTimer'], 1)
        self.assertTrue(result['es1ClosedOnSwitch'])
        self.assertTrue(result['fallbackPollArmed'])
        self.assertEqual(result['timersAfterDestroy'], 0)

    def test_chat_widget_active_session_live_proactive_takeover_and_ai_stream_reconciliation(self):
        script = f"""
        const vm = require('vm');
        const timers = new Map();
        let nextTimerId = 1;
        const doc = {{
          hidden: false,
          cookie: 'csrftoken=test-csrf',
          querySelector: () => null,
          getElementById: () => null,
          addEventListener: () => {{}},
          removeEventListener: () => {{}},
        }};

        const instances = [];
        class FakeEventSource {{
          static CONNECTING = 0;
          static OPEN = 1;
          static CLOSED = 2;
          constructor(url) {{
            this.url = url;
            this.readyState = FakeEventSource.CONNECTING;
            this.closed = false;
            this.handlers = {{}};
            this.onopen = null;
            this.onerror = null;
            instances.push(this);
          }}
          addEventListener(type, fn) {{ (this.handlers[type] ||= []).push(fn); }}
          emitOpen() {{
            this.readyState = FakeEventSource.OPEN;
            if (this.onopen) this.onopen({{}});
          }}
          emit(type, payload) {{
            for (const fn of (this.handlers[type] || [])) {{
              fn({{ data: JSON.stringify(payload) }});
            }}
          }}
          emitError(readyState = FakeEventSource.CONNECTING) {{
            this.readyState = readyState;
            if (this.onerror) this.onerror({{}});
          }}
          close() {{
            this.closed = true;
            this.readyState = FakeEventSource.CLOSED;
          }}
        }}

        let releaseSecondChunk = null;
        let rejectNetworkMsg = null;
        let msgPostCount = 0;
        const encoder = new TextEncoder();
        const fetchImpl = (url) => {{
          if (url === '/support/chat/stream/') {{
            const chunks = [
              encoder.encode('data: ' + JSON.stringify({{ token: 'Раджу ' }}) + '\\n\\n'),
              encoder.encode(
                'data: ' + JSON.stringify({{ token: 'тірамісу!' }}) + '\\n\\n' +
                'data: ' + JSON.stringify({{ done: true }}) + '\\n\\n'
              ),
            ];
            let step = 0;
            return Promise.resolve({{
              ok: true,
              status: 200,
              headers: {{ get: (k) => k === 'X-Chat-Session-UUID' ? '33333333-3333-3333-3333-333333333333' : null }},
              body: {{
                getReader() {{
                  return {{
                    read() {{
                      if (step === 0) {{
                        step = 1;
                        return Promise.resolve({{ done: false, value: chunks[0] }});
                      }}
                      if (step === 1) {{
                        step = 2;
                        return new Promise((resolve) => {{
                          releaseSecondChunk = () => resolve({{ done: false, value: chunks[1] }});
                        }});
                      }}
                      return Promise.resolve({{ done: true, value: undefined }});
                    }},
                  }};
                }},
              }},
            }});
          }}
          if (url === '/support/chat/message/') {{
            msgPostCount += 1;
            if (msgPostCount === 1) {{
              return Promise.resolve({{
                ok: true,
                status: 201,
                json: async () => ({{
                  message: {{ id: 105, role: 'user', sender: 'user', content: 'Перше клієнта', created_at: '15:01' }},
                  is_working_hours: true,
                }}),
              }});
            }}
            return new Promise((_resolve, reject) => {{
              rejectNetworkMsg = () => reject(new Error('Network dropped after commit'));
            }});
          }}
          throw new Error('Unexpected fetch: ' + url);
        }};

        const sandbox = {{
          document: doc,
          window: {{ htmx: null, EventSource: FakeEventSource }},
          EventSource: FakeEventSource,
          fetch: fetchImpl,
          AbortController,
          TextDecoder,
          encodeURIComponent,
          decodeURIComponent,
          JSON,
          Math,
          Array,
          Boolean,
          String,
          Number,
          Date,
          setTimeout: (fn, ms) => {{
            const id = nextTimerId++;
            timers.set(id, {{ fn, ms }});
            return id;
          }},
          clearTimeout: (id) => {{ timers.delete(id); }},
        }};

        (async () => {{
          const widget = vm.runInNewContext('(' + {json.dumps(self.widget_xdata)} + ')', sandbox);
          widget.$nextTick = (cb) => cb();
          widget.$refs = {{ msgContainer: {{ scrollTop: 0, scrollHeight: 100 }} }};

          // 1. Open ACTIVE session with existing sessionUuid -> connects SSE, NEVER polls on error
          widget.open = true;
          widget.sessionUuid = '33333333-3333-3333-3333-333333333333';
          widget.status = 'ACTIVE';
          widget.isEscalated = false;
          widget.lastMessageId = 100;
          widget.startRealtime(false);

          const es = instances[0];
          es.emitError(FakeEventSource.CONNECTING);
          const pollingOnActiveError = Array.from(timers.values()).some(t => t.ms === 3000);
          es.emitOpen();

          // 2. AI stream runs while SSE is open; SSE delivers both user + assistant messages MID-STREAM
          widget.input = 'Який десерт?';
          const aiPromise = widget.sendMessage();
          await new Promise(r => setImmediate(r));
          await new Promise(r => setImmediate(r));
          es.emit('message', {{
            type: 'message',
            session_uuid: widget.sessionUuid,
            event_id: 101,
            data: {{ id: 101, role: 'user', sender: 'user', content: 'Який десерт?', created_at: '15:00' }},
          }});
          es.emit('message', {{
            type: 'message',
            session_uuid: widget.sessionUuid,
            event_id: 102,
            data: {{ id: 102, role: 'assistant', sender: 'ai', content: 'Раджу тірамісу!', created_at: '15:00' }},
          }});
          releaseSecondChunk();
          await aiPromise;

          const aiMsgs = widget.messages.filter(m => m.role === 'assistant');
          const userMsgs101 = widget.messages.filter(m => m.role === 'user');

          // 3. Manager proactively takes over ACTIVE chat -> customer sees live switch without reload!
          es.emit('message', {{
            type: 'message',
            session_uuid: widget.sessionUuid,
            event_id: 103,
            data: {{
              id: 103,
              role: 'system',
              sender: 'system',
              content: 'До діалогу підключився адміністратор ресторану.',
              created_at: '15:00',
            }},
          }});
          es.emit('operator_joined', {{
            type: 'operator_joined',
            session_uuid: widget.sessionUuid,
            data: {{ status: 'ESCALATED', is_escalated: true, operator_connected: true }},
          }});

          // 4. sendOperatorMessage must NOT jump lastMessageId past an unseen intervening operator msg (104)
          widget.input = 'Перше клієнта';
          await widget.sendMessage();
          const lastIdAfter201 = widget.lastMessageId;

          // Intervening operator msg 104 + SSE echo of 105 arrive cleanly without duplicate
          es.emit('message', {{
            type: 'message',
            session_uuid: widget.sessionUuid,
            event_id: 104,
            data: {{ id: 104, role: 'operator', sender: 'operator', content: 'Чим допомогти?', created_at: '15:01' }},
          }});
          es.emit('message', {{
            type: 'message',
            session_uuid: widget.sessionUuid,
            event_id: 105,
            data: {{ id: 105, role: 'user', sender: 'user', content: 'Перше клієнта', created_at: '15:01' }},
          }});
          const lastIdAfterSseSync = widget.lastMessageId;

          // 5. sendOperatorMessage preserves SSE-confirmed message even if HTTP response drops!
          widget.input = 'Підтверджене через SSE';
          const p2 = widget.sendMessage();
          es.emit('message', {{
            type: 'message',
            session_uuid: widget.sessionUuid,
            event_id: 106,
            data: {{ id: 106, role: 'user', sender: 'user', content: 'Підтверджене через SSE', created_at: '15:02' }},
          }});
          rejectNetworkMsg();
          await p2;

          process.stdout.write(JSON.stringify({{
            pollingOnActiveError,
            aiMsgCount: aiMsgs.length,
            aiMsgContent: aiMsgs[0]?.content || '',
            aiMsgId: aiMsgs[0]?.id || null,
            userMsg101Count: userMsgs101.length,
            escalatedAfterTakeover: widget.isEscalated,
            operatorConnectedAfterTakeover: widget.operatorConnected,
            lastIdAfter201,
            lastIdAfterSseSync,
            hasInterveningOpMsg104: widget.messages.some(m => m.id === 104),
            keptConfirmedMsg106: widget.messages.some(m => m.id === 106 && m.content === 'Підтверджене через SSE'),
            inputAfterConfirmedDrop: widget.input,
            errorBubblesCount: widget.messages.filter(m => m.error).length,
          }}));
        }})().catch(err => {{ console.error(err); process.exit(1); }});
        """
        result = self._run_node_script(script)
        self.assertFalse(result['pollingOnActiveError'])
        self.assertEqual(result['aiMsgCount'], 1)
        self.assertEqual(result['aiMsgContent'], 'Раджу тірамісу!')
        self.assertEqual(result['aiMsgId'], 102)
        self.assertEqual(result['userMsg101Count'], 1)
        self.assertTrue(result['escalatedAfterTakeover'])
        self.assertTrue(result['operatorConnectedAfterTakeover'])
        self.assertEqual(result['lastIdAfter201'], 103)
        self.assertEqual(result['lastIdAfterSseSync'], 105)
        self.assertTrue(result['hasInterveningOpMsg104'])
        self.assertTrue(result['keptConfirmedMsg106'])
        self.assertEqual(result['inputAfterConfirmedDrop'], '')
        self.assertEqual(result['errorBubblesCount'], 0)

    def test_desk_controller_buffers_sse_during_thread_swap_marks_read_and_skips_catchup_queue_refresh(self):
        script = f"""
        const vm = require('vm');
        const timers = new Map();
        let nextTimerId = 1;
        const bodyListeners = {{}};
        const htmxCalls = [];
        const fetchCalls = [];
        const appendedNodes = [];

        function makeEl(tag) {{
          return {{
            tagName: tag,
            className: '',
            textContent: '',
            attrs: {{}},
            children: [],
            setAttribute(k, v) {{ this.attrs[k] = v; }},
            getAttribute(k) {{ return this.attrs[k] || null; }},
            appendChild(child) {{ this.children.push(child); return child; }},
            insertBefore(child) {{ this.children.push(child); return child; }},
            remove() {{}},
          }};
        }}

        const msgContainer = {{
          scrollTop: 0,
          scrollHeight: 500,
          querySelector(sel) {{
            const m = sel.match(/data-message-id="(\\d+)"/);
            if (m) {{
              return appendedNodes.find(n => n.getAttribute('data-message-id') === m[1]) || null;
            }}
            return null;
          }},
          querySelectorAll() {{ return appendedNodes; }},
          appendChild(node) {{ appendedNodes.push(node); return node; }},
          insertBefore(node) {{ appendedNodes.push(node); return node; }},
        }};

        let threadRoot = {{
          dataset: {{
            sessionUuid: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
            sessionStatus: 'ESCALATED',
            assignedOperatorId: '7',
            isOwnedByMe: '1',
            lastMessageId: '20',
          }},
        }};

        const doc = {{
          hidden: false,
          cookie: 'csrftoken=desk-csrf',
          body: {{
            addEventListener: (ev, fn) => {{ (bodyListeners[ev] ||= []).push(fn); }},
            removeEventListener: (ev, fn) => {{
              bodyListeners[ev] = (bodyListeners[ev] || []).filter(f => f !== fn);
            }},
          }},
          getElementById: (id) => {{
            if (id === 'desk-thread-root') return threadRoot;
            if (id === 'desk-messages-list') return msgContainer;
            return null;
          }},
          querySelector: () => null,
          createElement: (tag) => makeEl(tag),
          createElementNS: (_ns, tag) => makeEl(tag),
          createTextNode: (txt) => ({{ textContent: txt }}),
          addEventListener: () => {{}},
          removeEventListener: () => {{}},
        }};

        const instances = [];
        class FakeEventSource {{
          static CONNECTING = 0;
          static OPEN = 1;
          static CLOSED = 2;
          constructor(url) {{
            this.url = url;
            this.readyState = FakeEventSource.CONNECTING;
            this.closed = false;
            this.handlers = {{}};
            this.onopen = null;
            this.onerror = null;
            instances.push(this);
          }}
          addEventListener(type, fn) {{ (this.handlers[type] ||= []).push(fn); }}
          emitOpen() {{
            this.readyState = FakeEventSource.OPEN;
            if (this.onopen) this.onopen({{}});
          }}
          emit(type, payload) {{
            for (const fn of (this.handlers[type] || [])) {{
              fn({{ data: JSON.stringify(payload) }});
            }}
          }}
          close() {{
            this.closed = true;
            this.readyState = FakeEventSource.CLOSED;
          }}
        }}

        const win = {{
          innerWidth: 1280,
          history: {{ replaceState: () => {{}} }},
          htmx: {{
            ajax: (method, url, opts) => htmxCalls.push({{ method, url, target: opts.target }}),
          }},
          EventSource: FakeEventSource,
        }};

        const sandbox = {{
          document: doc,
          window: win,
          EventSource: FakeEventSource,
          fetch: (url) => {{
            fetchCalls.push(url);
            return Promise.resolve({{ ok: true, json: async () => ({{}}) }});
          }},
          URLSearchParams,
          encodeURIComponent,
          decodeURIComponent,
          JSON,
          Math,
          Array,
          Boolean,
          String,
          Number,
          setTimeout: (fn, ms) => {{
            const id = nextTimerId++;
            timers.set(id, {{ fn, ms }});
            return id;
          }},
          clearTimeout: (id) => {{ timers.delete(id); }},
        }};

        (async () => {{
          vm.runInNewContext({json.dumps(self.desk_controller_src)}, sandbox);
          const desk = sandbox.supportDeskController({{
            activeTab: 'in_progress',
            searchQuery: '',
            activeSessionUuid: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
            lastMessageId: 20,
            sessionStatus: 'ESCALATED',
            assignedOperatorId: '7',
            isOwnedByMe: true,
          }});
          desk.$nextTick = (cb) => cb();
          desk.init();
          const es = instances[0];
          es.emitOpen();

          // 1. Initial catch-up status_changed matching current DOM must NOT schedule queue refresh!
          es.emit('status_changed', {{
            type: 'status_changed',
            session_uuid: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
            data: {{ status: 'ESCALATED', assigned_operator_id: 7, reason: 'catchup' }},
          }});
          const timersAfterMatchingCatchup = timers.size;

          // 2. Customer message via SSE on visible owned thread triggers debounced mark-read fetch
          es.emit('message', {{
            type: 'message',
            session_uuid: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
            event_id: 21,
            data: {{
              id: 21,
              role: 'user',
              sender: 'user',
              sender_label: 'Анна',
              content: 'Читаєте?',
              created_at: '16:00',
            }},
          }});
          for (const [id, entry] of Array.from(timers.entries())) {{
            timers.delete(id);
            entry.fn();
          }}
          const markReadCountVisible = fetchCalls.length;
          const markReadUrl = fetchCalls[0] || '';

          // 3. On mobile queue view, customer message must NOT mark read!
          win.innerWidth = 375;
          desk.mobileView = 'queue';
          es.emit('message', {{
            type: 'message',
            session_uuid: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
            event_id: 22,
            data: {{
              id: 22,
              role: 'user',
              sender: 'user',
              sender_label: 'Анна',
              content: 'На мобільному списку',
              created_at: '16:01',
            }},
          }});
          for (const [id, entry] of Array.from(timers.entries())) {{
            timers.delete(id);
            entry.fn();
          }}
          const markReadCountOnMobileQueue = fetchCalls.length;
          win.innerWidth = 1280;
          desk.mobileView = 'chat';

          // 4. Race condition: SSE message + colleague takeover arrive WHILE threadLoading === true!
          desk.refreshThread('cccccccc-cccc-cccc-cccc-cccccccccccc');
          const threadReloadsBeforeSwap = htmxCalls.filter(c => c.target === '#desk-workspace').length;
          es.emit('message', {{
            type: 'message',
            session_uuid: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
            event_id: 23,
            data: {{
              id: 23,
              role: 'user',
              sender: 'user',
              sender_label: 'Анна',
              content: 'Під час свопу!',
              created_at: '16:02',
            }},
          }});
          es.emit('operator_joined', {{
            type: 'operator_joined',
            session_uuid: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
            data: {{ status: 'ESCALATED', assigned_operator_id: 99 }},
          }});

          // HTMX swap finishes with STALE HTML (still operator 7, lastMessageId 22)
          threadRoot.dataset.assignedOperatorId = '7';
          threadRoot.dataset.lastMessageId = '22';
          for (const fn of (bodyListeners['htmx:afterSwap'] || [])) {{
            fn({{ detail: {{ target: {{ id: 'desk-workspace' }} }} }});
          }}
          const bufferedMsg23Rendered = appendedNodes.some(n => n.getAttribute('data-message-id') === '23');
          const threadReloadsAfterStaleSwap = htmxCalls.filter(c => c.target === '#desk-workspace').length;

          desk.destroy();
          process.stdout.write(JSON.stringify({{
            timersAfterMatchingCatchup,
            markReadCountVisible,
            markReadUrl,
            markReadCountOnMobileQueue,
            bufferedMsg23Rendered,
            triggeredCorrectiveReload: threadReloadsAfterStaleSwap > threadReloadsBeforeSwap,
          }}));
        }})().catch(err => {{ console.error(err); process.exit(1); }});
        """
        result = self._run_node_script(script)
        self.assertEqual(result['timersAfterMatchingCatchup'], 0)
        self.assertEqual(result['markReadCountVisible'], 1)
        self.assertIn(
            '/support/desk/api/sessions/cccccccc-cccc-cccc-cccc-cccccccccccc/messages/?after_id=21',
            result['markReadUrl'],
        )
        self.assertEqual(result['markReadCountOnMobileQueue'], 1)
        self.assertTrue(result['bufferedMsg23Rendered'])
        self.assertTrue(result['triggeredCorrectiveReload'])
