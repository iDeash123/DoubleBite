import asyncio
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.db import close_old_connections, connections
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
from django.utils import timezone
from orders.middleware import GUEST_TOKEN_SESSION_KEY

from support.models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    MessageRole,
    SupportTicket,
    TicketReason,
)
from support.services import ChatSessionService
from support.views import (
    _close_thread_connections,
    _get_history,
    _iter_async_gen_sync,
    _iter_async_gen_threaded,
    _save_message,
    chat_stream_view,
)

User = get_user_model()


async def mock_async_stream_tokens(*args, **kwargs):
    yield {'token': 'Привіт! '}
    yield {'token': 'Як справи?'}


async def mock_async_stream_escalate(*args, **kwargs):
    yield {'escalated': True, 'ticket_id': 101, 'support_phone': '+380 44 123 45 67'}
    yield {'token': 'Передано адміністратору.'}


async def mock_async_stream_cart(*args, **kwargs):
    yield {'cart_mutation': True, 'cart_items_count': 3, 'total_amount': 650.0}
    yield {'token': 'Страву додано до вашого кошика!'}


class TrackingCancelledStream:
    def __init__(self):
        self.closed = False
        self._step = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._step == 0:
            self._step += 1
            return {'token': 'Часткова відповідь '}
        raise asyncio.CancelledError()

    async def aclose(self):
        self.closed = True


class SupportViewsTest(TestCase):
    def setUp(self):
        cache.clear()
        self.sync_client = Client()
        self.async_client = AsyncClient()
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            email='view_test@example.com',
            password='password123',
            phone='+380501234567',
        )
        self.other_user = User.objects.create_user(
            email='other_view_test@example.com',
            password='password123',
        )

    def test_stream_view_get_method_not_allowed(self):
        response = self.sync_client.get('/support/chat/stream/')
        self.assertEqual(response.status_code, 405)

    def test_cart_badge_get_allowed_and_post_not_allowed(self):
        badge_url = reverse('orders:cart_badge')
        self.assertEqual(badge_url, '/orders/cart/badge/')
        get_resp = self.sync_client.get(badge_url)
        self.assertEqual(get_resp.status_code, 200)
        post_resp = self.sync_client.post(badge_url)
        self.assertEqual(post_resp.status_code, 405)

    async def test_stream_view_invalid_json(self):
        response = await self.async_client.post(
            '/support/chat/stream/',
            data='invalid-json',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content.decode('utf-8'))
        self.assertIn('error', data)

    async def test_stream_view_empty_message(self):
        response = await self.async_client.post(
            '/support/chat/stream/',
            data=json.dumps({'message': '   '}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    @patch(
        'support.agent.client.MistralSupportAgent.stream_chat_response',
        side_effect=mock_async_stream_tokens,
    )
    async def test_stream_view_success_streaming_and_db_persistence(self, mock_stream):
        await self.async_client.aforce_login(self.user)
        response = await self.async_client.post(
            '/support/chat/stream/',
            data=json.dumps({'message': 'Привіт, бот!'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/event-stream')

        chunks = []
        async for chunk in response.streaming_content:
            chunks.append(chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk)

        joined_chunks = ''.join(chunks)
        self.assertIn('data: {"token": "Привіт! "}', joined_chunks)
        self.assertIn('data: {"token": "Як справи?"}', joined_chunks)
        self.assertIn('data: {"done": true}', joined_chunks)

        def verify_persisted_messages():
            session = ChatSession.objects.filter(
                user=self.user, status=ChatSessionStatus.ACTIVE
            ).first()
            self.assertIsNotNone(session)
            user_msg = session.messages.filter(role=MessageRole.USER).first()
            self.assertEqual(user_msg.content, 'Привіт, бот!')
            assistant_msg = session.messages.filter(role=MessageRole.ASSISTANT).first()
            self.assertEqual(assistant_msg.content, 'Привіт! Як справи?')

        await sync_to_async(verify_persisted_messages, thread_sensitive=True)()

    @patch('support.agent.client.MistralSupportAgent.stream_chat_response')
    async def test_escalated_session_returns_409_and_blocks_mistral(self, mock_stream):
        def create_escalated_session():
            return ChatSession.objects.create(
                user=self.user,
                status=ChatSessionStatus.ESCALATED,
                is_escalated=True,
                escalation_reason='Клієнт попросив оператора',
            )

        session = await sync_to_async(create_escalated_session, thread_sensitive=True)()
        await self.async_client.aforce_login(self.user)

        response = await self.async_client.post(
            '/support/chat/stream/',
            data=json.dumps({'message': 'Гей, боте!'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 409)
        data = json.loads(response.content.decode('utf-8'))
        self.assertTrue(data['is_escalated'])
        self.assertEqual(data['status'], ChatSessionStatus.ESCALATED)
        mock_stream.assert_not_called()

        msg_count = await sync_to_async(
            lambda: ChatMessage.objects.filter(session=session).count(),
            thread_sensitive=True,
        )()
        self.assertEqual(msg_count, 0)

    @patch(
        'support.agent.client.MistralSupportAgent.stream_chat_response',
        side_effect=mock_async_stream_escalate,
    )
    async def test_stream_view_escalation_event(self, mock_stream):
        await self.async_client.aforce_login(self.user)
        response = await self.async_client.post(
            '/support/chat/stream/',
            data=json.dumps({'message': 'Поклич людину!'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)

        chunks = []
        async for chunk in response.streaming_content:
            chunks.append(chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk)

        joined = ''.join(chunks)
        self.assertIn('escalated', joined)
        self.assertIn('101', joined)

    @patch(
        'support.agent.client.MistralSupportAgent.stream_chat_response',
        side_effect=mock_async_stream_cart,
    )
    async def test_stream_view_cart_mutation_event(self, mock_stream):
        await self.async_client.aforce_login(self.user)
        response = await self.async_client.post(
            '/support/chat/stream/',
            data=json.dumps({'message': 'Додай страву'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)

        chunks = []
        async for chunk in response.streaming_content:
            chunks.append(chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk)

        joined = ''.join(chunks)
        self.assertIn('cart_mutation', joined)
        self.assertIn('650.0', joined)

    @patch(
        'support.agent.client.MistralSupportAgent.stream_chat_response',
        side_effect=mock_async_stream_tokens,
    )
    async def test_stream_view_rate_limiting(self, mock_stream):
        await self.async_client.aforce_login(self.user)
        for i in range(10):
            res = await self.async_client.post(
                '/support/chat/stream/',
                data=json.dumps({'message': f'Повідомлення {i}'}),
                content_type='application/json',
            )
            self.assertEqual(res.status_code, 200)

        res_blocked = await self.async_client.post(
            '/support/chat/stream/',
            data=json.dumps({'message': 'Повідомлення 11'}),
            content_type='application/json',
        )
        self.assertEqual(res_blocked.status_code, 429)
        self.assertIn('Retry-After', res_blocked.headers)

    @override_settings(TRUST_PROXY_HEADERS=False)
    @patch(
        'support.agent.client.MistralSupportAgent.stream_chat_response',
        side_effect=mock_async_stream_tokens,
    )
    async def test_stream_view_blocks_xff_spoofing_when_trust_proxy_headers_false(
        self, mock_stream
    ):
        for i in range(10):
            res = await self.async_client.post(
                '/support/chat/stream/',
                data=json.dumps({'message': f'Спуф {i}'}),
                content_type='application/json',
                REMOTE_ADDR='127.0.0.1',
                HTTP_X_FORWARDED_FOR=f'203.0.113.{i + 1}',
            )
            self.assertEqual(res.status_code, 200)

        res_blocked = await self.async_client.post(
            '/support/chat/stream/',
            data=json.dumps({'message': 'Спуф 11'}),
            content_type='application/json',
            REMOTE_ADDR='127.0.0.1',
            HTTP_X_FORWARDED_FOR='203.0.113.250',
        )
        self.assertEqual(res_blocked.status_code, 429)
        self.assertIn('Retry-After', res_blocked.headers)

    def test_reset_view(self):
        self.sync_client.force_login(self.user)
        session = ChatSession.objects.create(user=self.user, status=ChatSessionStatus.ACTIVE)

        res = self.sync_client.post('/support/chat/reset/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'ok')

        session.refresh_from_db()
        self.assertEqual(session.status, ChatSessionStatus.RESOLVED)

        new_session = ChatSession.objects.filter(
            user=self.user, status=ChatSessionStatus.ACTIVE
        ).first()
        self.assertIsNotNone(new_session)
        self.assertEqual(str(new_session.session_uuid), data['session_uuid'])

    def test_reset_view_rate_limit_429_and_retry_after(self):
        self.sync_client.force_login(self.user)
        for _ in range(10):
            res = self.sync_client.post('/support/chat/reset/')
            self.assertEqual(res.status_code, 200)

        blocked = self.sync_client.post('/support/chat/reset/')
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('Retry-After', blocked.headers)
        data = blocked.json()
        self.assertIn('error', data)
        self.assertGreaterEqual(data['retry_after'], 1)

    def test_reset_view_get_method_not_allowed(self):
        self.sync_client.force_login(self.user)
        res = self.sync_client.get('/support/chat/reset/')
        self.assertEqual(res.status_code, 405)

    def test_history_view_and_session_isolation(self):
        self.sync_client.force_login(self.user)
        session_a = ChatSession.objects.create(user=self.user, status=ChatSessionStatus.ACTIVE)
        ChatMessage.objects.create(
            session=session_a, role=MessageRole.USER, content='Питання від А'
        )
        ChatMessage.objects.create(
            session=session_a, role=MessageRole.ASSISTANT, content='Відповідь для А'
        )

        session_b = ChatSession.objects.create(
            user=self.other_user, status=ChatSessionStatus.ACTIVE
        )
        ChatMessage.objects.create(
            session=session_b, role=MessageRole.USER, content='Питання від Б'
        )

        res_a = self.sync_client.get('/support/chat/history/')
        self.assertEqual(res_a.status_code, 200)
        data_a = res_a.json()
        self.assertEqual(len(data_a['messages']), 2)
        self.assertEqual(data_a['messages'][0]['content'], 'Питання від А')
        self.assertEqual(data_a['messages'][0]['sender'], 'user')
        self.assertEqual(data_a['messages'][1]['content'], 'Відповідь для А')
        self.assertEqual(data_a['messages'][1]['sender'], 'ai')

        self.sync_client.force_login(self.other_user)
        res_b = self.sync_client.get('/support/chat/history/')
        self.assertEqual(res_b.status_code, 200)
        data_b = res_b.json()
        self.assertEqual(len(data_b['messages']), 1)
        self.assertEqual(data_b['messages'][0]['content'], 'Питання від Б')

    def test_history_view_explicit_sender_roles_and_id_tiebreaker_ordering(self):
        self.sync_client.force_login(self.user)
        session = ChatSession.objects.create(
            user=self.user,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        fixed_ts = timezone.now()
        m1 = ChatMessage.objects.create(
            session=session, role=MessageRole.USER, content='М1 user'
        )
        m2 = ChatMessage.objects.create(
            session=session, role=MessageRole.ASSISTANT, content='М2 ai'
        )
        m_tool = ChatMessage.objects.create(
            session=session, role=MessageRole.TOOL, content='{"hidden": true}'
        )
        m3 = ChatMessage.objects.create(
            session=session, role=MessageRole.OPERATOR, content='М3 operator'
        )
        m4 = ChatMessage.objects.create(
            session=session, role=MessageRole.SYSTEM, content='М4 system'
        )
        ChatMessage.objects.filter(
            pk__in=[m1.pk, m2.pk, m_tool.pk, m3.pk, m4.pk]
        ).update(created_at=fixed_ts)

        res = self.sync_client.get('/support/chat/history/')
        self.assertEqual(res.status_code, 200)
        msgs = res.json()['messages']
        self.assertEqual([m['id'] for m in msgs], [m1.id, m2.id, m3.id, m4.id])
        self.assertEqual(
            [(m['role'], m['sender']) for m in msgs],
            [
                (MessageRole.USER, 'user'),
                (MessageRole.ASSISTANT, 'ai'),
                (MessageRole.OPERATOR, 'operator'),
                (MessageRole.SYSTEM, 'system'),
            ],
        )

    def test_get_history_returns_latest_20_messages_chronologically_and_filters_roles(self):
        session = ChatSession.objects.create(user=self.user, status=ChatSessionStatus.ACTIVE)
        ChatMessage.objects.create(
            session=session, role=MessageRole.TOOL, content='tool log'
        )
        ChatMessage.objects.create(
            session=session, role=MessageRole.SYSTEM, content='system note'
        )
        ChatMessage.objects.create(
            session=session, role=MessageRole.OPERATOR, content='operator reply'
        )
        for i in range(25):
            ChatMessage.objects.create(
                session=session,
                role=MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT,
                content=f'Message {i}',
            )

        history = _get_history(session.session_uuid)
        self.assertEqual(len(history), 20)
        self.assertEqual(history[0]['content'], 'Message 5')
        self.assertEqual(history[-1]['content'], 'Message 24')
        self.assertTrue(
            all(item['role'] in (MessageRole.USER, MessageRole.ASSISTANT) for item in history)
        )

    def test_save_message_preserves_active_atomic_transaction(self):
        session = ChatSession.objects.create(user=self.user, status=ChatSessionStatus.ACTIVE)
        with patch('support.views.close_old_connections') as mock_close:
            msg = _save_message(session.session_uuid, MessageRole.USER, 'Перевірка транзакції')
        mock_close.assert_not_called()
        self.assertEqual(msg.content, 'Перевірка транзакції')

    async def test_cancelled_error_closes_stream_and_does_not_save_partial_reply(self):
        tracking_stream = TrackingCancelledStream()
        session = await sync_to_async(
            ChatSession.objects.create, thread_sensitive=True
        )(user=self.user, status=ChatSessionStatus.ACTIVE)

        req = self.factory.post(
            '/support/chat/stream/',
            data=json.dumps({'message': 'Перерваний запит'}),
            content_type='application/json',
        )
        req.user = self.user
        req.session = {}
        req.scope = {'type': 'http'}

        with patch(
            'support.agent.client.MistralSupportAgent.stream_chat_response',
            return_value=tracking_stream,
        ):
            response = await chat_stream_view(req)
            self.assertEqual(response.status_code, 200)
            stream_iter = response.streaming_content.__aiter__()
            first_chunk = await stream_iter.__anext__()
            self.assertIn('Часткова відповідь', first_chunk.decode('utf-8'))

            with self.assertRaises(asyncio.CancelledError):
                await stream_iter.__anext__()

        self.assertTrue(tracking_stream.closed)

        def fetch_roles():
            return list(
                ChatMessage.objects.filter(session=session).values_list('role', flat=True)
            )

        roles = await sync_to_async(fetch_roles, thread_sensitive=True)()
        self.assertEqual(roles, [MessageRole.USER])

    def test_anonymous_history_view_without_session_creates_no_database_records(self):
        initial_sessions = ChatSession.objects.count()
        client = Client()
        res = client.get('/support/chat/history/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIsNone(data['session_uuid'])
        self.assertEqual(data['messages'], [])
        self.assertEqual(ChatSession.objects.count(), initial_sessions)

    @patch(
        'support.agent.client.MistralSupportAgent.stream_chat_response',
        side_effect=mock_async_stream_tokens,
    )
    async def test_stream_view_rate_limit_by_ip_across_sessions(self, mock_stream):
        for i in range(10):
            res = await self.async_client.post(
                '/support/chat/stream/',
                data=json.dumps({'message': f'Повідомлення {i}'}),
                content_type='application/json',
                HTTP_X_FORWARDED_FOR='198.51.100.42',
            )
            self.assertEqual(res.status_code, 200)

        res_blocked = await self.async_client.post(
            '/support/chat/stream/',
            data=json.dumps({'message': 'Повідомлення 11'}),
            content_type='application/json',
            HTTP_X_FORWARDED_FOR='198.51.100.42',
        )
        self.assertEqual(res_blocked.status_code, 429)

    def test_rendered_chat_widget_contracts(self):
        req = self.factory.get('/')
        req.user = AnonymousUser()
        rendered = render_to_string(
            'support/chat_widget.html',
            {'cart_items_count': 2, 'csp_nonce': 'nonce-123'},
            request=req,
        )

        self.assertIn('/orders/cart/badge/', rendered)
        self.assertNotRegex(rendered, r"(?<!/orders)/cart/badge/")

        mutation_match = re.search(
            r'if\s*\(data\.cart_mutation\)\s*\{([\s\S]*?)\n {14}\}',
            rendered,
        )
        self.assertIsNotNone(mutation_match)
        mutation_block = mutation_match.group(1)
        self.assertNotIn('hiddenByModal = true', mutation_block)
        self.assertNotIn('this.open = false', mutation_block)
        self.assertNotIn('open-cart-drawer', mutation_block)
        self.assertIn('assistantMsg.cartUpdated = true', mutation_block)
        self.assertIn('Відкрити кошик', rendered)

        self.assertIn('new AbortController()', rendered)
        self.assertIn('abortActiveStream()', rendered)

        finally_match = re.search(r'\}\s*finally\s*\{([\s\S]*?)\n {6}\}', rendered)
        self.assertIsNotNone(finally_match)
        self.assertIn('this.loading = false', finally_match.group(1))
        self.assertIn('/orders/cart/badge/', finally_match.group(1))

        self.assertIn('assistantMsg.error = data.error', rendered)
        self.assertNotIn('assistantMsg.content = data.error', rendered)

        self.assertIn('csrftoken=', rendered)
        self.assertIn("msg.sender === 'operator' ? 'Адміністратор ресторану'", rendered)
        self.assertNotIn("isEscalated ? 'Адміністратор ресторану'", rendered)

    async def test_iter_async_gen_sync_works_when_loop_already_running(self):
        async def sample_gen():
            yield 'a'
            yield 'b'

        collected = await sync_to_async(
            lambda: list(_iter_async_gen_sync(sample_gen())),
            thread_sensitive=False,
        )()
        self.assertEqual(collected, ['a', 'b'])

        collected_inside_loop = list(_iter_async_gen_sync(sample_gen()))
        self.assertEqual(collected_inside_loop, ['a', 'b'])


class SupportWsgiAndConcurrencyTest(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            email='conc_support@example.com',
            password='password123',
            phone='+380501112233',
        )

    def test_save_message_invokes_close_old_connections(self):
        session = ChatSession.objects.create(user=self.user, status=ChatSessionStatus.ACTIVE)
        with patch('support.views.close_old_connections', wraps=close_old_connections) as mock_close:
            msg = _save_message(session.session_uuid, MessageRole.USER, 'Перевірка зʼєднання')
        mock_close.assert_called_once()
        self.assertEqual(msg.content, 'Перевірка зʼєднання')

    @patch(
        'support.agent.client.MistralSupportAgent.stream_chat_response',
        side_effect=mock_async_stream_tokens,
    )
    def test_wsgi_sync_client_streams_sse_cleanly_and_persists_messages(self, mock_stream):
        sync_client = Client()
        sync_client.force_login(self.user)
        response = sync_client.post(
            '/support/chat/stream/',
            data=json.dumps({'message': 'WSGI запит без scope'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/event-stream')

        chunks = [
            chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk
            for chunk in response.streaming_content
        ]
        joined = ''.join(chunks)
        self.assertIn('data: {"token": "Привіт! "}', joined)
        self.assertIn('data: {"token": "Як справи?"}', joined)
        self.assertIn('data: {"done": true}', joined)

        session = ChatSession.objects.get(user=self.user, status=ChatSessionStatus.ACTIVE)
        messages = list(session.messages.order_by('created_at', 'id'))
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, MessageRole.USER)
        self.assertEqual(messages[0].content, 'WSGI запит без scope')
        self.assertEqual(messages[1].role, MessageRole.ASSISTANT)
        self.assertEqual(messages[1].content, 'Привіт! Як справи?')

    def test_threaded_wsgi_adapter_abrupt_disconnect_closes_stream_and_connections(self):
        closed_flag = False

        def db_ping():
            with connections['default'].cursor() as cursor:
                cursor.execute('SELECT 1')
                return cursor.fetchone()[0]

        async def streaming_gen():
            nonlocal closed_flag
            try:
                await sync_to_async(db_ping, thread_sensitive=True)()
                yield 'chunk-1'
                await asyncio.sleep(5)
                yield 'chunk-2'
            finally:
                closed_flag = True

        with patch(
            'support.views._close_non_atomic_connections',
            wraps=close_old_connections,
        ) as mock_close:
            iterator = _iter_async_gen_threaded(streaming_gen())
            self.assertEqual(next(iterator), 'chunk-1')
            iterator.close()

        self.assertTrue(closed_flag)
        self.assertGreaterEqual(mock_close.call_count, 1)

        orig_max_age = connections['default'].settings_dict.get('CONN_MAX_AGE', 0)
        connections['default'].settings_dict['CONN_MAX_AGE'] = 60
        try:
            conn = connections['default']
            with conn.cursor() as cursor:
                cursor.execute('SELECT 1')
            self.assertIsNotNone(conn.connection)
            with patch.object(conn, 'close', wraps=conn.close) as mock_conn_close:
                _close_thread_connections()
            mock_conn_close.assert_called_once()
            if not (conn.vendor == 'sqlite' and conn.is_in_memory_db()):
                self.assertIsNone(conn.connection)
        finally:
            connections['default'].settings_dict['CONN_MAX_AGE'] = orig_max_age

    def test_concurrent_get_or_create_active_session_creates_single_session(self):
        workers = 6
        barrier = threading.Barrier(workers)

        def worker_auth(_idx: int):
            try:
                req = self.factory.post('/support/chat/stream/')
                req.user = self.user
                req.session = {}
                barrier.wait(timeout=10)
                return ChatSessionService.get_or_create_active_session(req).session_uuid
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            auth_ids = list(pool.map(worker_auth, range(workers)))

        self.assertEqual(len(set(auth_ids)), 1)
        self.assertEqual(
            ChatSession.objects.filter(
                user=self.user,
                status__in=[ChatSessionStatus.ACTIVE, ChatSessionStatus.ESCALATED],
            ).count(),
            1,
        )

        guest_tok = 'concurrent-guest-tok-999'
        barrier_guest = threading.Barrier(workers)

        def worker_guest(_idx: int):
            try:
                req = self.factory.post('/support/chat/stream/')
                req.user = AnonymousUser()
                session = SessionStore()
                session[GUEST_TOKEN_SESSION_KEY] = guest_tok
                req.session = session
                barrier_guest.wait(timeout=10)
                return ChatSessionService.get_or_create_active_session(req).session_uuid
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            guest_ids = list(pool.map(worker_guest, range(workers)))

        self.assertEqual(len(set(guest_ids)), 1)
        self.assertEqual(
            ChatSession.objects.filter(
                user__isnull=True,
                guest_token=guest_tok,
                status__in=[ChatSessionStatus.ACTIVE, ChatSessionStatus.ESCALATED],
            ).count(),
            1,
        )

    def test_concurrent_escalate_session_creates_single_open_ticket(self):
        session = ChatSession.objects.create(user=self.user, status=ChatSessionStatus.ACTIVE)
        workers = 6
        barrier = threading.Barrier(workers)

        def worker(idx: int) -> int:
            try:
                barrier.wait(timeout=10)
                ticket = ChatSessionService.escalate_session(
                    session=session,
                    reason=TicketReason.COMPLAINT,
                    details=f'Одночасна ескалація #{idx}',
                    customer_phone='+380501112233',
                )
                return ticket.id
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            ticket_ids = list(pool.map(worker, range(workers)))

        self.assertEqual(len(set(ticket_ids)), 1)
        self.assertEqual(SupportTicket.objects.filter(session=session).count(), 1)
