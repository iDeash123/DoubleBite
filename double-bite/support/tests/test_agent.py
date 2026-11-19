import json
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory, TestCase
from menu.models import Category, Dish
from orders.models import Order, OrderStatus

from support.agent.client import MistralSupportAgent
from support.agent.prompts import SYSTEM_PROMPT
from support.models import ChatSession

User = get_user_model()


class MockDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class MockDeltaToolCall:
    def __init__(self, index=0, call_id=None, name=None, arguments=None):
        self.index = index
        self.id = call_id
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = arguments


class MockChoice:
    def __init__(self, content=None, tool_calls=None):
        self.delta = MockDelta(content=content, tool_calls=tool_calls)


class MockChunk:
    def __init__(
        self,
        content=None,
        tool_calls=None,
        *,
        choices=None,
        has_data=True,
        usage=None,
    ):
        if not has_data:
            self.data = None
            return
        self.data = MagicMock()
        if choices is not None:
            self.data.choices = choices
        else:
            self.data.choices = [MockChoice(content=content, tool_calls=tool_calls)]
        self.data.usage = usage


class MockAsyncStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        self._iter = iter(self.chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self):
        self.closed = True


class MistralSupportAgentTest(TestCase):
    def setUp(self):
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
        agent = MistralSupportAgent(
            api_key='test-key',
            model='mistral-large-latest',
            temperature=0.5,
            max_tokens=512,
            timeout_ms=12000,
            max_rounds=4,
        )
        self.assertEqual(agent.api_key, 'test-key')
        self.assertEqual(agent.model, 'mistral-large-latest')
        self.assertEqual(agent.temperature, 0.5)
        self.assertEqual(agent.max_tokens, 512)
        self.assertEqual(agent.timeout_ms, 12000)
        self.assertEqual(agent.max_rounds, 4)

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
    async def test_direct_response_streams_via_stream_async_with_timeouts_and_max_tokens(
        self, mock_mistral_cls
    ):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client

        stream = MockAsyncStream([
            MockChunk('Доброго дня! '),
            MockChunk('Чим можу допомогти?'),
        ])
        mock_client.chat.stream_async.return_value = stream

        agent = MistralSupportAgent(api_key='fake-key', max_tokens=777, timeout_ms=18000)
        events = []
        async for ev in agent.stream_chat_response([{'role': 'user', 'content': 'Привіт'}]):
            events.append(ev)

        self.assertEqual(
            events,
            [{'token': 'Доброго дня! '}, {'token': 'Чим можу допомогти?'}],
        )
        mock_mistral_cls.assert_called_once_with(api_key='fake-key', timeout_ms=18000)
        mock_client.chat.complete_async.assert_not_called()
        mock_client.chat.stream_async.assert_called_once()
        call_kwargs = mock_client.chat.stream_async.call_args.kwargs
        self.assertEqual(call_kwargs['max_tokens'], 777)
        self.assertEqual(call_kwargs['timeout_ms'], 18000)
        self.assertIn('tools', call_kwargs)
        self.assertTrue(stream.closed)

    @patch('support.agent.client.Mistral')
    async def test_response_with_tool_call_check_order(self, mock_mistral_cls):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client

        tc_chunk = MockChunk(
            tool_calls=[
                MockDeltaToolCall(
                    index=0,
                    call_id='call_order_1',
                    name='check_order_status',
                    arguments=json.dumps({'order_id': 'DB-4001'}),
                )
            ]
        )
        text_stream = MockAsyncStream([
            MockChunk('Ваше замовлення #DB-4001 '),
            MockChunk('вже у дорозі!'),
        ])
        mock_client.chat.stream_async.side_effect = [
            MockAsyncStream([tc_chunk]),
            text_stream,
        ]

        req = self._get_request()
        agent = MistralSupportAgent(api_key='fake-key')
        tokens = []
        async for ev in agent.stream_chat_response(
            [{'role': 'user', 'content': 'Де моє замовлення DB-4001?'}],
            request=req,
            session=self.session,
        ):
            if 'token' in ev:
                tokens.append(ev['token'])

        full_text = ''.join(tokens)
        self.assertIn('Ваше замовлення #DB-4001 вже у дорозі!', full_text)
        mock_client.chat.complete_async.assert_not_called()
        self.assertEqual(mock_client.chat.stream_async.call_count, 2)

    @patch('support.agent.client.Mistral')
    async def test_response_with_tool_call_escalate(self, mock_mistral_cls):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client

        tc_chunk = MockChunk(
            tool_calls=[
                MockDeltaToolCall(
                    index=0,
                    call_id='call_esc_1',
                    name='escalate_to_operator',
                    arguments=json.dumps({
                        'reason': 'COMPLAINT',
                        'details': 'Замовлення затримується',
                    }),
                )
            ]
        )
        mock_client.chat.stream_async.side_effect = [
            MockAsyncStream([tc_chunk]),
            MockAsyncStream([MockChunk('Я покликав оператора.')]),
        ]

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
    async def test_response_with_tool_call_cart_flushes_after_streamed_text(self, mock_mistral_cls):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client

        tc_chunk = MockChunk(
            tool_calls=[
                MockDeltaToolCall(
                    index=0,
                    call_id='call_cart_1',
                    name='add_to_cart',
                    arguments=json.dumps({'dish_id': self.dish.id, 'quantity': 1}),
                )
            ]
        )
        mock_client.chat.stream_async.side_effect = [
            MockAsyncStream([tc_chunk]),
            MockAsyncStream([MockChunk('Додав Кальцоне до вашого кошика!')]),
        ]

        req = self._get_request()
        agent = MistralSupportAgent(api_key='fake-key')
        events = []
        async for ev in agent.stream_chat_response(
            [{'role': 'user', 'content': 'Додай кальцоне в кошик'}],
            request=req,
            session=self.session,
        ):
            events.append(ev)

        token_idx = next(i for i, e in enumerate(events) if 'token' in e)
        cart_idx = next(i for i, e in enumerate(events) if e.get('cart_mutation'))
        self.assertGreater(cart_idx, token_idx)
        self.assertTrue(events[cart_idx]['cart_mutation'])
        self.assertEqual(events[cart_idx]['cart_items_count'], 1)

    @patch('support.agent.client.Mistral')
    async def test_multi_round_tool_chain_search_then_add_to_cart_then_reply(
        self, mock_mistral_cls
    ):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client

        round1_chunks = [
            MockChunk(
                tool_calls=[
                    MockDeltaToolCall(
                        index=0,
                        call_id='call_search_1',
                        name='search_dishes',
                        arguments='{"query": "',
                    )
                ]
            ),
            MockChunk(
                tool_calls=[
                    MockDeltaToolCall(
                        index=0,
                        arguments='Кальцоне"}',
                    )
                ]
            ),
        ]
        round2_chunks = [
            MockChunk(
                tool_calls=[
                    MockDeltaToolCall(
                        index=0,
                        call_id='call_add_2',
                        name='add_to_cart',
                        arguments=json.dumps({'dish_id': self.dish.id, 'quantity': 2}),
                    )
                ]
            ),
        ]
        round3_chunks = [
            MockChunk('Знайшов Кальцоне '),
            MockChunk('і додав 2 шт. до кошика!'),
        ]
        mock_client.chat.stream_async.side_effect = [
            MockAsyncStream(round1_chunks),
            MockAsyncStream(round2_chunks),
            MockAsyncStream(round3_chunks),
        ]

        req = self._get_request()
        agent = MistralSupportAgent(api_key='fake-key', max_rounds=3)
        events = []
        async for ev in agent.stream_chat_response(
            [{'role': 'user', 'content': 'Знайди кальцоне і додай 2 шт'}],
            request=req,
            session=self.session,
        ):
            events.append(ev)

        self.assertEqual(mock_client.chat.stream_async.call_count, 3)
        tokens = [e['token'] for e in events if 'token' in e]
        self.assertEqual(''.join(tokens), 'Знайшов Кальцоне і додав 2 шт. до кошика!')
        cart_ev = next((e for e in events if e.get('cart_mutation')), None)
        self.assertIsNotNone(cart_ev)
        self.assertEqual(cart_ev['cart_items_count'], 2)
        self.assertEqual(events[-1], cart_ev)

    @patch('support.agent.client.Mistral')
    async def test_max_rounds_exhaustion_runs_final_stream_without_tools(
        self, mock_mistral_cls
    ):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client

        r1 = MockAsyncStream([
            MockChunk(
                tool_calls=[
                    MockDeltaToolCall(
                        index=0,
                        call_id='call_r1',
                        name='search_dishes',
                        arguments=json.dumps({'query': 'Кальцоне'}),
                    )
                ]
            )
        ])
        r2 = MockAsyncStream([
            MockChunk(
                tool_calls=[
                    MockDeltaToolCall(
                        index=0,
                        call_id='call_r2',
                        name='add_to_cart',
                        arguments=json.dumps({'dish_id': self.dish.id, 'quantity': 1}),
                    )
                ]
            )
        ])
        final_r3 = MockAsyncStream([MockChunk('Фінальна відповідь без інструментів.')])
        mock_client.chat.stream_async.side_effect = [r1, r2, final_r3]

        req = self._get_request()
        agent = MistralSupportAgent(api_key='fake-key', max_rounds=2, max_tokens=500, timeout_ms=15000)
        events = []
        async for ev in agent.stream_chat_response(
            [{'role': 'user', 'content': 'Знайди і додай кальцоне'}],
            request=req,
            session=self.session,
        ):
            events.append(ev)

        self.assertEqual(mock_client.chat.stream_async.call_count, 3)
        final_kwargs = mock_client.chat.stream_async.call_args_list[2].kwargs
        self.assertNotIn('tools', final_kwargs)
        self.assertEqual(final_kwargs['max_tokens'], 500)
        self.assertEqual(final_kwargs['timeout_ms'], 15000)
        self.assertEqual(events[0], {'token': 'Фінальна відповідь без інструментів.'})
        self.assertTrue(events[-1]['cart_mutation'])

    @patch('support.agent.client.execute_agent_tool')
    @patch('support.agent.client.Mistral')
    async def test_malformed_tool_args_return_error_json_without_crashing(
        self, mock_mistral_cls, mock_exec_tool
    ):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client

        bad_calls = [
            MockDeltaToolCall(index=0, call_id='c0', name='search_dishes', arguments='null'),
            MockDeltaToolCall(index=1, call_id='c1', name='search_dishes', arguments='[1]'),
            MockDeltaToolCall(index=2, call_id='c2', name='search_dishes', arguments='{broken'),
            MockDeltaToolCall(index=3, call_id='c3', name='search_dishes', arguments=123),
        ]
        captured_messages = []

        async def fake_stream_async(**kwargs):
            captured_messages.append([dict(m) for m in kwargs['messages']])
            if len(captured_messages) == 1:
                return MockAsyncStream([MockChunk(tool_calls=bad_calls)])
            return MockAsyncStream([MockChunk('Вибачте, спробую ще раз.')])

        mock_client.chat.stream_async.side_effect = fake_stream_async

        agent = MistralSupportAgent(api_key='fake-key')
        events = []
        async for ev in agent.stream_chat_response([{'role': 'user', 'content': 'Тест'}]):
            events.append(ev)

        mock_exec_tool.assert_not_called()
        self.assertEqual(events, [{'token': 'Вибачте, спробую ще раз.'}])
        second_round_msgs = captured_messages[1]
        tool_msgs = [m for m in second_round_msgs if m.get('role') == 'tool']
        self.assertEqual(len(tool_msgs), 4)
        for tmsg in tool_msgs:
            payload = json.loads(tmsg['content'])
            self.assertEqual(
                payload,
                {'error': 'Invalid tool arguments, expected JSON object'},
            )

    @patch('support.agent.client.Mistral')
    async def test_empty_choices_none_data_and_think_chunks_ignored(self, mock_mistral_cls):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client

        think_part = MagicMock()
        think_part.type = 'thinking'
        think_part.text = 'internal thought'
        text_part = MagicMock()
        text_part.type = 'text'
        text_part.text = 'Видимий текст'

        chunks = [
            MockChunk(has_data=False),
            MockChunk(choices=[]),
            MockChunk(content=[think_part, text_part]),
        ]
        mock_client.chat.stream_async.return_value = MockAsyncStream(chunks)

        agent = MistralSupportAgent(api_key='fake-key')
        events = []
        async for ev in agent.stream_chat_response([{'role': 'user', 'content': 'Привіт'}]):
            events.append(ev)

        self.assertEqual(events, [{'token': 'Видимий текст'}])

    @patch('support.agent.client.asyncio.sleep', new_callable=AsyncMock)
    @patch('support.agent.client._SYSTEM_RANDOM')
    @patch('support.agent.client.Mistral')
    async def test_rate_limit_429_retry_backoff_includes_jitter(
        self, mock_mistral_cls, mock_rng, mock_sleep
    ):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client
        mock_rng.uniform.side_effect = [0.25, 0.75]

        err_429 = RuntimeError('Rate limit')
        err_429.status_code = 429

        mock_client.chat.stream_async.side_effect = [
            err_429,
            err_429,
            MockAsyncStream([MockChunk('Успіх після ретраю')]),
        ]

        agent = MistralSupportAgent(api_key='fake-key')
        events = []
        async for ev in agent.stream_chat_response([{'role': 'user', 'content': 'Привіт'}]):
            events.append(ev)

        self.assertEqual(events, [{'token': 'Успіх після ретраю'}])
        self.assertEqual(mock_rng.uniform.call_args_list[0].args, (0.0, 1.0))
        self.assertEqual(mock_rng.uniform.call_args_list[1].args, (0.0, 2.0))
        self.assertEqual(mock_sleep.await_count, 2)
        self.assertAlmostEqual(mock_sleep.await_args_list[0].args[0], 2.25)
        self.assertAlmostEqual(mock_sleep.await_args_list[1].args[0], 4.75)

    @patch('support.agent.client.Mistral')
    async def test_api_exception_fallback(self, mock_mistral_cls):
        mock_client = AsyncMock()
        mock_mistral_cls.return_value.__aenter__.return_value = mock_client
        mock_client.chat.stream_async.side_effect = RuntimeError('Connection reset')

        agent = MistralSupportAgent(api_key='fake-key')
        events = []
        async for ev in agent.stream_chat_response([{'role': 'user', 'content': 'Привіт'}]):
            events.append(ev)

        self.assertEqual(len(events), 1)
        self.assertIn('error', events[0])
        self.assertIn('+380 44 123 45 67', events[0]['error'])
