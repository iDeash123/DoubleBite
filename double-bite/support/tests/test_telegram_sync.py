import json
from io import StringIO
from unittest.mock import MagicMock, patch

import httpx
from accounts.admin import UserAdmin
from accounts.models import Role
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from menu.models import Category, Dish
from orders.models import Order, OrderItem

from support import telegram as tg
from support.admin import SupportTicketAdmin
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

TEST_TELEGRAM_SETTINGS = {
    'TELEGRAM_BOT_ENABLED': True,
    'TELEGRAM_BOT_TOKEN': '123456:TEST-TOKEN-SECRET',
    'TELEGRAM_SUPPORT_CHAT_ID': '-1001999888777',
    'TELEGRAM_WEBHOOK_SECRET': 'super-secret-webhook-token',
    'SITE_DOMAIN': 'doublebite.ua',
    'SITE_PROTOCOL': 'https',
}


def _fake_tg_response(result, *, status_code: int = 200, ok: bool = True):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {'ok': ok, 'result': result}
    return resp


class TelegramOutboundSyncTest(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.customer = User.objects.create_user(
            email='tg-customer@example.com',
            password='Password123!',
            first_name='Олена',
            last_name='Сидоренко',
            phone='+380671112233',
            role=Role.CUSTOMER,
        )
        self.operator = User.objects.create_user(
            email='tg-operator@example.com',
            password='Password123!',
            first_name='Марія',
            last_name='Коваль',
            role=Role.RESTAURANT_ADMIN,
            telegram_id=555001,
        )
        self.other_operator = User.objects.create_user(
            email='tg-operator2@example.com',
            password='Password123!',
            first_name='Іван',
            last_name='Петренко',
            role=Role.RESTAURANT_ADMIN,
            telegram_id=555002,
        )

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_escalate_creates_forum_topic_with_history_card_and_buttons(self):
        category = Category.objects.create(name='Бургери', slug='burgers')
        dish = Dish.objects.create(
            category=category,
            title='Double Smash',
            slug='double-smash',
            price=250,
            weight_grams=280,
            is_available=True,
        )
        order = Order.objects.create(
            user=self.customer,
            order_number='DB-7701',
            customer_name='Олена Сидоренко',
            customer_phone='+380671112233',
            customer_email=self.customer.email,
            delivery_address='Київ, Хрещатик 1',
            total_amount=250,
        )
        OrderItem.objects.create(
            order=order,
            dish=dish,
            dish_title=dish.title,
            price=250,
            quantity=1,
        )

        session = ChatSession.objects.create(user=self.customer)
        ChatMessage.objects.create(
            session=session,
            role=MessageRole.USER,
            source=MessageSource.WEB,
            content='Де моє замовлення <script>alert(1)</script>?',
        )
        ChatMessage.objects.create(
            session=session,
            role=MessageRole.ASSISTANT,
            source=MessageSource.AI,
            content='Зараз перевірю статус DB-7701.',
        )

        calls: list[tuple[str, dict]] = []

        def fake_post(url, json=None, timeout=None):
            method = url.rsplit('/', 1)[-1]
            calls.append((method, dict(json or {})))
            if method == 'createForumTopic':
                return _fake_tg_response({'message_thread_id': 4242})
            if method == 'sendMessage':
                return _fake_tg_response({'message_id': 9001})
            if method == 'pinChatMessage':
                return _fake_tg_response(True)
            return _fake_tg_response(True)

        with patch('support.telegram.httpx.post', side_effect=fake_post):
            ticket = ChatSessionService.escalate_session(
                session,
                TicketReason.ORDER_ISSUE,
                'Курʼєр затримується & клієнт хвилюється',
                order=order,
            )

        ticket.refresh_from_db()
        self.assertEqual(ticket.telegram_thread_id, 4242)
        self.assertEqual(ticket.telegram_control_message_id, 9001)

        methods = [m for m, _ in calls]
        self.assertEqual(methods, ['createForumTopic', 'sendMessage', 'pinChatMessage'])

        topic_payload = calls[0][1]
        self.assertEqual(topic_payload['chat_id'], -1001999888777)
        self.assertIn(f'#{ticket.id}', topic_payload['name'])
        self.assertIn('Олена Сидоренко', topic_payload['name'])
        self.assertIn('Проблема із замовленням', topic_payload['name'])
        self.assertLessEqual(len(topic_payload['name']), 128)

        card_payload = calls[1][1]
        self.assertEqual(card_payload['message_thread_id'], 4242)
        self.assertEqual(card_payload['parse_mode'], 'HTML')
        self.assertIn('DB-7701', card_payload['text'])
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', card_payload['text'])
        self.assertNotIn('<script>', card_payload['text'])
        self.assertIn('Зараз перевірю статус DB-7701.', card_payload['text'])
        self.assertIn('Курʼєр затримується &amp; клієнт хвилюється', card_payload['text'])

        buttons = [
            btn
            for row in card_payload['reply_markup']['inline_keyboard']
            for btn in row
        ]
        cb_data = [b.get('callback_data') for b in buttons if 'callback_data' in b]
        self.assertEqual(
            cb_data,
            [f'claim:{ticket.id}', f'return_ai:{ticket.id}', f'resolve:{ticket.id}'],
        )
        url_buttons = [b.get('url') for b in buttons if 'url' in b]
        self.assertEqual(
            url_buttons,
            [f'https://doublebite.ua/support/desk/{session.session_uuid}/'],
        )

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_topic_title_is_truncated_to_telegram_128_char_limit(self):
        long_user = User.objects.create_user(
            email='longname@example.com',
            password='Password123!',
            first_name='Олександра-' * 8,
            last_name='Довгопрізвище-' * 5,
            role=Role.CUSTOMER,
        )
        session = ChatSession.objects.create(user=long_user)
        with patch('support.telegram.call_telegram_api', return_value={'message_thread_id': 77}):
            ticket = ChatSessionService.escalate_session(
                session,
                TicketReason.COMPLAINT,
                'Довге імʼя',
            )
        title = tg.format_telegram_topic_name(ticket)
        self.assertLessEqual(len(title), 128)
        self.assertTrue(title.endswith('…'))

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_customer_and_web_operator_messages_mirror_to_topic(self):
        session = ChatSession.objects.create(user=self.customer)
        with patch('support.telegram.httpx.post') as mock_post:
            mock_post.side_effect = [
                _fake_tg_response({'message_thread_id': 501}),
                _fake_tg_response({'message_id': 601}),
                _fake_tg_response(True),
            ]
            ticket = ChatSessionService.escalate_session(
                session, TicketReason.HUMAN_REQUESTED, 'Питання'
            )

        with patch('support.telegram.httpx.post') as mock_post:
            mock_post.return_value = _fake_tg_response({'message_id': 602})
            cust_msg = SupportDeskService.send_customer_message(
                session, 'Підкажіть час доставки, будь ласка.'
            )

        cust_msg.refresh_from_db()
        self.assertEqual(cust_msg.telegram_message_id, 602)
        sent_cust = mock_post.call_args.kwargs['json']
        self.assertEqual(sent_cust['message_thread_id'], 501)
        self.assertIn('Олена Сидоренко', sent_cust['text'])
        self.assertIn('Підкажіть час доставки, будь ласка.', sent_cust['text'])

        with patch('support.telegram.httpx.post') as mock_post:
            mock_post.return_value = _fake_tg_response({'message_id': 603})
            SupportDeskService.claim_session(session, self.operator)

        with patch('support.telegram.httpx.post') as mock_post:
            mock_post.return_value = _fake_tg_response({'message_id': 604})
            op_msg = SupportDeskService.send_operator_message(
                session,
                self.operator,
                'Курʼєр буде через 15 хвилин!',
                source=MessageSource.WEB,
            )

        op_msg.refresh_from_db()
        self.assertEqual(op_msg.telegram_message_id, 604)
        sent_op = mock_post.call_args.kwargs['json']
        self.assertEqual(sent_op['message_thread_id'], 501)
        self.assertIn('[Веб-пульт · Марія Коваль]', sent_op['text'])
        self.assertIn('Курʼєр буде через 15 хвилин!', sent_op['text'])

        # Telegram-originated operator reply MUST NOT echo back to Telegram
        with patch('support.telegram.httpx.post') as mock_post:
            SupportDeskService.send_operator_message(
                session,
                self.operator,
                'Відповідь прямо з Telegram',
                source=MessageSource.TELEGRAM,
                telegram_message_id=777111,
            )
        mock_post.assert_not_called()
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatus.IN_PROGRESS)

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_proactive_takeover_creates_topic_in_progress(self):
        session = ChatSession.objects.create(user=self.customer)
        calls: list[tuple[str, dict]] = []

        def fake_post(url, json=None, timeout=None):
            method = url.rsplit('/', 1)[-1]
            calls.append((method, dict(json or {})))
            if method == 'createForumTopic':
                return _fake_tg_response({'message_thread_id': 888})
            if method == 'sendMessage':
                return _fake_tg_response({'message_id': 889})
            return _fake_tg_response(True)

        with patch('support.telegram.httpx.post', side_effect=fake_post):
            result = SupportDeskService.proactive_takeover(session, self.operator)

        result.ticket.refresh_from_db()
        self.assertEqual(result.ticket.telegram_thread_id, 888)
        self.assertEqual(result.ticket.telegram_control_message_id, 889)
        card_payload = calls[1][1]
        self.assertIn('В роботі', card_payload['text'])
        self.assertIn('Марія Коваль', card_payload['text'])

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_telegram_network_failures_are_fail_silent(self):
        session = ChatSession.objects.create(user=self.customer)
        with patch(
            'support.telegram.httpx.post',
            side_effect=httpx.ConnectTimeout('Telegram unreachable'),
        ):
            ticket = ChatSessionService.escalate_session(
                session, TicketReason.COMPLAINT, 'Мережа впала'
            )

        session.refresh_from_db()
        self.assertEqual(session.status, ChatSessionStatus.ESCALATED)
        self.assertIsNone(ticket.telegram_thread_id)

        with patch(
            'support.telegram.httpx.post',
            return_value=_fake_tg_response(None, status_code=500, ok=False),
        ):
            msg = SupportDeskService.send_customer_message(session, 'Повідомлення попри збій')
        self.assertIsNotNone(msg.id)

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_close_topic_on_resolve_return_to_ai_and_client_reset(self):
        for action_name in ('resolve', 'return_ai', 'reset'):
            customer = User.objects.create_user(
                email=f'{action_name}@example.com',
                password='Password123!',
                role=Role.CUSTOMER,
            )
            session = ChatSession.objects.create(user=customer)
            with patch('support.telegram.httpx.post') as mock_post:
                mock_post.side_effect = [
                    _fake_tg_response({'message_thread_id': 910}),
                    _fake_tg_response({'message_id': 911}),
                    _fake_tg_response(True),
                ]
                ticket = ChatSessionService.escalate_session(
                    session, TicketReason.HUMAN_REQUESTED, f'Тест {action_name}'
                )

            calls: list[tuple[str, dict]] = []

            def record_post(url, json=None, timeout=None, *, sink=calls):
                method = url.rsplit('/', 1)[-1]
                sink.append((method, dict(json or {})))
                return _fake_tg_response(True)

            with patch('support.telegram.httpx.post', side_effect=record_post):
                if action_name == 'resolve':
                    SupportDeskService.resolve_session(session, self.operator)
                elif action_name == 'return_ai':
                    SupportDeskService.return_to_ai(session, self.operator)
                else:
                    factory = Client()
                    factory.force_login(customer)
                    factory.post(reverse('support:chat_reset'))

            methods = [m for m, _ in calls]
            self.assertIn('editMessageText', methods)
            self.assertIn('sendMessage', methods)
            self.assertIn('closeForumTopic', methods)

            edit_payload = next(p for m, p in calls if m == 'editMessageText')
            self.assertEqual(edit_payload['message_id'], 911)
            self.assertEqual(edit_payload['reply_markup'], {'inline_keyboard': []})

            close_payload = next(p for m, p in calls if m == 'closeForumTopic')
            self.assertEqual(close_payload['message_thread_id'], 910)

            ticket.refresh_from_db()
            self.assertEqual(ticket.status, TicketStatus.RESOLVED)


class TelegramWebhookTest(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.webhook_url = reverse('support:telegram_webhook')
        self.headers = {'HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN': 'super-secret-webhook-token'}
        self.customer = User.objects.create_user(
            email='wh-cust@example.com',
            password='Password123!',
            first_name='Тарас',
            role=Role.CUSTOMER,
            telegram_id=999111,
        )
        self.operator = User.objects.create_user(
            email='wh-op@example.com',
            password='Password123!',
            first_name='Ольга',
            last_name='Мельник',
            role=Role.RESTAURANT_ADMIN,
            telegram_id=777001,
        )
        self.other_operator = User.objects.create_user(
            email='wh-op2@example.com',
            password='Password123!',
            first_name='Богдан',
            last_name='Савчук',
            role=Role.RESTAURANT_ADMIN,
            telegram_id=777002,
        )

    def _create_escalated_ticket(
        self,
        *,
        thread_id: int = 3301,
        control_msg_id: int = 4401,
        guest_token: str = '',
    ):
        session = (
            ChatSession.objects.create(guest_token=guest_token)
            if guest_token
            else ChatSession.objects.create(user=self.customer)
        )
        with patch('support.telegram.httpx.post') as mock_post:
            mock_post.side_effect = [
                _fake_tg_response({'message_thread_id': thread_id}),
                _fake_tg_response({'message_id': control_msg_id}),
                _fake_tg_response(True),
            ]
            ticket = ChatSessionService.escalate_session(
                session, TicketReason.COMPLAINT, 'Проблема з доставкою'
            )
        ticket.refresh_from_db()
        return session, ticket

    def test_disabled_by_default_in_tests_returns_503(self):
        resp = self.client.post(
            self.webhook_url,
            data=json.dumps({'update_id': 1}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 503)

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_rejects_wrong_secret_missing_secret_and_foreign_chat_id(self):
        session, ticket = self._create_escalated_ticket()

        # Missing secret
        resp_missing = self.client.post(
            self.webhook_url,
            data=json.dumps({'update_id': 1}),
            content_type='application/json',
        )
        self.assertEqual(resp_missing.status_code, 403)

        # Wrong secret
        resp_wrong = self.client.post(
            self.webhook_url,
            data=json.dumps({'update_id': 2}),
            content_type='application/json',
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='wrong-secret',
        )
        self.assertEqual(resp_wrong.status_code, 403)

        # Foreign chat_id on message
        resp_foreign_msg = self.client.post(
            self.webhook_url,
            data=json.dumps({
                'update_id': 3,
                'message': {
                    'message_id': 101,
                    'message_thread_id': ticket.telegram_thread_id,
                    'chat': {'id': -1009999999999, 'type': 'supergroup'},
                    'from': {'id': 777001, 'is_bot': False, 'first_name': 'Ольга'},
                    'text': 'Спроба з чужого чату',
                },
            }),
            content_type='application/json',
            **self.headers,
        )
        self.assertEqual(resp_foreign_msg.status_code, 403)
        self.assertFalse(
            ChatMessage.objects.filter(session=session, role=MessageRole.OPERATOR).exists()
        )

        # Foreign chat_id on callback_query
        resp_foreign_cb = self.client.post(
            self.webhook_url,
            data=json.dumps({
                'update_id': 4,
                'callback_query': {
                    'id': 'cb-foreign',
                    'from': {'id': 777001, 'is_bot': False},
                    'data': f'claim:{ticket.id}',
                    'message': {
                        'message_id': 4401,
                        'chat': {'id': -1009999999999, 'type': 'supergroup'},
                    },
                },
            }),
            content_type='application/json',
            **self.headers,
        )
        self.assertEqual(resp_foreign_cb.status_code, 403)

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_incoming_topic_reply_saves_message_auto_claims_publishes_sse_and_deduplicates(self):
        session, ticket = self._create_escalated_ticket()
        update_payload = {
            'update_id': 10,
            'message': {
                'message_id': 55501,
                'message_thread_id': ticket.telegram_thread_id,
                'chat': {'id': -1001999888777, 'type': 'supergroup'},
                'from': {'id': 777001, 'is_bot': False, 'first_name': 'Ольга'},
                'text': 'Вітаю! Вже передали кухні на пріоритет.',
            },
        }

        with (
            patch('support.services.publish_session_event') as mock_session_pub,
            patch('support.services.publish_desk_event') as mock_desk_pub,
            patch('support.telegram.httpx.post', return_value=_fake_tg_response(True)) as mock_tg_post,
        ):
            resp1 = self.client.post(
                self.webhook_url,
                data=json.dumps(update_payload),
                content_type='application/json',
                **self.headers,
            )
            self.assertEqual(resp1.status_code, 200)
            body1 = resp1.json()
            self.assertTrue(body1['ok'])
            msg_id = body1['message_id']

            # Duplicate delivery with same telegram_message_id -> idempotent no-op
            resp2 = self.client.post(
                self.webhook_url,
                data=json.dumps(update_payload),
                content_type='application/json',
                **self.headers,
            )
            self.assertEqual(resp2.status_code, 200)
            self.assertTrue(resp2.json()['duplicate'])

        op_msgs = ChatMessage.objects.filter(session=session, role=MessageRole.OPERATOR)
        self.assertEqual(op_msgs.count(), 1)
        saved = op_msgs.get()
        self.assertEqual(saved.id, msg_id)
        self.assertEqual(saved.source, MessageSource.TELEGRAM)
        self.assertEqual(saved.sender_user, self.operator)
        self.assertEqual(saved.telegram_message_id, 55501)

        session.refresh_from_db()
        ticket.refresh_from_db()
        self.assertEqual(session.assigned_operator, self.operator)
        self.assertEqual(ticket.status, TicketStatus.IN_PROGRESS)
        self.assertEqual(ticket.assigned_operator, self.operator)

        # SSE events published exactly once (auto-join system msg + operator_joined + operator msg)
        event_types = [call.args[1] for call in mock_session_pub.call_args_list]
        self.assertEqual(event_types, ['message', 'operator_joined', 'message'])
        self.assertEqual(mock_desk_pub.call_count, 1)

        # Card was updated via editMessageText, and Telegram reply was NOT echoed back as sendMessage
        tg_methods = [call.args[0].rsplit('/', 1)[-1] for call in mock_tg_post.call_args_list]
        self.assertEqual(tg_methods, ['editMessageText'])

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_ignores_bot_non_operator_attribution_general_messages_and_unknown_thread(self):
        session, ticket = self._create_escalated_ticket()

        # Bot message ignored
        resp_bot = self.client.post(
            self.webhook_url,
            data=json.dumps({
                'update_id': 20,
                'message': {
                    'message_id': 55601,
                    'message_thread_id': ticket.telegram_thread_id,
                    'chat': {'id': -1001999888777},
                    'from': {'id': 999999, 'is_bot': True, 'first_name': 'Bot'},
                    'text': 'Echo',
                },
            }),
            content_type='application/json',
            **self.headers,
        )
        self.assertEqual(resp_bot.status_code, 200)
        self.assertEqual(resp_bot.json()['ignored'], 'bot_message')

        # General chat message without thread_id ignored
        resp_no_thread = self.client.post(
            self.webhook_url,
            data=json.dumps({
                'update_id': 21,
                'message': {
                    'message_id': 55602,
                    'chat': {'id': -1001999888777},
                    'from': {'id': 777001, 'is_bot': False},
                    'text': 'Повідомлення поза темою',
                },
            }),
            content_type='application/json',
            **self.headers,
        )
        self.assertEqual(resp_no_thread.status_code, 200)
        self.assertEqual(resp_no_thread.json()['ignored'], 'no_thread_id')

        # Unknown thread ignored
        resp_unknown = self.client.post(
            self.webhook_url,
            data=json.dumps({
                'update_id': 22,
                'message': {
                    'message_id': 55603,
                    'message_thread_id': 999999,
                    'chat': {'id': -1001999888777},
                    'from': {'id': 777001, 'is_bot': False},
                    'text': 'Невідома тема',
                },
            }),
            content_type='application/json',
            **self.headers,
        )
        self.assertEqual(resp_unknown.status_code, 200)
        self.assertEqual(resp_unknown.json()['ignored'], 'unknown_thread')

        # Customer's telegram_id must NOT be attributed as sender_user operator
        with patch('support.telegram.httpx.post', return_value=_fake_tg_response(True)):
            resp_unlinked = self.client.post(
                self.webhook_url,
                data=json.dumps({
                    'update_id': 23,
                    'message': {
                        'message_id': 55604,
                        'message_thread_id': ticket.telegram_thread_id,
                        'chat': {'id': -1001999888777},
                        'from': {'id': 999111, 'is_bot': False, 'first_name': 'Менеджер'},
                        'text': 'Відповідь без привʼязаного адміна',
                    },
                }),
                content_type='application/json',
                **self.headers,
            )
        self.assertEqual(resp_unlinked.status_code, 200)
        saved = ChatMessage.objects.get(telegram_message_id=55604)
        self.assertIsNone(saved.sender_user)

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_callback_buttons_claim_conflict_return_ai_and_resolve(self):
        # 1. Claim -> Return to AI
        session1, ticket1 = self._create_escalated_ticket(thread_id=6101, control_msg_id=6201)
        with patch('support.telegram.httpx.post', return_value=_fake_tg_response(True)):
            resp_claim = self.client.post(
                self.webhook_url,
                data=json.dumps({
                    'update_id': 30,
                    'callback_query': {
                        'id': 'cb-1',
                        'from': {'id': 777001, 'is_bot': False, 'first_name': 'Ольга'},
                        'data': f'claim:{ticket1.id}',
                        'message': {
                            'message_id': 6201,
                            'message_thread_id': 6101,
                            'chat': {'id': -1001999888777},
                        },
                    },
                }),
                content_type='application/json',
                **self.headers,
            )
        self.assertEqual(resp_claim.status_code, 200)
        session1.refresh_from_db()
        ticket1.refresh_from_db()
        self.assertEqual(session1.assigned_operator, self.operator)
        self.assertEqual(ticket1.status, TicketStatus.IN_PROGRESS)

        # Second operator trying to claim gets conflict alert
        with patch('support.telegram.httpx.post', return_value=_fake_tg_response(True)) as mock_cb:
            resp_conflict = self.client.post(
                self.webhook_url,
                data=json.dumps({
                    'update_id': 31,
                    'callback_query': {
                        'id': 'cb-conflict',
                        'from': {'id': 777002, 'is_bot': False, 'first_name': 'Богдан'},
                        'data': f'claim:{ticket1.id}',
                        'message': {
                            'message_id': 6201,
                            'message_thread_id': 6101,
                            'chat': {'id': -1001999888777},
                        },
                    },
                }),
                content_type='application/json',
                **self.headers,
            )
        self.assertEqual(resp_conflict.status_code, 200)
        self.assertEqual(resp_conflict.json()['status'], 'conflict')
        cb_answer = mock_cb.call_args.kwargs['json']
        self.assertTrue(cb_answer['show_alert'])

        # Return to AI closes topic & clears buttons
        calls_return: list[tuple[str, dict]] = []

        def record_return(url, json=None, timeout=None):
            calls_return.append((url.rsplit('/', 1)[-1], dict(json or {})))
            return _fake_tg_response(True)

        with patch('support.telegram.httpx.post', side_effect=record_return):
            resp_return = self.client.post(
                self.webhook_url,
                data=json.dumps({
                    'update_id': 32,
                    'callback_query': {
                        'id': 'cb-2',
                        'from': {'id': 777001, 'is_bot': False, 'first_name': 'Ольга'},
                        'data': f'return_ai:{ticket1.id}',
                        'message': {
                            'message_id': 6201,
                            'message_thread_id': 6101,
                            'chat': {'id': -1001999888777},
                        },
                    },
                }),
                content_type='application/json',
                **self.headers,
            )
        self.assertEqual(resp_return.status_code, 200)
        session1.refresh_from_db()
        ticket1.refresh_from_db()
        self.assertEqual(session1.status, ChatSessionStatus.ACTIVE)
        self.assertFalse(session1.is_escalated)
        self.assertEqual(ticket1.status, TicketStatus.RESOLVED)
        return_methods = [m for m, _ in calls_return]
        self.assertIn('closeForumTopic', return_methods)
        self.assertIn('answerCallbackQuery', return_methods)

        # 2. Re-escalate same session after return_ai -> creates new topic, then resolve callback closes it
        with patch('support.telegram.httpx.post') as mock_reescalate:
            mock_reescalate.side_effect = [
                _fake_tg_response({'message_thread_id': 7101}),
                _fake_tg_response({'message_id': 7201}),
                _fake_tg_response(True),
            ]
            ticket2 = ChatSessionService.escalate_session(
                session1, TicketReason.ORDER_ISSUE, 'Повторне звернення'
            )
        ticket2.refresh_from_db()
        self.assertEqual(ticket2.telegram_thread_id, 7101)
        self.assertNotEqual(ticket2.id, ticket1.id)
        session2 = session1
        calls_resolve: list[tuple[str, dict]] = []

        def record_resolve(url, json=None, timeout=None):
            calls_resolve.append((url.rsplit('/', 1)[-1], dict(json or {})))
            return _fake_tg_response(True)

        with patch('support.telegram.httpx.post', side_effect=record_resolve):
            resp_resolve = self.client.post(
                self.webhook_url,
                data=json.dumps({
                    'update_id': 33,
                    'callback_query': {
                        'id': 'cb-3',
                        'from': {'id': 777002, 'is_bot': False, 'first_name': 'Богдан'},
                        'data': f'resolve:{ticket2.id}',
                        'message': {
                            'message_id': 7201,
                            'message_thread_id': 7101,
                            'chat': {'id': -1001999888777},
                        },
                    },
                }),
                content_type='application/json',
                **self.headers,
            )
        self.assertEqual(resp_resolve.status_code, 200)
        session2.refresh_from_db()
        ticket2.refresh_from_db()
        self.assertEqual(session2.status, ChatSessionStatus.RESOLVED)
        self.assertEqual(ticket2.status, TicketStatus.RESOLVED)
        self.assertIn('closeForumTopic', [m for m, _ in calls_resolve])

        # Stale button press on already resolved ticket returns alert and clears buttons
        # and MUST NOT mutate a newer active ticket on the same session.
        with patch('support.telegram.httpx.post', return_value=_fake_tg_response(True)) as mock_stale:
            resp_stale = self.client.post(
                self.webhook_url,
                data=json.dumps({
                    'update_id': 34,
                    'callback_query': {
                        'id': 'cb-stale',
                        'from': {'id': 777002, 'is_bot': False},
                        'data': f'resolve:{ticket2.id}',
                        'message': {
                            'message_id': 7201,
                            'message_thread_id': 7101,
                            'chat': {'id': -1001999888777},
                        },
                    },
                }),
                content_type='application/json',
                **self.headers,
            )
        self.assertEqual(resp_stale.status_code, 200)
        self.assertEqual(resp_stale.json()['status'], 'invalid_state')
        self.assertTrue(mock_stale.called)

        # Create a fresh ticket on a new escalated session, resolve it via return_ai,
        # re-escalate, and press stale resolve:<old_ticket_id> -> new ticket stays OPEN.
        sess_re, old_t = self._create_escalated_ticket(
            thread_id=7501,
            control_msg_id=7601,
            guest_token='stale-reescalate-guest',
        )
        with patch('support.telegram.httpx.post', return_value=_fake_tg_response(True)):
            SupportDeskService.return_to_ai(sess_re, self.operator)
            new_t = ChatSessionService.escalate_session(
                sess_re, TicketReason.COMPLAINT, 'Новий тікет після повернення AI'
            )
        with patch('support.telegram.httpx.post', return_value=_fake_tg_response(True)):
            resp_stale_old = self.client.post(
                self.webhook_url,
                data=json.dumps({
                    'update_id': 3401,
                    'callback_query': {
                        'id': 'cb-stale-old',
                        'from': {'id': 777002, 'is_bot': False},
                        'data': f'resolve:{old_t.id}',
                        'message': {
                            'message_id': 7601,
                            'message_thread_id': 7501,
                            'chat': {'id': -1001999888777},
                        },
                    },
                }),
                content_type='application/json',
                **self.headers,
            )
        self.assertEqual(resp_stale_old.status_code, 200)
        self.assertEqual(resp_stale_old.json()['status'], 'invalid_state')
        new_t.refresh_from_db()
        sess_re.refresh_from_db()
        self.assertEqual(new_t.status, TicketStatus.OPEN)
        self.assertEqual(sess_re.status, ChatSessionStatus.ESCALATED)

        # Reply into closed topic is ignored without creating messages
        resp_closed_msg = self.client.post(
            self.webhook_url,
            data=json.dumps({
                'update_id': 35,
                'message': {
                    'message_id': 77001,
                    'message_thread_id': 7101,
                    'chat': {'id': -1001999888777},
                    'from': {'id': 777002, 'is_bot': False},
                    'text': 'Пізно відповідаю',
                },
            }),
            content_type='application/json',
            **self.headers,
        )
        self.assertEqual(resp_closed_msg.status_code, 200)
        self.assertEqual(resp_closed_msg.json()['ignored'], 'ticket_closed')

        # 3. Unlinked Telegram admin claiming ticket transitions it to IN_PROGRESS,
        # and a second unlinked claim returns conflict instead of duplicating.
        session3, ticket3 = self._create_escalated_ticket(
            thread_id=8101,
            control_msg_id=8201,
            guest_token='tg-cb-guest-3',
        )
        with patch('support.telegram.httpx.post', return_value=_fake_tg_response(True)):
            resp_anon_claim = self.client.post(
                self.webhook_url,
                data=json.dumps({
                    'update_id': 36,
                    'callback_query': {
                        'id': 'cb-anon',
                        'from': {'id': 888999, 'is_bot': False, 'first_name': 'Анна'},
                        'data': f'claim:{ticket3.id}',
                        'message': {
                            'message_id': 8201,
                            'message_thread_id': 8101,
                            'chat': {'id': -1001999888777},
                        },
                    },
                }),
                content_type='application/json',
                **self.headers,
            )
        self.assertEqual(resp_anon_claim.status_code, 200)
        ticket3.refresh_from_db()
        session3.refresh_from_db()
        self.assertEqual(ticket3.status, TicketStatus.IN_PROGRESS)
        self.assertIsNone(session3.assigned_operator)

        with patch('support.telegram.httpx.post', return_value=_fake_tg_response(True)):
            resp_anon_claim2 = self.client.post(
                self.webhook_url,
                data=json.dumps({
                    'update_id': 37,
                    'callback_query': {
                        'id': 'cb-anon-2',
                        'from': {'id': 888998, 'is_bot': False, 'first_name': 'Петро'},
                        'data': f'claim:{ticket3.id}',
                        'message': {
                            'message_id': 8201,
                            'message_thread_id': 8101,
                            'chat': {'id': -1001999888777},
                        },
                    },
                }),
                content_type='application/json',
                **self.headers,
            )
        self.assertEqual(resp_anon_claim2.status_code, 200)
        self.assertEqual(resp_anon_claim2.json()['status'], 'conflict')

        # Subsequent reply by a linked operator assigns them and refreshes Telegram card
        tg_calls: list[tuple[str, dict]] = []

        def record_linked_reply(url, json=None, timeout=None):
            tg_calls.append((url.rsplit('/', 1)[-1], dict(json or {})))
            return _fake_tg_response(True)

        with patch('support.telegram.httpx.post', side_effect=record_linked_reply):
            resp_linked_after = self.client.post(
                self.webhook_url,
                data=json.dumps({
                    'update_id': 38,
                    'message': {
                        'message_id': 88099,
                        'message_thread_id': 8101,
                        'chat': {'id': -1001999888777},
                        'from': {'id': 777001, 'is_bot': False, 'first_name': 'Ольга'},
                        'text': 'Підключаюсь до вашого звернення.',
                    },
                }),
                content_type='application/json',
                **self.headers,
            )
        self.assertEqual(resp_linked_after.status_code, 200)
        session3.refresh_from_db()
        self.assertEqual(session3.assigned_operator, self.operator)
        self.assertIn('editMessageText', [m for m, _ in tg_calls])
        edited_text = next(p['text'] for m, p in tg_calls if m == 'editMessageText')
        self.assertIn('Ольга Мельник', edited_text)

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_webhook_invalid_json_and_non_text_ignored(self):
        resp_bad_json = self.client.post(
            self.webhook_url,
            data='{not-json',
            content_type='application/json',
            **self.headers,
        )
        self.assertEqual(resp_bad_json.status_code, 400)

        resp_service = self.client.post(
            self.webhook_url,
            data=json.dumps({
                'update_id': 40,
                'message': {
                    'message_id': 88001,
                    'message_thread_id': 123,
                    'chat': {'id': -1001999888777},
                    'from': {'id': 777001, 'is_bot': False},
                    'forum_topic_closed': {},
                },
            }),
            content_type='application/json',
            **self.headers,
        )
        self.assertEqual(resp_service.status_code, 200)
        self.assertEqual(resp_service.json()['ignored'], 'non_text_message')

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_webhook_rate_limit_120_per_minute(self):
        payload = json.dumps({'update_id': 99})
        for _ in range(120):
            resp = self.client.post(
                self.webhook_url,
                data=payload,
                content_type='application/json',
                **self.headers,
            )
            self.assertEqual(resp.status_code, 200)

        resp_limited = self.client.post(
            self.webhook_url,
            data=payload,
            content_type='application/json',
            **self.headers,
        )
        self.assertEqual(resp_limited.status_code, 429)


class TelegramAdminAndCommandTest(TransactionTestCase):
    def setUp(self):
        cache.clear()

    def test_user_admin_includes_telegram_id_in_personal_data(self):
        user_admin = UserAdmin(User, AdminSite())
        personal_fieldset = dict(user_admin.fieldsets)['Персональні дані']
        self.assertIn('telegram_id', personal_fieldset['fields'])

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_setup_telegram_webhook_command_register_check_delete(self):
        out = StringIO()
        with patch('support.management.commands.setup_telegram_webhook.call_telegram_api') as mock_api:
            mock_api.return_value = True
            call_command('setup_telegram_webhook', stdout=out)

        mock_api.assert_called_once_with(
            'setWebhook',
            {
                'url': 'https://doublebite.ua/support/telegram/webhook/',
                'secret_token': 'super-secret-webhook-token',
                'allowed_updates': ['message', 'callback_query'],
                'drop_pending_updates': False,
            },
        )
        self.assertIn('https://doublebite.ua/support/telegram/webhook/', out.getvalue())

        check_out = StringIO()
        with patch('support.management.commands.setup_telegram_webhook.call_telegram_api') as mock_api:
            mock_api.side_effect = [
                {'id': 111, 'username': 'double_bite_support_bot'},
                {'url': 'https://doublebite.ua/support/telegram/webhook/', 'pending_update_count': 0},
            ]
            call_command('setup_telegram_webhook', '--check', stdout=check_out)
        self.assertIn('@double_bite_support_bot', check_out.getvalue())

        del_out = StringIO()
        with patch('support.management.commands.setup_telegram_webhook.call_telegram_api') as mock_api:
            mock_api.return_value = True
            call_command('setup_telegram_webhook', '--delete', stdout=del_out)
        mock_api.assert_called_once_with('deleteWebhook', {'drop_pending_updates': False})

    @override_settings(
        TELEGRAM_BOT_ENABLED=True,
        TELEGRAM_BOT_TOKEN='token',
        TELEGRAM_WEBHOOK_SECRET='secret',
        SITE_PROTOCOL='http',
        SITE_DOMAIN='127.0.0.1:8000',
    )
    def test_setup_telegram_webhook_rejects_non_https_url(self):
        with self.assertRaises(CommandError):
            call_command('setup_telegram_webhook')

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_admin_mark_resolved_closes_telegram_topic(self):
        customer = User.objects.create_user(
            email='adm-tg@example.com',
            password='Password123!',
            role=Role.CUSTOMER,
        )
        session = ChatSession.objects.create(user=customer)
        with patch('support.telegram.httpx.post') as mock_post:
            mock_post.side_effect = [
                _fake_tg_response({'message_thread_id': 951}),
                _fake_tg_response({'message_id': 952}),
                _fake_tg_response(True),
            ]
            ticket = ChatSessionService.escalate_session(
                session, TicketReason.ORDER_ISSUE, 'Закриття з адмінки'
            )

        ticket_admin = SupportTicketAdmin(SupportTicket, AdminSite())
        calls: list[str] = []

        def record_admin_close(url, json=None, timeout=None):
            calls.append(url.rsplit('/', 1)[-1])
            return _fake_tg_response(True)

        with patch('support.telegram.httpx.post', side_effect=record_admin_close):
            ticket_admin.mark_resolved(
                None,
                SupportTicket.objects.filter(pk=ticket.id),
            )

        self.assertIn('closeForumTopic', calls)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatus.RESOLVED)

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_long_card_history_and_html_entities_respect_4096_limit(self):
        customer = User.objects.create_user(
            email='longcard@example.com',
            password='Password123!',
            first_name='<' * 120,
            last_name='>' * 120,
            role=Role.CUSTOMER,
        )
        session = ChatSession.objects.create(user=customer)
        for idx in range(8):
            ChatMessage.objects.create(
                session=session,
                role=MessageRole.USER if idx % 2 == 0 else MessageRole.ASSISTANT,
                content=('<' * 700) + f' msg-{idx}',
            )
        ticket = SupportTicket.objects.create(
            session=session,
            reason=TicketReason.COMPLAINT,
            details='<' * 1000,
        )
        card = tg.build_ticket_card_text(ticket)
        self.assertLessEqual(len(card), tg.TELEGRAM_MESSAGE_LIMIT)
        self.assertEqual(card.count('<b>'), card.count('</b>'))
        self.assertEqual(card.count('<code>'), card.count('</code>'))
        formatted = tg._format_telegram_html_message('<b>Prefix:</b>\n', '<' * 2000)
        self.assertLessEqual(len(formatted), tg.TELEGRAM_MESSAGE_LIMIT)
        self.assertFalse(formatted.endswith('&l'))

    @override_settings(
        TELEGRAM_BOT_ENABLED=True,
        TELEGRAM_BOT_TOKEN='123456:TEST-TOKEN-SECRET',
        TELEGRAM_SUPPORT_CHAT_ID='-1001999888777',
        TELEGRAM_WEBHOOK_SECRET='super-secret-webhook-token',
        SITE_PROTOCOL='http',
        SITE_DOMAIN='127.0.0.1:8000',
    )
    def test_local_dev_domain_omits_telegram_url_button_and_keeps_text_link(self):
        customer = User.objects.create_user(
            email='local-dev-tg@example.com',
            password='Password123!',
            role=Role.CUSTOMER,
        )
        session = ChatSession.objects.create(
            user=customer,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        ticket = SupportTicket.objects.create(
            session=session,
            reason=TicketReason.HUMAN_REQUESTED,
            details='Локальний тест',
        )
        kb = tg.build_ticket_control_keyboard(ticket)
        buttons = [btn for row in kb['inline_keyboard'] for btn in row]
        self.assertFalse(any('url' in btn for btn in buttons))
        self.assertEqual(
            [btn.get('callback_data') for btn in buttons],
            [f'claim:{ticket.id}', f'return_ai:{ticket.id}', f'resolve:{ticket.id}'],
        )
        card = tg.build_ticket_card_text(ticket)
        self.assertIn(f'http://127.0.0.1:8000/support/desk/{session.session_uuid}/', card)

    @override_settings(**TEST_TELEGRAM_SETTINGS)
    def test_unlinked_telegram_topic_reply_preserves_actor_name_on_card(self):
        customer = User.objects.create_user(
            email='unlinked-reply-cust@example.com',
            password='Password123!',
            role=Role.CUSTOMER,
        )
        session = ChatSession.objects.create(user=customer)
        with patch('support.telegram.httpx.post') as mock_post:
            mock_post.side_effect = [
                _fake_tg_response({'message_thread_id': 9101}),
                _fake_tg_response({'message_id': 9102}),
                _fake_tg_response(True),
            ]
            ticket = ChatSessionService.escalate_session(
                session, TicketReason.HUMAN_REQUESTED, 'Питання'
            )
        ticket.refresh_from_db()

        tg_calls: list[tuple[str, dict]] = []

        def record_tg(url, json=None, timeout=None):
            tg_calls.append((url.rsplit('/', 1)[-1], dict(json or {})))
            return _fake_tg_response(True)

        client = Client()
        with patch('support.telegram.httpx.post', side_effect=record_tg):
            resp = client.post(
                reverse('support:telegram_webhook'),
                data=json.dumps({
                    'update_id': 9901,
                    'message': {
                        'message_id': 91005,
                        'message_thread_id': ticket.telegram_thread_id,
                        'chat': {'id': -1001999888777},
                        'from': {'id': 112233, 'is_bot': False, 'first_name': 'Анна'},
                        'text': 'Доброго дня! Підказую деталі.',
                    },
                }),
                content_type='application/json',
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='super-secret-webhook-token',
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('message_id', resp.json())
        edited_text = next(p['text'] for m, p in tg_calls if m == 'editMessageText')
        self.assertIn('В роботі · Анна', edited_text)
