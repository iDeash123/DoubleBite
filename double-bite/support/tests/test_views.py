import json
import os
from unittest.mock import patch

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import AsyncClient, Client, TestCase

from support.models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    MessageRole,
)

os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'

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


class SupportViewsTest(TestCase):
    def setUp(self):
        cache.clear()
        self.sync_client = Client()
        self.async_client = AsyncClient()
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

    @patch('support.agent.client.MistralSupportAgent.stream_chat_response', side_effect=mock_async_stream_tokens)
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
            session = ChatSession.objects.filter(user=self.user, status=ChatSessionStatus.ACTIVE).first()
            self.assertIsNotNone(session)
            user_msg = session.messages.filter(role=MessageRole.USER).first()
            self.assertEqual(user_msg.content, 'Привіт, бот!')
            assistant_msg = session.messages.filter(role=MessageRole.ASSISTANT).first()
            self.assertEqual(assistant_msg.content, 'Привіт! Як справи?')

        await sync_to_async(verify_persisted_messages, thread_sensitive=True)()

    @patch('support.agent.client.MistralSupportAgent.stream_chat_response', side_effect=mock_async_stream_escalate)
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

    @patch('support.agent.client.MistralSupportAgent.stream_chat_response', side_effect=mock_async_stream_cart)
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

    @patch('support.agent.client.MistralSupportAgent.stream_chat_response', side_effect=mock_async_stream_tokens)
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

    def test_reset_view(self):
        self.sync_client.force_login(self.user)
        session = ChatSession.objects.create(user=self.user, status=ChatSessionStatus.ACTIVE)

        res = self.sync_client.post('/support/chat/reset/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'ok')

        session.refresh_from_db()
        self.assertEqual(session.status, ChatSessionStatus.RESOLVED)

        new_session = ChatSession.objects.filter(user=self.user, status=ChatSessionStatus.ACTIVE).first()
        self.assertIsNotNone(new_session)
        self.assertEqual(str(new_session.session_uuid), data['session_uuid'])

    def test_reset_view_get_method_not_allowed(self):
        self.sync_client.force_login(self.user)
        res = self.sync_client.get('/support/chat/reset/')
        self.assertEqual(res.status_code, 405)

    def test_history_view_and_session_isolation(self):
        self.sync_client.force_login(self.user)
        session_a = ChatSession.objects.create(user=self.user, status=ChatSessionStatus.ACTIVE)
        ChatMessage.objects.create(session=session_a, role=MessageRole.USER, content='Питання від А')
        ChatMessage.objects.create(session=session_a, role=MessageRole.ASSISTANT, content='Відповідь для А')

        session_b = ChatSession.objects.create(user=self.other_user, status=ChatSessionStatus.ACTIVE)
        ChatMessage.objects.create(session=session_b, role=MessageRole.USER, content='Питання від Б')

        res_a = self.sync_client.get('/support/chat/history/')
        self.assertEqual(res_a.status_code, 200)
        data_a = res_a.json()
        self.assertEqual(len(data_a['messages']), 2)
        self.assertEqual(data_a['messages'][0]['content'], 'Питання від А')
        self.assertEqual(data_a['messages'][1]['content'], 'Відповідь для А')

        self.sync_client.force_login(self.other_user)
        res_b = self.sync_client.get('/support/chat/history/')
        self.assertEqual(res_b.status_code, 200)
        data_b = res_b.json()
        self.assertEqual(len(data_b['messages']), 1)
        self.assertEqual(data_b['messages'][0]['content'], 'Питання від Б')

    def test_get_history_returns_latest_20_messages_chronologically(self):
        from support.views import _get_history
        session = ChatSession.objects.create(user=self.user, status=ChatSessionStatus.ACTIVE)
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
