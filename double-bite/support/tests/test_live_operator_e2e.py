import json
import re
import shutil
import subprocess
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from accounts.models import Role
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from menu.models import Category, Dish
from orders.models import Cart, CartItem, Order, OrderItem, OrderStatus

from support.agent.tools import execute_agent_tool
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
from support.views import OPERATOR_HISTORY_PREFIX

User = get_user_model()

KYIV_TZ = ZoneInfo('Europe/Kyiv')

E2E_TELEGRAM_SETTINGS = {
    'TELEGRAM_BOT_ENABLED': True,
    'TELEGRAM_BOT_TOKEN': '123456:E2E-BOT-TOKEN',
    'TELEGRAM_SUPPORT_CHAT_ID': '-1001999888777',
    'TELEGRAM_WEBHOOK_SECRET': 'e2e-telegram-webhook-secret',
    'SITE_DOMAIN': 'doublebite.ua',
    'SITE_PROTOCOL': 'https',
}


def _fake_tg_response(result: Any, *, status_code: int = 200, ok: bool = True) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {'ok': ok, 'result': result}
    return resp


def _read_sse_body(response) -> str:
    return b''.join(response.streaming_content).decode('utf-8')


class LiveOperatorFullLifecycleE2ETest(TransactionTestCase):
    """End-to-end journey across client chat, operator desk, Telegram sync, and AI handback."""

    def setUp(self):
        cache.clear()
        mail.outbox.clear()

        self.customer = User.objects.create_user(
            email='e2e.customer@example.com',
            password='Password123!',
            first_name='Софія',
            last_name='Мельник',
            phone='+380671234567',
            role=Role.CUSTOMER,
        )
        self.other_customer = User.objects.create_user(
            email='e2e.other@example.com',
            password='Password123!',
            first_name='Андрій',
            last_name='Шевченко',
            role=Role.CUSTOMER,
        )
        self.operator = User.objects.create_user(
            email='e2e.operator@example.com',
            password='Password123!',
            first_name='Олена',
            last_name='Коваль',
            role=Role.RESTAURANT_ADMIN,
            telegram_id=777001,
        )
        self.colleague = User.objects.create_user(
            email='e2e.colleague@example.com',
            password='Password123!',
            first_name='Максим',
            last_name='Бондар',
            role=Role.RESTAURANT_ADMIN,
            telegram_id=777002,
        )

        self.category = Category.objects.create(name='Бургери', slug='e2e-burgers', is_active=True)
        self.dish = Dish.objects.create(
            category=self.category,
            title='Трюфельний Smash',
            slug='e2e-truffle-smash',
            price=Decimal('340.00'),
            weight_grams=290,
            is_available=True,
        )

        self.order = Order.objects.create(
            user=self.customer,
            order_number='DB-9042',
            customer_name='Софія Мельник',
            customer_phone='+380671234567',
            customer_email=self.customer.email,
            delivery_address='Київ, вул. Саксаганського 12',
            total_amount=Decimal('340.00'),
            status=OrderStatus.PREPARING,
        )
        OrderItem.objects.create(
            order=self.order,
            dish=self.dish,
            dish_title=self.dish.title,
            price=Decimal('340.00'),
            quantity=1,
        )

        self.cart = Cart.objects.create(user=self.customer)
        CartItem.objects.create(cart=self.cart, dish=self.dish, quantity=2)

        # Past resolved conversation for the same customer
        self.past_session = ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.RESOLVED,
            is_escalated=False,
        )
        self.past_ticket = SupportTicket.objects.create(
            session=self.past_session,
            reason=TicketReason.ORDER_ISSUE,
            details='Минуле звернення щодо соусу',
            status=TicketStatus.RESOLVED,
            assigned_operator=self.operator,
        )
        ChatMessage.objects.create(
            session=self.past_session,
            role=MessageRole.USER,
            source=MessageSource.WEB,
            content='Минулого разу не поклали трюфельний соус.',
        )
        ChatMessage.objects.create(
            session=self.past_session,
            role=MessageRole.OPERATOR,
            sender_user=self.operator,
            source=MessageSource.WEB,
            content='Ми компенсували соус промокодом.',
        )

        # Unrelated customer's past session (must never leak via context or history partial)
        self.other_past_session = ChatSession.objects.create(
            user=self.other_customer,
            status=ChatSessionStatus.RESOLVED,
        )
        ChatMessage.objects.create(
            session=self.other_past_session,
            role=MessageRole.USER,
            source=MessageSource.WEB,
            content='ПРИВАТНЕ ПОВІДОМЛЕННЯ ІНШОГО КЛІЄНТА',
        )

    @override_settings(**E2E_TELEGRAM_SETTINGS)
    def test_full_journey_ai_escalate_desk_telegram_return_ai_proactive_takeover_resolve(self):
        customer_client = Client()
        operator_client = Client()
        colleague_client = Client()
        webhook_client = Client()

        customer_client.force_login(self.customer)
        operator_client.force_login(self.operator)
        colleague_client.force_login(self.colleague)

        tg_calls: list[tuple[str, dict[str, Any]]] = []
        topic_seq = {'n': 5000}
        msg_seq = {'n': 8000}

        def fake_tg_post(url: str, json: dict[str, Any] | None = None, timeout: float | None = None):
            method = url.rsplit('/', 1)[-1]
            payload = dict(json or {})
            tg_calls.append((method, payload))
            if method == 'createForumTopic':
                topic_seq['n'] += 1
                return _fake_tg_response({'message_thread_id': topic_seq['n']})
            if method == 'sendMessage':
                msg_seq['n'] += 1
                return _fake_tg_response({'message_id': msg_seq['n']})
            return _fake_tg_response(True)

        captured_ai_histories: list[list[dict[str, Any]]] = []

        async def fake_ai_stream(agent_self, history: list[dict[str, Any]], **kwargs):
            captured_ai_histories.append(list(history))
            if len(captured_ai_histories) == 1:
                yield {'token': 'Бачу ваше замовлення DB-9042, '}
                yield {'token': 'зараз воно готується!'}
            else:
                yield {'token': 'Так, бачу обіцянку адміністратора щодо компенсації — '}
                yield {'token': 'можу додати десерт до кошика!'}

        working_now = datetime(2026, 11, 27, 14, 30, tzinfo=KYIV_TZ)

        with (
            patch('support.telegram.httpx.post', side_effect=fake_tg_post),
            patch('support.agent.client.GeminiSupportAgent.stream_chat_response', new=fake_ai_stream),
            patch('support.services.timezone.now', return_value=working_now),
        ):
            # 1. Customer chats with AI
            res_ai_1 = customer_client.post(
                reverse('support:chat_stream'),
                data=json.dumps({'message': 'Підкажіть по замовленню DB-9042'}),
                content_type='application/json',
            )
            self.assertEqual(res_ai_1.status_code, 200)
            sse_ai_1 = _read_sse_body(res_ai_1)
            self.assertIn('Бачу ваше замовлення DB-9042', sse_ai_1)
            self.assertIn('"done": true', sse_ai_1)

            session = ChatSession.objects.get(user=self.customer, status=ChatSessionStatus.ACTIVE)
            session_uuid = str(session.session_uuid)

            # 2. Customer manually escalates to live operator
            res_esc = customer_client.post(
                reverse('support:chat_escalate'),
                data=json.dumps({
                    'reason': TicketReason.ORDER_ISSUE,
                    'details': 'Потрібно змінити адресу доставки DB-9042.',
                }),
                content_type='application/json',
            )
            self.assertEqual(res_esc.status_code, 200)
            esc_data = res_esc.json()
            self.assertTrue(esc_data['is_escalated'])
            self.assertTrue(esc_data['is_working_hours'])
            ticket_1 = SupportTicket.objects.get(pk=esc_data['ticket_id'])
            self.assertEqual(ticket_1.status, TicketStatus.OPEN)
            self.assertEqual(ticket_1.telegram_thread_id, 5001)
            self.assertEqual(ticket_1.telegram_control_message_id, 8001)

            # Email alert points directly to /support/desk/<uuid>/
            self.assertEqual(len(mail.outbox), 1)
            self.assertIn(f'/support/desk/{session_uuid}/', mail.outbox[0].body)

            # Telegram topic created with AI chat excerpt and inline buttons
            tg_methods_after_esc = [m for m, _ in tg_calls]
            self.assertEqual(tg_methods_after_esc, ['createForumTopic', 'sendMessage', 'pinChatMessage'])
            self.assertIn('Бачу ваше замовлення DB-9042', tg_calls[1][1]['text'])

            # AI stream is locked (409) during escalation
            res_ai_locked = customer_client.post(
                reverse('support:chat_stream'),
                data=json.dumps({'message': 'Гей, AI?'}),
                content_type='application/json',
            )
            self.assertEqual(res_ai_locked.status_code, 409)
            self.assertTrue(res_ai_locked.json()['is_escalated'])

            # 3. Operator inspects desk queue, full context panel, and past history
            res_desk = operator_client.get(reverse('support:desk_session', args=[session_uuid]))
            self.assertEqual(res_desk.status_code, 200)
            desk_html = res_desk.content.decode('utf-8')
            self.assertIn('Софія Мельник', desk_html)
            self.assertIn(f'#{ticket_1.id}', desk_html)
            self.assertIn('DB-9042', desk_html)
            self.assertIn('Трюфельний Smash', desk_html)
            self.assertIn(str(self.past_session.session_uuid), desk_html)
            self.assertNotIn('ai-chat-widget', desk_html)
            self.assertNotIn('ПРИВАТНЕ ПОВІДОМЛЕННЯ ІНШОГО КЛІЄНТА', desk_html)

            # Operator opens customer's past session history partial
            res_past = operator_client.get(
                reverse('support:desk_past_history_partial', args=[self.past_session.session_uuid])
                + f'?session_uuid={session_uuid}'
            )
            self.assertEqual(res_past.status_code, 200)
            past_html = res_past.content.decode('utf-8')
            self.assertIn('Минулого разу не поклали трюфельний соус.', past_html)
            self.assertIn('Ми компенсували соус промокодом.', past_html)

            # IDOR guard: unrelated customer's past session returns 404
            res_other_past = operator_client.get(
                reverse('support:desk_past_history_partial', args=[self.other_past_session.session_uuid])
                + f'?session_uuid={session_uuid}'
            )
            self.assertEqual(res_other_past.status_code, 404)

            # 4. Operator claims the ticket; colleague gets 409 conflict
            tg_calls.clear()
            res_claim = operator_client.post(
                reverse('support:desk_claim', args=[session_uuid]),
                data='{}',
                content_type='application/json',
            )
            self.assertEqual(res_claim.status_code, 200)
            self.assertEqual(res_claim.json()['assigned_operator_id'], self.operator.id)
            self.assertEqual(res_claim.json()['ticket_status'], TicketStatus.IN_PROGRESS)
            self.assertIn('editMessageText', [m for m, _ in tg_calls])

            res_claim_conflict = colleague_client.post(
                reverse('support:desk_claim', args=[session_uuid]),
                data='{}',
                content_type='application/json',
            )
            self.assertEqual(res_claim_conflict.status_code, 409)

            # 5. Two-way live messaging: customer web -> desk + TG; operator web -> client + TG; TG -> client + desk
            tg_calls.clear()
            res_cust_msg = customer_client.post(
                reverse('support:chat_message'),
                data=json.dumps({'message': 'Нова адреса: вул. Рейтарська 25'}),
                content_type='application/json',
            )
            self.assertEqual(res_cust_msg.status_code, 201)
            cust_msg_id = res_cust_msg.json()['message']['id']
            self.assertEqual(len(tg_calls), 1)
            self.assertEqual(tg_calls[0][0], 'sendMessage')
            self.assertEqual(tg_calls[0][1]['message_thread_id'], 5001)
            self.assertIn('вул. Рейтарська 25', tg_calls[0][1]['text'])

            # Operator replies from web desk
            tg_calls.clear()
            res_op_reply = operator_client.post(
                reverse('support:desk_reply', args=[session_uuid]),
                data=json.dumps({'message': 'Адресу змінено на Рейтарська 25, даруємо комплімент.'}),
                content_type='application/json',
            )
            self.assertEqual(res_op_reply.status_code, 201)
            op_web_msg_id = res_op_reply.json()['message']['id']
            self.assertEqual(len(tg_calls), 1)
            self.assertEqual(tg_calls[0][0], 'sendMessage')
            self.assertIn('Веб-пульт', tg_calls[0][1]['text'])
            self.assertIn('Олена Коваль', tg_calls[0][1]['text'])

            # Operator sends a follow-up from Telegram Forum Topic
            res_tg_webhook = webhook_client.post(
                reverse('support:telegram_webhook'),
                data=json.dumps({
                    'update_id': 91001,
                    'message': {
                        'message_id': 89001,
                        'message_thread_id': 5001,
                        'chat': {'id': -1001999888777, 'type': 'supergroup'},
                        'from': {
                            'id': self.operator.telegram_id,
                            'is_bot': False,
                            'first_name': 'Олена',
                            'last_name': 'Коваль',
                        },
                        'text': 'Також додали компенсацію 100 грн на наступне замовлення.',
                    },
                }),
                content_type='application/json',
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=E2E_TELEGRAM_SETTINGS['TELEGRAM_WEBHOOK_SECRET'],
            )
            self.assertEqual(res_tg_webhook.status_code, 200)

            # Customer incremental history sees BOTH web and Telegram operator replies without leaking operator_name
            res_cust_hist = customer_client.get(
                reverse('support:chat_history') + f'?after_id={cust_msg_id}'
            )
            self.assertEqual(res_cust_hist.status_code, 200)
            cust_hist = res_cust_hist.json()
            self.assertTrue(cust_hist['operator_connected'])
            op_contents = [m['content'] for m in cust_hist['messages'] if m['sender'] == 'operator']
            self.assertEqual(
                op_contents,
                [
                    'Адресу змінено на Рейтарська 25, даруємо комплімент.',
                    'Також додали компенсацію 100 грн на наступне замовлення.',
                ],
            )
            for m in cust_hist['messages']:
                self.assertNotIn('operator_name', m)

            # Operator desk incremental messages sees the Telegram reply with TELEGRAM source
            res_desk_msgs = operator_client.get(
                reverse('support:desk_messages_api', args=[session_uuid]) + f'?after_id={op_web_msg_id}'
            )
            self.assertEqual(res_desk_msgs.status_code, 200)
            desk_new = res_desk_msgs.json()['messages']
            self.assertEqual(len(desk_new), 1)
            self.assertEqual(desk_new[0]['source'], MessageSource.TELEGRAM)
            self.assertEqual(desk_new[0]['source_badge'], 'TG')

            # 6. Operator returns conversation to AI
            tg_calls.clear()
            res_return_ai = operator_client.post(
                reverse('support:desk_return_ai', args=[session_uuid]),
                data='{}',
                content_type='application/json',
            )
            self.assertEqual(res_return_ai.status_code, 200)
            self.assertEqual(res_return_ai.json()['status'], ChatSessionStatus.ACTIVE)
            self.assertFalse(res_return_ai.json()['is_escalated'])

            ticket_1.refresh_from_db()
            self.assertEqual(ticket_1.status, TicketStatus.RESOLVED)
            self.assertIsNotNone(ticket_1.resolved_at)
            tg_return_methods = [m for m, _ in tg_calls]
            self.assertIn('closeForumTopic', tg_return_methods)

            # 7. Customer talks to AI again — AI receives operator's web + TG messages in history!
            res_ai_2 = customer_client.post(
                reverse('support:chat_stream'),
                data=json.dumps({'message': 'Що саме обіцяв адміністратор?'}),
                content_type='application/json',
            )
            self.assertEqual(res_ai_2.status_code, 200)
            sse_ai_2 = _read_sse_body(res_ai_2)
            self.assertIn('обіцянку адміністратора щодо компенсації', sse_ai_2)

            self.assertEqual(len(captured_ai_histories), 2)
            second_history = captured_ai_histories[1]
            operator_history_entries = [
                h for h in second_history if h['role'] == MessageRole.OPERATOR
            ]
            self.assertEqual(len(operator_history_entries), 2)
            self.assertTrue(
                all(h['content'].startswith(OPERATOR_HISTORY_PREFIX) for h in operator_history_entries)
            )
            self.assertIn('Рейтарська 25', operator_history_entries[0]['content'])
            self.assertIn('компенсацію 100 грн', operator_history_entries[1]['content'])

            # 8. Session is visible in Active AI tab; operator performs proactive takeover
            res_ai_queue = operator_client.get(reverse('support:desk_queue_partial') + '?tab=ai')
            self.assertEqual(res_ai_queue.status_code, 200)
            self.assertIn(session_uuid, res_ai_queue.content.decode('utf-8'))

            tg_calls.clear()
            res_takeover = operator_client.post(
                reverse('support:desk_takeover', args=[session_uuid]),
                data='{}',
                content_type='application/json',
            )
            self.assertEqual(res_takeover.status_code, 200)
            takeover_data = res_takeover.json()
            self.assertEqual(takeover_data['status'], ChatSessionStatus.ESCALATED)
            self.assertEqual(takeover_data['assigned_operator_id'], self.operator.id)

            ticket_2 = SupportTicket.objects.get(pk=takeover_data['ticket_id'])
            self.assertNotEqual(ticket_2.id, ticket_1.id)
            self.assertEqual(ticket_2.reason, TicketReason.OPERATOR_TAKEOVER)
            self.assertEqual(ticket_2.status, TicketStatus.IN_PROGRESS)
            self.assertEqual(ticket_2.telegram_thread_id, 5002)
            self.assertIn('createForumTopic', [m for m, _ in tg_calls])

            # Customer hitting /stream/ after proactive takeover gets 409, then can send via /message/
            res_race_stream = customer_client.post(
                reverse('support:chat_stream'),
                data=json.dumps({'message': 'Додайте ще тирамісу'}),
                content_type='application/json',
            )
            self.assertEqual(res_race_stream.status_code, 409)
            res_race_fallback = customer_client.post(
                reverse('support:chat_message'),
                data=json.dumps({'message': 'Додайте ще тирамісу'}),
                content_type='application/json',
            )
            self.assertEqual(res_race_fallback.status_code, 201)

            # 9. Operator resolves session — closes ticket, closes TG topic, moves to Resolved tab
            tg_calls.clear()
            res_resolve = operator_client.post(
                reverse('support:desk_resolve', args=[session_uuid]),
                data='{}',
                content_type='application/json',
            )
            self.assertEqual(res_resolve.status_code, 200)
            self.assertEqual(res_resolve.json()['status'], ChatSessionStatus.RESOLVED)
            self.assertIn('closeForumTopic', [m for m, _ in tg_calls])

            session.refresh_from_db()
            ticket_2.refresh_from_db()
            self.assertEqual(session.status, ChatSessionStatus.RESOLVED)
            self.assertFalse(session.is_escalated)
            self.assertIsNone(session.assigned_operator_id)
            self.assertEqual(ticket_2.status, TicketStatus.RESOLVED)

            res_resolved_queue = operator_client.get(reverse('support:desk_queue_partial') + '?tab=resolved')
            self.assertEqual(res_resolved_queue.status_code, 200)
            self.assertIn(session_uuid, res_resolved_queue.content.decode('utf-8'))

            # Customer can no longer post to resolved session via /message/
            res_after_resolve = customer_client.post(
                reverse('support:chat_message'),
                data=json.dumps({'message': 'Ще одне питання'}),
                content_type='application/json',
            )
            self.assertEqual(res_after_resolve.status_code, 409)

    @override_settings(**E2E_TELEGRAM_SETTINGS)
    def test_ai_tool_escalation_off_hours_telegram_callbacks_and_guest_reset(self):
        guest_client = Client()
        webhook_client = Client()

        tg_calls: list[tuple[str, dict[str, Any]]] = []
        topic_seq = {'n': 6000}
        msg_seq = {'n': 9000}

        def fake_tg_post(url: str, json: dict[str, Any] | None = None, timeout: float | None = None):
            method = url.rsplit('/', 1)[-1]
            payload = dict(json or {})
            tg_calls.append((method, payload))
            if method == 'createForumTopic':
                topic_seq['n'] += 1
                return _fake_tg_response({'message_thread_id': topic_seq['n']})
            if method == 'sendMessage':
                msg_seq['n'] += 1
                return _fake_tg_response({'message_id': msg_seq['n']})
            return _fake_tg_response(True)

        async def fake_ai_escalating_stream(
            agent_self,
            history: list[dict[str, Any]],
            request=None,
            session=None,
            **kwargs,
        ):
            result = await sync_to_async(execute_agent_tool, thread_sensitive=True)(
                'escalate_to_operator',
                {
                    'reason': TicketReason.COMPLAINT,
                    'details': 'Гість повідомив про пошкоджену упаковку.',
                },
                request=request,
                session=session,
            )
            yield {
                'escalated': True,
                'ticket_id': result['ticket_id'],
                'support_phone': result['support_phone'],
            }
            yield {'token': 'Я передав ваше звернення адміністратору просто в цей чат.'}

        off_hours_now = datetime(2026, 11, 27, 23, 15, tzinfo=KYIV_TZ)

        with (
            patch('support.telegram.httpx.post', side_effect=fake_tg_post),
            patch('support.agent.client.GeminiSupportAgent.stream_chat_response', new=fake_ai_escalating_stream),
            patch('support.services.timezone.now', return_value=off_hours_now),
        ):
            # 1. Guest triggers AI-tool escalation outside working hours (23:15 Europe/Kyiv)
            res_stream = guest_client.post(
                reverse('support:chat_stream'),
                data=json.dumps({'message': 'Упаковка порвана, покличте менеджера!'}),
                content_type='application/json',
            )
            self.assertEqual(res_stream.status_code, 200)
            sse_body = _read_sse_body(res_stream)
            self.assertIn('"escalated": true', sse_body)

            res_hist = guest_client.get(reverse('support:chat_history'))
            self.assertEqual(res_hist.status_code, 200)
            hist_data = res_hist.json()
            self.assertTrue(hist_data['is_escalated'])
            self.assertFalse(hist_data['is_working_hours'])
            self.assertEqual(hist_data['working_hours_label'], '10:00–22:00')
            self.assertTrue(
                any(
                    m['sender'] == 'system' and '10:00' in m['content']
                    for m in hist_data['messages']
                )
            )

            session = ChatSession.objects.get(session_uuid=hist_data['session_uuid'])
            ticket = SupportTicket.objects.get(session=session)
            self.assertEqual(ticket.reason, TicketReason.COMPLAINT)
            self.assertEqual(ticket.telegram_thread_id, 6001)

            # 2. Operator clicks inline button [Взяти в роботу] in Telegram
            res_cb_claim = webhook_client.post(
                reverse('support:telegram_webhook'),
                data=json.dumps({
                    'update_id': 92001,
                    'callback_query': {
                        'id': 'cb-claim-1',
                        'from': {
                            'id': self.operator.telegram_id,
                            'is_bot': False,
                            'first_name': 'Олена',
                        },
                        'data': f'claim:{ticket.id}',
                        'message': {
                            'message_id': ticket.telegram_control_message_id,
                            'message_thread_id': ticket.telegram_thread_id,
                            'chat': {'id': -1001999888777, 'type': 'supergroup'},
                        },
                    },
                }),
                content_type='application/json',
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=E2E_TELEGRAM_SETTINGS['TELEGRAM_WEBHOOK_SECRET'],
            )
            self.assertEqual(res_cb_claim.status_code, 200)
            ticket.refresh_from_db()
            session.refresh_from_db()
            self.assertEqual(ticket.status, TicketStatus.IN_PROGRESS)
            self.assertEqual(session.assigned_operator_id, self.operator.id)

            # 3. Guest clicks "Новий діалог" (/reset/) -> closes active ticket & closes Telegram topic
            tg_calls.clear()
            res_reset = guest_client.post(reverse('support:chat_reset'))
            self.assertEqual(res_reset.status_code, 200)
            new_uuid = res_reset.json()['session_uuid']
            self.assertNotEqual(new_uuid, str(session.session_uuid))

            session.refresh_from_db()
            ticket.refresh_from_db()
            self.assertEqual(session.status, ChatSessionStatus.RESOLVED)
            self.assertEqual(ticket.status, TicketStatus.RESOLVED)
            self.assertIn('closeForumTopic', [m for m, _ in tg_calls])


class SupportDeskResponsiveAndAssetAuditTest(TestCase):
    """Verify mobile/tablet desk UX contracts, compiled Tailwind CSS, and zero external CDNs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.node_bin = shutil.which('node')
        desk_template = Path(settings.BASE_DIR) / 'templates' / 'support' / 'desk' / 'index.html'
        desk_src = desk_template.read_text(encoding='utf-8')
        match = re.search(
            r'function supportDeskController\(initial\)\s*\{.*?\n\}',
            desk_src,
            re.DOTALL,
        )
        assert match is not None, 'supportDeskController function not found in desk/index.html'
        cls.desk_controller_src = match.group(0)

    def setUp(self):
        cache.clear()
        self.operator = User.objects.create_user(
            email='responsive.op@example.com',
            password='Password123!',
            first_name='Олена',
            last_name='Коваль',
            role=Role.RESTAURANT_ADMIN,
        )
        self.customer = User.objects.create_user(
            email='responsive.cust@example.com',
            password='Password123!',
            first_name='Тарас',
            last_name='Бондар',
            role=Role.CUSTOMER,
        )
        self.session = ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
            assigned_operator=self.operator,
        )
        SupportTicket.objects.create(
            session=self.session,
            reason=TicketReason.HUMAN_REQUESTED,
            status=TicketStatus.IN_PROGRESS,
            assigned_operator=self.operator,
        )
        self.client.force_login(self.operator)

    def test_desk_rendered_html_contains_mobile_responsive_and_a11y_contracts(self):
        res = self.client.get(reverse('support:desk_session', args=[self.session.session_uuid]))
        self.assertEqual(res.status_code, 200)
        html = res.content.decode('utf-8')

        # Dynamic viewport height & safe-area insets
        self.assertIn('desk-shell-height', html)
        self.assertIn('desk-safe-bottom', html)
        self.assertIn('overscroll-contain', html)

        # Mobile Queue <-> Chat toggle & Context slide-over drawer accessibility attributes
        self.assertIn('@keydown.escape.window="if (contextDrawerOpen) closeContextDrawer()"', html)
        self.assertIn('id="desk-queue-panel"', html)
        self.assertIn('id="desk-thread-main"', html)
        self.assertIn('id="desk-context-open-btn"', html)
        self.assertIn('aria-controls="desk-context-panel"', html)
        self.assertIn('id="desk-context-close-btn"', html)
        self.assertIn('w-[min(24rem,88vw)]', html)
        self.assertIn('lg:static lg:inset-auto', html)
        self.assertIn('z-[45]', html)

        # Header action bar must wrap cleanly inside narrow mobile viewports (no shrink-0 overflow)
        self.assertIn('flex flex-wrap items-center gap-2 min-w-0 max-w-full', html)
        self.assertNotIn('flex flex-wrap items-center gap-2 shrink-0', html)
        # Operator navbar label hides below lg to prevent tablet (768px) horizontal overflow
        self.assertIn('<span class="hidden lg:inline">Підтримка</span>', html)

        # Touch targets >= 44px & iOS anti-zoom text-base sm:text-sm on inputs
        self.assertIn('min-h-[44px]', html)
        self.assertIn('min-w-[44px]', html)
        self.assertIn('text-base sm:text-sm', html)

        # Dynamic working hours label from settings
        with override_settings(SUPPORT_WORK_START_HOUR=9, SUPPORT_WORK_END_HOUR=21):
            res_hours = self.client.get(reverse('support:desk_index'))
            self.assertEqual(res_hours.status_code, 200)
            self.assertIn('09:00–21:00', res_hours.content.decode('utf-8'))

    def test_desk_controller_mobile_drawer_focus_inert_and_resize_behavior_in_node(self):
        if not self.node_bin:
            self.fail('Node.js executable is required to verify mobile drawer JS behavior.')

        script = f"""
        const vm = require('vm');
        let mqListener = null;
        const mq = {{
          matches: false,
          addEventListener(ev, fn) {{ if (ev === 'change') mqListener = fn; }},
          removeEventListener(ev, fn) {{ if (ev === 'change' && mqListener === fn) mqListener = null; }},
        }};

        let threadMain = {{ inert: false }};
        const queuePanel = {{ inert: false }};
        const siteHeader = {{ inert: false }};
        const focusLog = [];
        let closeBtn = {{ focus(opts) {{ focusLog.push(['close-1', Boolean(opts && opts.preventScroll)]); }} }};
        const openBtn = {{ focus(opts) {{ focusLog.push(['open', Boolean(opts && opts.preventScroll)]); }} }};
        const bodyListeners = {{}};

        const doc = {{
          hidden: false,
          cookie: 'csrftoken=mobile-test',
          body: {{
            addEventListener(ev, fn) {{ bodyListeners[ev] = fn; }},
            removeEventListener(ev, fn) {{ if (bodyListeners[ev] === fn) delete bodyListeners[ev]; }},
          }},
          getElementById(id) {{
            if (id === 'desk-thread-main') return threadMain;
            if (id === 'desk-queue-panel') return queuePanel;
            if (id === 'desk-context-close-btn') return closeBtn;
            if (id === 'desk-context-open-btn') return openBtn;
            return null;
          }},
          querySelector(sel) {{
            if (sel === 'body > header') return siteHeader;
            return null;
          }},
          addEventListener() {{}},
          removeEventListener() {{}},
        }};

        const win = {{
          innerWidth: 375,
          matchMedia: () => mq,
          history: {{ replaceState() {{}} }},
          htmx: {{ ajax() {{}} }},
        }};

        const ctx = vm.createContext({{
          window: win,
          document: doc,
          URLSearchParams,
          setTimeout: () => 1,
          clearTimeout() {{}},
        }});
        vm.runInContext({json.dumps(self.desk_controller_src)}, ctx);

        const desk = ctx.supportDeskController({{
          activeTab: 'in_progress',
          searchQuery: '',
          activeSessionUuid: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
          lastMessageId: 1,
          sessionStatus: 'ESCALATED',
          assignedOperatorId: '1',
          isOwnedByMe: true,
        }});
        desk.$nextTick = (fn) => fn();
        desk.init();

        const initialDesktop = desk.isDesktopViewport;

        // Open drawer on mobile: inert=true across thread, queue, and site header; focuses close button
        desk.openContextDrawer();
        const openState = {{
          drawerOpen: desk.contextDrawerOpen,
          threadInert: threadMain.inert,
          queueInert: queuePanel.inert,
          headerInert: siteHeader.inert,
          focusLog: [...focusLog],
        }};

        // Simulate HTMX #desk-workspace swap while mobile drawer is open (new DOM nodes)
        threadMain = {{ inert: false }};
        closeBtn = {{ focus(opts) {{ focusLog.push(['close-2', Boolean(opts && opts.preventScroll)]); }} }};
        bodyListeners['htmx:afterSwap']({{ detail: {{ target: {{ id: 'desk-workspace' }} }} }});
        const afterSwapWhileOpen = {{
          drawerOpen: desk.contextDrawerOpen,
          threadInert: threadMain.inert,
          queueInert: queuePanel.inert,
          headerInert: siteHeader.inert,
          lastFocus: focusLog[focusLog.length - 1],
        }};

        // Close drawer on mobile: inert=false everywhere, restores focus to trigger button
        desk.closeContextDrawer();
        const closedState = {{
          drawerOpen: desk.contextDrawerOpen,
          threadInert: threadMain.inert,
          queueInert: queuePanel.inert,
          headerInert: siteHeader.inert,
          lastFocus: focusLog[focusLog.length - 1],
        }};

        // isDesktop() follows matchMedia('(min-width: 1024px)') even if innerWidth differs
        win.innerWidth = 1030;
        mq.matches = false;
        const followsMatchMediaMobile = !desk.isDesktop();

        // Re-open on mobile, then resize viewport to desktop (>= 1024px)
        desk.openContextDrawer();
        const focusCountBeforeResize = focusLog.length;
        win.innerWidth = 1280;
        mq.matches = true;
        mqListener({{ matches: true }});
        const afterResizeDesktop = {{
          drawerOpen: desk.contextDrawerOpen,
          isDesktopViewport: desk.isDesktopViewport,
          threadInert: threadMain.inert,
          queueInert: queuePanel.inert,
          headerInert: siteHeader.inert,
          focusStolenOnResize: focusLog.length !== focusCountBeforeResize,
        }};

        // Back to mobile, open drawer, then tap backToQueue()
        win.innerWidth = 390;
        mq.matches = false;
        mqListener({{ matches: false }});
        desk.openContextDrawer();
        desk.backToQueue();
        const afterBackToQueue = {{
          mobileView: desk.mobileView,
          drawerOpen: desk.contextDrawerOpen,
          threadInert: threadMain.inert,
          queueInert: queuePanel.inert,
          headerInert: siteHeader.inert,
        }};

        desk.destroy();
        const listenerCleanedUp = mqListener === null;

        console.log(JSON.stringify({{
          initialDesktop,
          openState,
          afterSwapWhileOpen,
          closedState,
          followsMatchMediaMobile,
          afterResizeDesktop,
          afterBackToQueue,
          listenerCleanedUp,
        }}));
        """

        proc = subprocess.run(  # noqa: S603
            [self.node_bin, '-'],
            input=script,
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
        )
        result = json.loads(proc.stdout)

        self.assertFalse(result['initialDesktop'])
        self.assertTrue(result['openState']['drawerOpen'])
        self.assertTrue(result['openState']['threadInert'])
        self.assertTrue(result['openState']['queueInert'])
        self.assertTrue(result['openState']['headerInert'])
        self.assertEqual(result['openState']['focusLog'], [['close-1', True]])

        self.assertTrue(result['afterSwapWhileOpen']['drawerOpen'])
        self.assertTrue(result['afterSwapWhileOpen']['threadInert'])
        self.assertTrue(result['afterSwapWhileOpen']['queueInert'])
        self.assertTrue(result['afterSwapWhileOpen']['headerInert'])
        self.assertEqual(result['afterSwapWhileOpen']['lastFocus'], ['close-2', True])

        self.assertFalse(result['closedState']['drawerOpen'])
        self.assertFalse(result['closedState']['threadInert'])
        self.assertFalse(result['closedState']['queueInert'])
        self.assertFalse(result['closedState']['headerInert'])
        self.assertEqual(result['closedState']['lastFocus'], ['open', True])

        self.assertTrue(result['followsMatchMediaMobile'])

        self.assertFalse(result['afterResizeDesktop']['drawerOpen'])
        self.assertTrue(result['afterResizeDesktop']['isDesktopViewport'])
        self.assertFalse(result['afterResizeDesktop']['threadInert'])
        self.assertFalse(result['afterResizeDesktop']['queueInert'])
        self.assertFalse(result['afterResizeDesktop']['headerInert'])
        self.assertFalse(result['afterResizeDesktop']['focusStolenOnResize'])

        self.assertEqual(result['afterBackToQueue']['mobileView'], 'queue')
        self.assertFalse(result['afterBackToQueue']['drawerOpen'])
        self.assertFalse(result['afterBackToQueue']['threadInert'])
        self.assertFalse(result['afterBackToQueue']['queueInert'])
        self.assertFalse(result['afterBackToQueue']['headerInert'])
        self.assertTrue(result['listenerCleanedUp'])

    def test_compiled_tailwind_css_contains_desk_utilities_and_zero_external_cdns(self):
        tailwind_css = (
            Path(settings.BASE_DIR) / 'static' / 'css' / 'tailwind.css'
        ).read_text(encoding='utf-8')

        required_snippets = [
            'desk-shell-height',
            '100dvh',
            'desk-safe-bottom',
            'safe-area-inset-bottom',
            'overscroll-contain',
            r'min-h-\[44px\]',
            r'min-w-\[44px\]',
            r'min-h-\[48px\]',
            r'lg\:static',
            r'lg\:inset-auto',
            r'lg\:shadow-none',
            r'xl\:w-\[340px\]',
            r'xl\:w-\[360px\]',
            r'z-\[45\]',
            'max-w-full',
            'width:min(24rem,88vw)',
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, tailwind_css, f'Missing compiled CSS utility: {snippet}')

        forbidden_cdns = (
            'cdn.tailwindcss.com',
            'unpkg.com',
            'cdnjs.cloudflare.com',
            'cdn.jsdelivr.net',
            'fonts.googleapis.com',
            'fonts.gstatic.com',
        )
        for cdn in forbidden_cdns:
            self.assertNotIn(cdn, tailwind_css, f'External CDN found in tailwind.css: {cdn}')

        templates_root = Path(settings.BASE_DIR) / 'templates'
        for html_file in templates_root.rglob('*.html'):
            content = html_file.read_text(encoding='utf-8')
            for cdn in forbidden_cdns:
                self.assertNotIn(
                    cdn,
                    content,
                    f'External CDN {cdn} found in template {html_file.relative_to(templates_root)}',
                )
