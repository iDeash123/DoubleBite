import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory, TestCase
from menu.models import Category, Dish
from orders.models import Order, OrderStatus

from support.agent.client import MistralSupportAgent
from support.agent.prompts import SYSTEM_PROMPT
from support.models import ChatSession

os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'

User = get_user_model()


class MockDelta:
    def __init__(self, content):
        self.content = content


class MockChoice:
    def __init__(self, content=None, tool_calls=None):
        self.delta = MockDelta(content)
        self.message = MagicMock()
        self.message.content = content
        self.message.tool_calls = tool_calls
        self.message.role = 'assistant'


class MockChunk:
    def __init__(self, content):
        self.data = MagicMock()
        self.data.choices = [MockChoice(content=content)]


class MockAsyncStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        self._iter = iter(self.chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class MistralSupportAgentTest(TestCase):
    def setUp(self):
        self._env_patch = patch.dict(os.environ, {'GEMINI_API_KEY': ''})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            email='agent_test@example.com',
            password='password123',
            phone='+380501112233',
        )
        self.category = Category.objects.create(name='Піца', slug='pizza', is_active=True)
        self.dish = Dish.objects.create(
            category=self.category,
            title='Кальцоне',
            slug='calzone',
            price=240.00,
            weight_grams=400,
            calories=600,
            is_available=True,
        )
        self.order = Order.objects.create(
            user=self.user,
            order_number='DB-4001',
            customer_name='Іван',
            customer_phone='+380501112233',
            delivery_address='вул. Сагайдачного, 10',
            status=OrderStatus.ON_WAY,
            total_amount=240.00,
            eta_minutes=15,
        )
        self.session = ChatSession.objects.create(user=self.user)
        self.session_store = SessionStore()
        self.session_store.create()

    def _get_request(self):
        request = self.factory.post('/support/chat/stream/')
        request.user = self.user
        request.session = self.session_store
        return request

    def test_agent_initialization(self):
        agent = MistralSupportAgent(api_key='test-key', model='mistral-large-latest', temperature=0.5, max_tokens=512)
        self.assertEqual(agent.api_key, 'test-key')
        self.assertEqual(agent.model, 'mistral-large-latest')
        self.assertEqual(agent.temperature, 0.5)
        self.assertEqual(agent.max_tokens, 512)
        self.assertEqual(agent.gemini_model, 'gemini-2.5-flash')

    def test_system_prompt_language_rules(self):
        self.assertIn('українська', SYSTEM_PROMPT.lower())
        self.assertIn('check_order_status', SYSTEM_PROMPT)
        self.assertIn('search_dishes', SYSTEM_PROMPT)
        self.assertIn('escalate_to_operator', SYSTEM_PROMPT)
        self.assertIn('+380 44 123 45 67', SYSTEM_PROMPT)

    async def test_missing_api_key_yields_message(self):
        agent = MistralSupportAgent(api_key='')
        tokens = []
        async for event in agent.stream_chat_response([{'role': 'user', 'content': 'Привіт'}]):
            if 'token' in event:
                tokens.append(event['token'])
        self.assertTrue(len(tokens) > 0)
        self.assertIn('тимчасово недоступний', tokens[0])

    @patch('support.agent.client.Mistral')
    async def test_direct_response_without_tools(self, mock_mistral_cls):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client

        mock_complete_resp = MagicMock()
        mock_complete_resp.choices = [
            MockChoice(content='Доброго дня! Чим можу допомогти?', tool_calls=None)
        ]
        mock_client.chat.complete_async.return_value = mock_complete_resp

        agent = MistralSupportAgent(api_key='fake-key')
        events = []
        async for ev in agent.stream_chat_response([{'role': 'user', 'content': 'Привіт'}]):
            events.append(ev)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], {'token': 'Доброго дня! Чим можу допомогти?'})
        mock_client.chat.complete_async.assert_called_once()

    @patch('support.agent.client.Mistral')
    async def test_response_with_tool_call_check_order(self, mock_mistral_cls):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client

        mock_tc = MagicMock()
        mock_tc.id = 'call_order_1'
        mock_tc.function.name = 'check_order_status'
        mock_tc.function.arguments = json.dumps({'order_id': self.order.id})

        mock_complete_resp = MagicMock()
        mock_complete_resp.choices = [MockChoice(content=None, tool_calls=[mock_tc])]
        mock_client.chat.complete_async.return_value = mock_complete_resp

        stream_chunks = [
            MockChunk('Ваше замовлення #DB-4001 '),
            MockChunk('вже у дорозі!'),
        ]
        mock_client.chat.stream_async.return_value = MockAsyncStream(stream_chunks)

        req = self._get_request()
        agent = MistralSupportAgent(api_key='fake-key')
        tokens = []
        async for ev in agent.stream_chat_response(
            [{'role': 'user', 'content': 'Де моє замовлення 4001?'}],
            request=req,
            session=self.session,
        ):
            if 'token' in ev:
                tokens.append(ev['token'])

        full_text = ''.join(tokens)
        self.assertIn('Ваше замовлення #DB-4001 вже у дорозі!', full_text)
        mock_client.chat.complete_async.assert_called_once()
        mock_client.chat.stream_async.assert_called_once()

    @patch('support.agent.client.Mistral')
    async def test_response_with_tool_call_escalate(self, mock_mistral_cls):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client

        mock_tc = MagicMock()
        mock_tc.id = 'call_esc_1'
        mock_tc.function.name = 'escalate_to_operator'
        mock_tc.function.arguments = json.dumps({
            'reason': 'COMPLAINT',
            'details': 'Замовлення затримується',
        })

        mock_complete_resp = MagicMock()
        mock_complete_resp.choices = [MockChoice(content=None, tool_calls=[mock_tc])]
        mock_client.chat.complete_async.return_value = mock_complete_resp

        stream_chunks = [MockChunk('Я покликав оператора.')]
        mock_client.chat.stream_async.return_value = MockAsyncStream(stream_chunks)

        req = self._get_request()
        agent = MistralSupportAgent(api_key='fake-key')
        events = []
        async for ev in agent.stream_chat_response(
            [{'role': 'user', 'content': 'Поклич людину терміново!'}],
            request=req,
            session=self.session,
        ):
            events.append(ev)

        esc_event = next((e for e in events if e.get('escalated')), None)
        self.assertIsNotNone(esc_event)
        self.assertTrue(esc_event['escalated'])
        self.assertEqual(esc_event['support_phone'], '+380 44 123 45 67')

    @patch('support.agent.client.Mistral')
    async def test_response_with_tool_call_cart(self, mock_mistral_cls):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client

        mock_tc = MagicMock()
        mock_tc.id = 'call_cart_1'
        mock_tc.function.name = 'add_to_cart'
        mock_tc.function.arguments = json.dumps({'dish_id': self.dish.id, 'quantity': 1})

        mock_complete_resp = MagicMock()
        mock_complete_resp.choices = [MockChoice(content=None, tool_calls=[mock_tc])]
        mock_client.chat.complete_async.return_value = mock_complete_resp

        stream_chunks = [MockChunk('Додав Кальцоне до вашого кошика!')]
        mock_client.chat.stream_async.return_value = MockAsyncStream(stream_chunks)

        req = self._get_request()
        agent = MistralSupportAgent(api_key='fake-key')
        events = []
        async for ev in agent.stream_chat_response(
            [{'role': 'user', 'content': 'Додай кальцоне в кошик'}],
            request=req,
            session=self.session,
        ):
            events.append(ev)

        cart_ev = next((e for e in events if e.get('cart_mutation')), None)
        self.assertIsNotNone(cart_ev)
        self.assertTrue(cart_ev['cart_mutation'])
        self.assertEqual(cart_ev['cart_items_count'], 1)

    @patch('support.agent.client.Mistral')
    async def test_api_exception_fallback(self, mock_mistral_cls):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client
        mock_client.chat.complete_async.side_effect = RuntimeError('Connection reset')

        agent = MistralSupportAgent(api_key='fake-key')
        events = []
        async for ev in agent.stream_chat_response([{'role': 'user', 'content': 'Привіт'}]):
            events.append(ev)

        self.assertEqual(len(events), 1)
        self.assertIn('error', events[0])
        self.assertIn('+380 44 123 45 67', events[0]['error'])

    @patch('support.agent.client.genai.Client')
    async def test_stream_gemini_passes_system_content_with_cart(self, mock_genai_client_cls):
        mock_client = MagicMock()
        mock_genai_client_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [MagicMock(text='Ось ваша відповідь', function_call=None)]
        mock_response.candidates = [mock_candidate]
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

        agent = MistralSupportAgent(gemini_api_key='fake-gemini-key')
        messages = [
            {'role': 'system', 'content': 'SYSTEM_PROMPT_CUSTOM_CART_CONTEXT'},
            {'role': 'user', 'content': 'Привіт'},
        ]
        tokens = []
        async for event in agent._stream_gemini(messages, request=None, session=None):
            if 'token' in event:
                tokens.append(event['token'])

        self.assertEqual(tokens, ['Ось ваша відповідь'])
        mock_client.aio.models.generate_content.assert_called_once()
        _, call_kwargs = mock_client.aio.models.generate_content.call_args
        self.assertEqual(call_kwargs['config'].system_instruction, 'SYSTEM_PROMPT_CUSTOM_CART_CONTEXT')
        self.assertEqual(call_kwargs['model'], 'gemini-2.5-flash')

    @patch('support.agent.client.genai.Client')
    async def test_stream_gemini_handles_empty_candidates(self, mock_genai_client_cls):
        mock_client = MagicMock()
        mock_genai_client_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.candidates = []
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

        agent = MistralSupportAgent(gemini_api_key='fake-gemini-key')
        messages = [{'role': 'user', 'content': 'Привіт'}]
        events = [e async for e in agent._stream_gemini(messages, request=None, session=None)]
        self.assertEqual(events, [])
