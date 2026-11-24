import json
import re
import shutil
import subprocess
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from accounts.models import Role
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import AsyncClient, Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from orders.middleware import (
    GUEST_TOKEN_SESSION_KEY,
    PRE_LOGIN_GUEST_TOKEN_SESSION_KEY,
)

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
from support.services import (
    MAX_SUPPORT_MESSAGE_LENGTH,
    ChatSessionService,
    SupportDeskService,
)

User = get_user_model()
KYIV_TZ = ZoneInfo('Europe/Kyiv')


class ClientLiveChatApiTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client_a = Client()
        self.client_b = Client()
        self.guest_client_1 = Client()
        self.guest_client_2 = Client()
        self.async_client = AsyncClient()

        self.user_a = User.objects.create_user(
            email='live_user_a@example.com',
            password='password123',
            first_name='Анна',
            last_name='Шевченко',
            phone='+380501112233',
            role=Role.CUSTOMER,
        )
        self.user_b = User.objects.create_user(
            email='live_user_b@example.com',
            password='password123',
            first_name='Богдан',
            last_name='Мельник',
            phone='+380502223344',
            role=Role.CUSTOMER,
        )
        self.operator = User.objects.create_user(
            email='live_operator@example.com',
            password='password123',
            first_name='Олена',
            last_name='Коваль',
            role=Role.RESTAURANT_ADMIN,
        )

    # ------------------------------------------------------------------
    # URLs & HTTP methods
    # ------------------------------------------------------------------

    def test_url_names_and_method_restrictions(self):
        self.assertEqual(reverse('support:chat_escalate'), '/support/chat/escalate/')
        self.assertEqual(reverse('support:chat_message'), '/support/chat/message/')
        self.assertEqual(reverse('support:chat_history'), '/support/chat/history/')

        self.assertEqual(self.client_a.get('/support/chat/escalate/').status_code, 405)
        self.assertEqual(self.client_a.get('/support/chat/message/').status_code, 405)
        self.assertEqual(self.client_a.post('/support/chat/history/').status_code, 405)

    def test_csrf_protection_enforced_on_escalate_and_message(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user_a)
        res_esc = csrf_client.post('/support/chat/escalate/')
        self.assertEqual(res_esc.status_code, 403)
        res_msg = csrf_client.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'Без CSRF'}),
            content_type='application/json',
        )
        self.assertEqual(res_msg.status_code, 403)

    # ------------------------------------------------------------------
    # POST /support/chat/escalate/
    # ------------------------------------------------------------------

    def test_escalate_authenticated_one_click_empty_body_and_idempotency(self):
        self.client_a.force_login(self.user_a)
        in_hours = datetime(2026, 7, 15, 14, 30, tzinfo=KYIV_TZ)

        with patch('support.views.timezone.now', return_value=in_hours):
            res = self.client_a.post('/support/chat/escalate/')

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], ChatSessionStatus.ESCALATED)
        self.assertTrue(data['is_escalated'])
        self.assertFalse(data['operator_connected'])
        self.assertTrue(data['is_working_hours'])
        self.assertEqual(data['working_hours_label'], '10:00–22:00')
        self.assertIsInstance(data['ticket_id'], int)
        self.assertGreater(data['last_message_id'], 0)
        self.assertEqual(len(data['messages']), 1)
        self.assertEqual(data['messages'][0]['role'], MessageRole.SYSTEM)
        self.assertEqual(data['messages'][0]['sender'], 'system')
        self.assertIn(f"Звернення #{data['ticket_id']}", data['messages'][0]['content'])
        self.assertNotIn('Зараз неробочий час', data['messages'][0]['content'])

        ticket = SupportTicket.objects.get(pk=data['ticket_id'])
        self.assertEqual(ticket.reason, TicketReason.HUMAN_REQUESTED)
        self.assertEqual(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.customer_phone, '+380501112233')

        # Second click is idempotent: same ticket, no duplicate system messages
        with patch('support.views.timezone.now', return_value=in_hours):
            res2 = self.client_a.post(
                '/support/chat/escalate/',
                data=json.dumps({}),
                content_type='application/json',
            )
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertEqual(data2['ticket_id'], data['ticket_id'])
        self.assertEqual(data2['session_uuid'], data['session_uuid'])
        self.assertEqual(SupportTicket.objects.count(), 1)
        self.assertEqual(len(data2['messages']), 1)

    def test_escalate_custom_fields_and_disallowed_internal_reason(self):
        self.client_a.force_login(self.user_a)
        res = self.client_a.post(
            '/support/chat/escalate/',
            data=json.dumps({
                'reason': TicketReason.ORDER_ISSUE,
                'details': 'Піца приїхала холодною',
                'customer_phone': '+380671234567',
            }),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200)
        ticket = SupportTicket.objects.get(pk=res.json()['ticket_id'])
        self.assertEqual(ticket.reason, TicketReason.ORDER_ISSUE)
        self.assertEqual(ticket.details, 'Піца приїхала холодною')
        self.assertEqual(ticket.customer_phone, '+380671234567')

        # Client cannot forge OPERATOR_TAKEOVER reason
        self.client_b.force_login(self.user_b)
        res_b = self.client_b.post(
            '/support/chat/escalate/',
            data=json.dumps({'reason': TicketReason.OPERATOR_TAKEOVER}),
            content_type='application/json',
        )
        self.assertEqual(res_b.status_code, 200)
        ticket_b = SupportTicket.objects.get(pk=res_b.json()['ticket_id'])
        self.assertEqual(ticket_b.reason, TicketReason.HUMAN_REQUESTED)

    def test_escalate_off_hours_includes_warning_and_respects_custom_settings(self):
        self.client_a.force_login(self.user_a)
        off_hours = datetime(2026, 7, 15, 23, 15, tzinfo=KYIV_TZ)

        with patch('support.views.timezone.now', return_value=off_hours):
            res = self.client_a.post('/support/chat/escalate/')

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data['is_working_hours'])
        self.assertEqual(data['working_hours_label'], '10:00–22:00')
        self.assertIn('Зараз неробочий час (10:00–22:00)', data['off_hours_notice'])
        self.assertIn('Зараз неробочий час (10:00–22:00)', data['messages'][-1]['content'])
        self.assertIn('з 10:00', data['messages'][-1]['content'])

        with override_settings(SUPPORT_WORK_START_HOUR=9, SUPPORT_WORK_END_HOUR=21):
            early_morning = datetime(2026, 7, 15, 8, 59, tzinfo=KYIV_TZ)
            self.client_b.force_login(self.user_b)
            with patch('support.views.timezone.now', return_value=early_morning):
                res_b = self.client_b.post('/support/chat/escalate/')
            self.assertEqual(res_b.status_code, 200)
            data_b = res_b.json()
            self.assertFalse(data_b['is_working_hours'])
            self.assertEqual(data_b['working_hours_label'], '09:00–21:00')
            self.assertIn('з 09:00', data_b['messages'][-1]['content'])

    def test_escalate_validation_errors(self):
        self.client_a.force_login(self.user_a)
        bad_payloads = [
            b'{not-json',
            b'["list-not-dict"]',
            json.dumps({'reason': 123}).encode('utf-8'),
            json.dumps({'details': ['bad']}).encode('utf-8'),
            json.dumps({'customer_phone': 38050}).encode('utf-8'),
            json.dumps({'details': 'bad\x00nul'}).encode('utf-8'),
        ]
        for payload in bad_payloads:
            res = self.client_a.post(
                '/support/chat/escalate/',
                data=payload,
                content_type='application/json',
            )
            self.assertEqual(res.status_code, 400, payload)

    def test_escalate_guest_session_and_history_continuity(self):
        res = self.guest_client_1.post('/support/chat/escalate/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['is_escalated'])
        self.assertEqual(data['status'], ChatSessionStatus.ESCALATED)

        session = ChatSession.objects.get(session_uuid=data['session_uuid'])
        self.assertIsNone(session.user)
        self.assertTrue(bool(session.guest_token))

        hist = self.guest_client_1.get('/support/chat/history/')
        self.assertEqual(hist.status_code, 200)
        hist_data = hist.json()
        self.assertEqual(hist_data['session_uuid'], data['session_uuid'])
        self.assertTrue(hist_data['is_escalated'])
        self.assertEqual(len(hist_data['messages']), 1)

    def test_escalate_rate_limit_ip_and_session(self):
        self.client_a.force_login(self.user_a)
        for _ in range(5):
            res = self.client_a.post('/support/chat/escalate/')
            self.assertEqual(res.status_code, 200)

        blocked = self.client_a.post('/support/chat/escalate/')
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('Retry-After', blocked.headers)
        self.assertIn('error', blocked.json())
        self.assertGreaterEqual(blocked.json()['retry_after'], 1)

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=['127.0.0.1'])
    def test_escalate_rate_limit_enforced_by_session_across_different_ips(self):
        self.client_a.force_login(self.user_a)
        for idx in range(5):
            res = self.client_a.post(
                '/support/chat/escalate/',
                REMOTE_ADDR='127.0.0.1',
                HTTP_X_FORWARDED_FOR=f'198.51.100.{idx + 1}',
            )
            self.assertEqual(res.status_code, 200)

        blocked = self.client_a.post(
            '/support/chat/escalate/',
            REMOTE_ADDR='127.0.0.1',
            HTTP_X_FORWARDED_FOR='198.51.100.99',
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('Retry-After', blocked.headers)

    @override_settings(TRUST_PROXY_HEADERS=False)
    def test_escalate_blocks_xff_spoofing_when_trust_proxy_headers_false(self):
        for idx in range(5):
            guest = Client()
            res = guest.post(
                '/support/chat/escalate/',
                REMOTE_ADDR='127.0.0.1',
                HTTP_X_FORWARDED_FOR=f'198.51.100.{idx + 1}',
            )
            self.assertEqual(res.status_code, 200)

        blocked = Client().post(
            '/support/chat/escalate/',
            REMOTE_ADDR='127.0.0.1',
            HTTP_X_FORWARDED_FOR='198.51.100.250',
        )
        self.assertEqual(blocked.status_code, 429)

    # ------------------------------------------------------------------
    # POST /support/chat/message/
    # ------------------------------------------------------------------

    @patch('support.agent.client.MistralSupportAgent.stream_chat_response')
    def test_chat_message_success_authenticated_and_does_not_call_ai(self, mock_ai):
        self.client_a.force_login(self.user_a)
        esc = self.client_a.post('/support/chat/escalate/')
        self.assertEqual(esc.status_code, 200)
        session_uuid = esc.json()['session_uuid']

        res = self.client_a.post(
            '/support/chat/message/',
            data=json.dumps({'message': '  Підкажіть, де зараз курʼєр?  '}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertIn('is_working_hours', data)
        self.assertEqual(data['message']['role'], MessageRole.USER)
        self.assertEqual(data['message']['sender'], 'user')
        self.assertEqual(data['message']['content'], 'Підкажіть, де зараз курʼєр?')
        self.assertRegex(data['message']['created_at'], r'^\d{2}:\d{2}$')

        msg = ChatMessage.objects.get(pk=data['message']['id'])
        self.assertEqual(str(msg.session.session_uuid), session_uuid)
        self.assertEqual(msg.source, MessageSource.WEB)
        mock_ai.assert_not_called()

    def test_chat_message_guest_and_pre_login_session_linking(self):
        esc = self.guest_client_1.post('/support/chat/escalate/')
        self.assertEqual(esc.status_code, 200)
        session_uuid = esc.json()['session_uuid']

        res_guest = self.guest_client_1.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'Повідомлення від гостя'}),
            content_type='application/json',
        )
        self.assertEqual(res_guest.status_code, 201)
        self.assertEqual(res_guest.json()['message']['content'], 'Повідомлення від гостя')

        # Simulate user logging in with pre-login guest token carrying escalated session
        guest_session = ChatSession.objects.get(session_uuid=session_uuid)
        guest_token = guest_session.guest_token
        self.client_b.force_login(self.user_b)
        sess = self.client_b.session
        sess[PRE_LOGIN_GUEST_TOKEN_SESSION_KEY] = guest_token
        sess.save()

        res_linked = self.client_b.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'Після входу продовжую діалог'}),
            content_type='application/json',
        )
        self.assertEqual(res_linked.status_code, 201)
        guest_session.refresh_from_db()
        self.assertEqual(guest_session.user_id, self.user_b.id)
        self.assertEqual(guest_session.guest_token, '')

    def test_chat_message_validation_and_boundary_lengths(self):
        self.client_a.force_login(self.user_a)
        self.client_a.post('/support/chat/escalate/')

        bad_bodies = [
            b'not-json',
            b'[]',
            json.dumps({}).encode('utf-8'),
            json.dumps({'message': ''}).encode('utf-8'),
            json.dumps({'message': '   \n\t  '}).encode('utf-8'),
            json.dumps({'message': 42}).encode('utf-8'),
            json.dumps({'message': 'null\x00byte'}).encode('utf-8'),
            json.dumps({'message': 'a' * (MAX_SUPPORT_MESSAGE_LENGTH + 1)}).encode('utf-8'),
        ]
        for body in bad_bodies:
            res = self.client_a.post(
                '/support/chat/message/',
                data=body,
                content_type='application/json',
            )
            self.assertEqual(res.status_code, 400, body[:60])

        # Exact max length (2000 chars) succeeds
        max_text = 'я' * MAX_SUPPORT_MESSAGE_LENGTH
        res_ok = self.client_a.post(
            '/support/chat/message/',
            data=json.dumps({'message': max_text}),
            content_type='application/json',
        )
        self.assertEqual(res_ok.status_code, 201)
        self.assertEqual(len(res_ok.json()['message']['content']), MAX_SUPPORT_MESSAGE_LENGTH)

    def test_chat_message_conflicts_409_when_not_escalated_or_no_session(self):
        # Anonymous without session gets 409 and creates NO DB records
        initial_sessions = ChatSession.objects.count()
        res_anon = self.guest_client_1.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'Без сесії'}),
            content_type='application/json',
        )
        self.assertEqual(res_anon.status_code, 409)
        self.assertFalse(res_anon.json()['is_escalated'])
        self.assertEqual(res_anon.json()['status'], ChatSessionStatus.ACTIVE)
        self.assertEqual(ChatSession.objects.count(), initial_sessions)

        # Authenticated ACTIVE session -> 409
        self.client_a.force_login(self.user_a)
        session = ChatSession.objects.create(
            user=self.user_a,
            status=ChatSessionStatus.ACTIVE,
        )
        res_active = self.client_a.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'У звичайній AI-сесії'}),
            content_type='application/json',
        )
        self.assertEqual(res_active.status_code, 409)
        self.assertEqual(res_active.json()['status'], ChatSessionStatus.ACTIVE)
        self.assertEqual(session.messages.count(), 0)

        # Escalate -> return_to_ai -> 409 ACTIVE
        ChatSessionService.escalate_session(session, TicketReason.HUMAN_REQUESTED, 'help')
        SupportDeskService.return_to_ai(session, self.operator)
        res_returned = self.client_a.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'Після повернення AI'}),
            content_type='application/json',
        )
        self.assertEqual(res_returned.status_code, 409)
        self.assertEqual(res_returned.json()['status'], ChatSessionStatus.ACTIVE)

        # Resolve -> 409 RESOLVED
        SupportDeskService.resolve_session(session, self.operator)
        res_resolved = self.client_a.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'Після завершення'}),
            content_type='application/json',
        )
        self.assertEqual(res_resolved.status_code, 409)
        self.assertEqual(res_resolved.json()['status'], ChatSessionStatus.RESOLVED)

    def test_chat_message_rate_limit_20_per_minute(self):
        self.client_a.force_login(self.user_a)
        self.client_a.post('/support/chat/escalate/')

        for i in range(20):
            res = self.client_a.post(
                '/support/chat/message/',
                data=json.dumps({'message': f'Повідомлення #{i}'}),
                content_type='application/json',
            )
            self.assertEqual(res.status_code, 201)

        blocked = self.client_a.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'Повідомлення #21'}),
            content_type='application/json',
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('Retry-After', blocked.headers)
        self.assertGreaterEqual(blocked.json()['retry_after'], 1)

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=['127.0.0.1'])
    def test_chat_message_rate_limit_by_session_across_ips(self):
        self.client_a.force_login(self.user_a)
        self.client_a.post('/support/chat/escalate/')

        for i in range(20):
            res = self.client_a.post(
                '/support/chat/message/',
                data=json.dumps({'message': f'Повідомлення {i}'}),
                content_type='application/json',
                REMOTE_ADDR='127.0.0.1',
                HTTP_X_FORWARDED_FOR=f'203.0.113.{i + 1}',
            )
            self.assertEqual(res.status_code, 201)

        blocked = self.client_a.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'Перевищення ліміту'}),
            content_type='application/json',
            REMOTE_ADDR='127.0.0.1',
            HTTP_X_FORWARDED_FOR='203.0.113.200',
        )
        self.assertEqual(blocked.status_code, 429)

    # ------------------------------------------------------------------
    # GET /support/chat/history/?after_id=<int>
    # ------------------------------------------------------------------

    def test_history_empty_anonymous_contract_and_no_db_writes(self):
        initial_count = ChatSession.objects.count()
        res = self.guest_client_1.get('/support/chat/history/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Cache-Control'], 'no-store')
        data = res.json()
        self.assertIsNone(data['session_uuid'])
        self.assertEqual(data['status'], ChatSessionStatus.ACTIVE)
        self.assertFalse(data['is_escalated'])
        self.assertFalse(data['operator_connected'])
        self.assertIsInstance(data['is_working_hours'], bool)
        self.assertEqual(data['working_hours_label'], '10:00–22:00')
        self.assertEqual(data['last_message_id'], 0)
        self.assertEqual(data['messages'], [])
        self.assertEqual(ChatSession.objects.count(), initial_count)

    def test_history_after_id_incremental_polling_and_lifecycle_transitions(self):
        self.client_a.force_login(self.user_a)

        # 1. Escalate via endpoint
        esc = self.client_a.post('/support/chat/escalate/')
        self.assertEqual(esc.status_code, 200)
        session = ChatSession.objects.get(session_uuid=esc.json()['session_uuid'])
        cursor = esc.json()['last_message_id']
        self.assertGreater(cursor, 0)

        # Polling with current cursor returns empty messages AND keeps last_message_id
        poll0 = self.client_a.get(f'/support/chat/history/?after_id={cursor}')
        self.assertEqual(poll0.status_code, 200)
        p0 = poll0.json()
        self.assertEqual(p0['messages'], [])
        self.assertEqual(p0['last_message_id'], cursor)
        self.assertTrue(p0['is_escalated'])
        self.assertFalse(p0['operator_connected'])

        # Tool message must remain hidden and not advance visible messages
        ChatMessage.objects.create(
            session=session,
            role=MessageRole.TOOL,
            source=MessageSource.SYSTEM,
            content='{"secret": true}',
        )

        # 2. Operator claims session
        SupportDeskService.claim_session(session, self.operator)
        poll1 = self.client_a.get(f'/support/chat/history/?after_id={cursor}')
        self.assertEqual(poll1.status_code, 200)
        p1 = poll1.json()
        self.assertTrue(p1['operator_connected'])
        self.assertEqual(len(p1['messages']), 1)
        self.assertEqual(p1['messages'][0]['role'], MessageRole.SYSTEM)
        self.assertIn('підключився адміністратор', p1['messages'][0]['content'])
        cursor = p1['last_message_id']

        # 3. Operator replies -> client receives operator message WITHOUT operator_name leak
        SupportDeskService.send_operator_message(
            session,
            self.operator,
            'Добрий день! Вже перевіряю ваше замовлення.',
        )
        poll2 = self.client_a.get(f'/support/chat/history/?after_id={cursor}')
        self.assertEqual(poll2.status_code, 200)
        p2 = poll2.json()
        self.assertEqual(len(p2['messages']), 1)
        op_msg = p2['messages'][0]
        self.assertEqual(op_msg['role'], MessageRole.OPERATOR)
        self.assertEqual(op_msg['sender'], 'operator')
        self.assertEqual(op_msg['content'], 'Добрий день! Вже перевіряю ваше замовлення.')
        self.assertNotIn('operator_name', op_msg)
        self.assertNotIn('sender_user', op_msg)
        self.assertNotIn('Олена', json.dumps(p2, ensure_ascii=False))
        cursor = p2['last_message_id']

        # 4. Operator returns to AI
        SupportDeskService.return_to_ai(session, self.operator)
        poll3 = self.client_a.get(f'/support/chat/history/?after_id={cursor}')
        self.assertEqual(poll3.status_code, 200)
        p3 = poll3.json()
        self.assertEqual(p3['status'], ChatSessionStatus.ACTIVE)
        self.assertFalse(p3['is_escalated'])
        self.assertFalse(p3['operator_connected'])
        self.assertEqual(len(p3['messages']), 1)
        self.assertIn('AI-консьєрж', p3['messages'][0]['content'])
        cursor = p3['last_message_id']

        # 5. Operator resolves session -> history still surfaces RESOLVED status + final message
        SupportDeskService.resolve_session(session, self.operator)
        poll4 = self.client_a.get(f'/support/chat/history/?after_id={cursor}')
        self.assertEqual(poll4.status_code, 200)
        p4 = poll4.json()
        self.assertEqual(p4['session_uuid'], str(session.session_uuid))
        self.assertEqual(p4['status'], ChatSessionStatus.RESOLVED)
        self.assertFalse(p4['is_escalated'])
        self.assertFalse(p4['operator_connected'])
        self.assertEqual(len(p4['messages']), 1)
        self.assertIn('Звернення завершено', p4['messages'][0]['content'])

        # 6. Client clicks "Новий діалог" -> fresh ACTIVE session replaces RESOLVED in history
        reset_res = self.client_a.post('/support/chat/reset/')
        self.assertEqual(reset_res.status_code, 200)
        new_uuid = reset_res.json()['session_uuid']
        fresh_hist = self.client_a.get('/support/chat/history/')
        self.assertEqual(fresh_hist.status_code, 200)
        fh = fresh_hist.json()
        self.assertEqual(fh['session_uuid'], new_uuid)
        self.assertEqual(fh['status'], ChatSessionStatus.ACTIVE)
        self.assertFalse(fh['is_escalated'])
        self.assertEqual(fh['messages'], [])
        self.assertEqual(fh['last_message_id'], 0)

    def test_history_operator_connected_true_for_unlinked_telegram_reply(self):
        self.client_a.force_login(self.user_a)
        esc = self.client_a.post('/support/chat/escalate/')
        session = ChatSession.objects.get(session_uuid=esc.json()['session_uuid'])

        SupportDeskService.send_operator_message(
            session,
            None,
            'Відповідь з Telegram без привʼязки користувача',
            source=MessageSource.TELEGRAM,
        )
        res = self.client_a.get('/support/chat/history/')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['operator_connected'])

    def test_history_invalid_after_id_returns_400_and_zero_returns_all(self):
        self.client_a.force_login(self.user_a)
        self.client_a.post('/support/chat/escalate/')

        for bad in ('-1', 'abc', '1.5', '²', str(2**63)):
            res = self.client_a.get(f'/support/chat/history/?after_id={bad}')
            self.assertEqual(res.status_code, 400, bad)

        res_zero = self.client_a.get('/support/chat/history/?after_id=0')
        self.assertEqual(res_zero.status_code, 200)
        self.assertEqual(len(res_zero.json()['messages']), 1)

    def test_history_formats_created_at_in_kyiv_local_timezone(self):
        self.client_a.force_login(self.user_a)
        session = ChatSession.objects.create(
            user=self.user_a,
            status=ChatSessionStatus.ACTIVE,
        )
        msg = ChatMessage.objects.create(
            session=session,
            role=MessageRole.USER,
            content='Перевірка часу',
        )
        # 11:15 UTC in summer = 14:15 Europe/Kyiv (UTC+3)
        summer_utc = datetime(2026, 7, 15, 11, 15, tzinfo=ZoneInfo('UTC'))
        ChatMessage.objects.filter(pk=msg.pk).update(created_at=summer_utc)

        res = self.client_a.get('/support/chat/history/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['messages'][0]['created_at'], '14:15')

    # ------------------------------------------------------------------
    # IDOR isolation
    # ------------------------------------------------------------------

    def test_idor_isolation_between_authenticated_users_and_guests(self):
        # User A escalates and posts private message
        self.client_a.force_login(self.user_a)
        esc_a = self.client_a.post('/support/chat/escalate/').json()
        session_a_uuid = esc_a['session_uuid']
        self.client_a.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'Приватне повідомлення А'}),
            content_type='application/json',
        )

        # User B tries passing session_uuid of User A in query and body -> completely ignored
        self.client_b.force_login(self.user_b)
        hist_b = self.client_b.get(
            f'/support/chat/history/?session_uuid={session_a_uuid}&after_id=0'
        )
        self.assertEqual(hist_b.status_code, 200)
        self.assertIsNone(hist_b.json()['session_uuid'])
        self.assertEqual(hist_b.json()['messages'], [])

        msg_b_attempt = self.client_b.post(
            '/support/chat/message/',
            data=json.dumps({
                'session_uuid': session_a_uuid,
                'message': 'Спроба інʼєкції в чужий чат',
            }),
            content_type='application/json',
        )
        self.assertEqual(msg_b_attempt.status_code, 409)

        # Guest 1 escalates and writes; Guest 2 cannot see or write into Guest 1's session
        esc_g1 = self.guest_client_1.post('/support/chat/escalate/').json()
        g1_uuid = esc_g1['session_uuid']
        self.guest_client_1.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'Секрет гостя 1'}),
            content_type='application/json',
        )

        # Give Guest 2 its own distinct guest token
        g2_sess = self.guest_client_2.session
        g2_sess[GUEST_TOKEN_SESSION_KEY] = 'guest-token-isolated-2'
        g2_sess.save()

        hist_g2 = self.guest_client_2.get(
            f'/support/chat/history/?session_uuid={g1_uuid}&after_id=0'
        )
        self.assertEqual(hist_g2.status_code, 200)
        self.assertIsNone(hist_g2.json()['session_uuid'])
        self.assertEqual(hist_g2.json()['messages'], [])

        msg_g2 = self.guest_client_2.post(
            '/support/chat/message/',
            data=json.dumps({
                'session_uuid': g1_uuid,
                'message': 'Гість 2 лізе до Гостя 1',
            }),
            content_type='application/json',
        )
        self.assertEqual(msg_g2.status_code, 409)

        # Verify User A and Guest 1 sessions have only their own messages
        session_a = ChatSession.objects.get(session_uuid=session_a_uuid)
        contents_a = list(session_a.messages.values_list('content', flat=True))
        self.assertNotIn('Спроба інʼєкції в чужий чат', contents_a)

        session_g1 = ChatSession.objects.get(session_uuid=g1_uuid)
        contents_g1 = list(session_g1.messages.values_list('content', flat=True))
        self.assertNotIn('Гість 2 лізе до Гостя 1', contents_g1)

    # ------------------------------------------------------------------
    # Preservation of 409 on POST /support/chat/stream/
    # ------------------------------------------------------------------

    @patch('support.agent.client.MistralSupportAgent.stream_chat_response')
    async def test_stream_preserves_409_after_manual_escalate_and_proactive_takeover(
        self, mock_stream
    ):
        await self.async_client.aforce_login(self.user_a)

        # Manual escalation via POST /support/chat/escalate/
        esc = await self.async_client.post('/support/chat/escalate/')
        self.assertEqual(esc.status_code, 200)

        stream_res = await self.async_client.post(
            '/support/chat/stream/',
            data=json.dumps({'message': 'Гей, AI!'}),
            content_type='application/json',
        )
        self.assertEqual(stream_res.status_code, 409)
        body = json.loads(stream_res.content.decode('utf-8'))
        self.assertTrue(body['is_escalated'])
        self.assertEqual(body['status'], ChatSessionStatus.ESCALATED)
        mock_stream.assert_not_called()

        # Proactive takeover on User B's ACTIVE session -> /stream/ returns 409, /message/ returns 201
        session_b = await sync_to_async(ChatSession.objects.create, thread_sensitive=True)(
            user=self.user_b,
            status=ChatSessionStatus.ACTIVE,
        )
        await sync_to_async(SupportDeskService.proactive_takeover, thread_sensitive=True)(
            session_b,
            self.operator,
        )
        await self.async_client.aforce_login(self.user_b)

        stream_b = await self.async_client.post(
            '/support/chat/stream/',
            data=json.dumps({'message': 'Моє повідомлення під час перехоплення'}),
            content_type='application/json',
        )
        self.assertEqual(stream_b.status_code, 409)
        body_b = json.loads(stream_b.content.decode('utf-8'))
        self.assertTrue(body_b['is_escalated'])
        mock_stream.assert_not_called()

        retry_b = await self.async_client.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'Моє повідомлення під час перехоплення'}),
            content_type='application/json',
        )
        self.assertEqual(retry_b.status_code, 201)
        self.assertEqual(
            json.loads(retry_b.content.decode('utf-8'))['message']['content'],
            'Моє повідомлення під час перехоплення',
        )

    # ------------------------------------------------------------------
    # Concurrency, cache headers, and pre-login edge cases
    # ------------------------------------------------------------------

    def test_chat_message_pre_login_guest_resolved_race_creates_no_new_session(self):
        # Guest escalates, then logs in carrying PRE_LOGIN_GUEST_TOKEN_SESSION_KEY
        esc = self.guest_client_1.post('/support/chat/escalate/')
        self.assertEqual(esc.status_code, 200)
        guest_session = ChatSession.objects.get(session_uuid=esc.json()['session_uuid'])
        guest_token = guest_session.guest_token

        self.client_b.force_login(self.user_b)
        sess = self.client_b.session
        sess[PRE_LOGIN_GUEST_TOKEN_SESSION_KEY] = guest_token
        sess.save()

        # Simulate operator resolving the guest session right between
        # get_active_session() and attach_guest_session_to_user().
        orig_attach = ChatSessionService.attach_guest_session_to_user

        def resolve_then_attach(request, session):
            SupportDeskService.resolve_session(session, self.operator)
            return orig_attach(request, session)

        initial_session_count = ChatSession.objects.count()
        with patch.object(
            ChatSessionService,
            'attach_guest_session_to_user',
            side_effect=resolve_then_attach,
        ):
            res = self.client_b.post(
                '/support/chat/message/',
                data=json.dumps({'message': 'Гонка перед завершенням'}),
                content_type='application/json',
            )

        self.assertEqual(res.status_code, 409)
        self.assertEqual(res['Cache-Control'], 'no-store')
        self.assertEqual(res.json()['status'], ChatSessionStatus.RESOLVED)
        self.assertFalse(res.json()['is_escalated'])
        # Must NOT have created a bogus empty ACTIVE session hiding the RESOLVED one
        self.assertEqual(ChatSession.objects.count(), initial_session_count)
        hist = self.client_b.get('/support/chat/history/')
        self.assertEqual(hist.status_code, 200)
        self.assertEqual(hist.json()['status'], ChatSessionStatus.RESOLVED)
        self.assertEqual(hist.json()['session_uuid'], str(guest_session.session_uuid))

    def test_escalate_concurrent_resolve_returns_accurate_409_status(self):
        self.client_a.force_login(self.user_a)
        session = ChatSession.objects.create(
            user=self.user_a,
            status=ChatSessionStatus.ACTIVE,
        )
        orig_escalate = ChatSessionService.escalate_session

        def resolve_then_escalate(*args, **kwargs):
            SupportDeskService.resolve_session(session, self.operator)
            return orig_escalate(*args, **kwargs)

        with patch.object(
            ChatSessionService,
            'escalate_session',
            side_effect=resolve_then_escalate,
        ):
            res = self.client_a.post('/support/chat/escalate/')

        self.assertEqual(res.status_code, 409)
        self.assertEqual(res['Cache-Control'], 'no-store')
        data = res.json()
        self.assertEqual(data['status'], ChatSessionStatus.RESOLVED)
        self.assertFalse(data['is_escalated'])

    def test_history_get_latest_session_prefers_newer_pre_login_resolved_session(self):
        older_user_session = ChatSession.objects.create(
            user=self.user_a,
            status=ChatSessionStatus.RESOLVED,
        )
        ChatSession.objects.filter(pk=older_user_session.pk).update(
            updated_at=datetime(2026, 7, 1, 10, 0, tzinfo=KYIV_TZ),
        )

        guest_token = 'pre-login-newer-resolved-token'
        newer_guest_session = ChatSession.objects.create(
            guest_token=guest_token,
            status=ChatSessionStatus.RESOLVED,
        )
        ChatMessage.objects.create(
            session=newer_guest_session,
            role=MessageRole.SYSTEM,
            source=MessageSource.SYSTEM,
            content='Звернення завершено.',
        )
        ChatSession.objects.filter(pk=newer_guest_session.pk).update(
            updated_at=datetime(2026, 7, 15, 18, 0, tzinfo=KYIV_TZ),
        )

        self.client_a.force_login(self.user_a)
        sess = self.client_a.session
        sess[PRE_LOGIN_GUEST_TOKEN_SESSION_KEY] = guest_token
        sess.save()

        res = self.client_a.get('/support/chat/history/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['session_uuid'], str(newer_guest_session.session_uuid))
        self.assertEqual(data['status'], ChatSessionStatus.RESOLVED)

    def test_post_endpoints_set_cache_control_no_store_and_off_hours_notice(self):
        self.client_a.force_login(self.user_a)
        esc = self.client_a.post('/support/chat/escalate/')
        self.assertEqual(esc.status_code, 200)
        self.assertEqual(esc['Cache-Control'], 'no-store')

        msg = self.client_a.post(
            '/support/chat/message/',
            data=json.dumps({'message': 'Перевірка заголовків'}),
            content_type='application/json',
        )
        self.assertEqual(msg.status_code, 201)
        self.assertEqual(msg['Cache-Control'], 'no-store')
        self.assertIn('off_hours_notice', msg.json())
        self.assertIn('Зараз неробочий час', msg.json()['off_hours_notice'])

        reset = self.client_a.post('/support/chat/reset/')
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset['Cache-Control'], 'no-store')


class ChatWidgetFrontendBehaviorTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        factory = RequestFactory()
        req = factory.get('/')
        req.user = AnonymousUser()
        cls.rendered = render_to_string(
            'support/chat_widget.html',
            {'cart_items_count': 0, 'csp_nonce': 'test-nonce'},
            request=req,
        )
        match = re.search(
            r'id="ai-chat-widget"\s+x-data="(\{[\s\S]*?\n {2}\})"',
            cls.rendered,
        )
        if not match:
            raise ValueError('Could not extract x-data expression from chat_widget.html')
        cls.xdata_expr = match.group(1)
        cls.node_bin = shutil.which('node')

    def _run_node(self, script_body: str) -> dict:
        if not self.node_bin:
            self.fail('Node.js executable is required to verify chat_widget.html JS behavior.')
        harness = f"""
        const vm = require('vm');
        const timers = new Map();
        let nextTimerId = 1;
        const listeners = {{}};
        const doc = {{
          hidden: false,
          cookie: 'csrftoken=test-csrf-token',
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
        function makeWidget(fetchImpl) {{
          const sandbox = {{
            document: doc,
            window: {{ htmx: null }},
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
            setTimeout: (fn, ms) => {{
              const id = nextTimerId++;
              timers.set(id, {{ fn, ms }});
              return id;
            }},
            clearTimeout: (id) => {{ timers.delete(id); }},
          }};
          const obj = vm.runInNewContext('(' + {json.dumps(self.xdata_expr)} + ')', sandbox);
          obj.$nextTick = (cb) => cb();
          obj.$refs = {{ msgContainer: {{ scrollTop: 0, scrollHeight: 100 }} }};
          return {{ widget: obj, doc, timers }};
        }}
        (async () => {{
          {script_body}
        }})().catch(err => {{
          console.error(err);
          process.exit(1);
        }});
        """
        proc = subprocess.run(  # noqa: S603
            [self.node_bin, '-'],
            input=harness,
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
        )
        return json.loads(proc.stdout)

    def test_template_contains_required_slice2_ui_elements_and_no_emojis(self):
        required_snippets = [
            '/support/chat/escalate/',
            '/support/chat/message/',
            '/support/chat/history/?after_id=',
            'Оператор',
            'Покликати оператора',
            'Mistral AI',
            'Очікуємо менеджера',
            'Адміністратор',
            'Завершено',
            'Запит передано · можете дописати деталі',
            'Менеджер на звʼязку',
            'Напишіть адміністратору…',
            'Почати новий діалог',
            'Зараз неробочий час',
            'Завершити активне звернення до адміністратора',
            'role="alertdialog"',
            'visibilitychange',
            'document.hidden',
            '3000',
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, self.rendered)

        # Ensure input is no longer disabled when isEscalated
        self.assertNotIn(':disabled="isEscalated || loading"', self.rendered)
        self.assertIn(':disabled="loading || escalating || resetting || isResolved"', self.rendered)

        # All interactive controls meet mobile 44px touch-target requirement
        self.assertNotIn('min-h-[36px]', self.rendered)
        self.assertNotIn('min-w-[36px]', self.rendered)

        # No emoji characters in UI
        self.assertIsNone(re.search(r'[\U0001F300-\U0001FAFF\u2600-\u27BF]', self.rendered))

    def test_js_manual_escalate_polling_visibility_pause_and_reset_confirm(self):
        result = self._run_node("""
          const calls = [];
          const fetchImpl = async (url, opts = {}) => {
            calls.push({ url, method: opts.method || 'GET', body: opts.body || null });
            if (url === '/support/chat/history/') {
              return {
                ok: true,
                status: 200,
                json: async () => ({
                  session_uuid: null,
                  status: 'ACTIVE',
                  is_escalated: false,
                  operator_connected: false,
                  is_working_hours: false,
                  working_hours_label: '10:00–22:00',
                  off_hours_notice: 'Зараз неробочий час (10:00–22:00).',
                  last_message_id: 0,
                  messages: [],
                }),
              };
            }
            if (url === '/support/chat/escalate/') {
              return {
                ok: true,
                status: 200,
                json: async () => ({
                  session_uuid: '11111111-1111-1111-1111-111111111111',
                  status: 'ESCALATED',
                  ticket_id: 42,
                  is_escalated: true,
                  operator_connected: false,
                  is_working_hours: false,
                  working_hours_label: '10:00–22:00',
                  off_hours_notice: 'Зараз неробочий час (10:00–22:00).',
                  last_message_id: 10,
                  messages: [
                    {
                      id: 10,
                      role: 'system',
                      sender: 'system',
                      content: 'Звернення #42 передано адміністратору.',
                      created_at: '23:10',
                    },
                  ],
                }),
              };
            }
            if (url === '/support/chat/message/') {
              return {
                ok: true,
                status: 201,
                json: async () => ({
                  message: {
                    id: 12,
                    role: 'user',
                    sender: 'user',
                    content: 'Замовлення не прийшло',
                    created_at: '23:11',
                  },
                  is_working_hours: false,
                  working_hours_label: '10:00–22:00',
                }),
              };
            }
            if (url === '/support/chat/history/?after_id=10') {
              return {
                ok: true,
                status: 200,
                json: async () => ({
                  session_uuid: '11111111-1111-1111-1111-111111111111',
                  status: 'ESCALATED',
                  is_escalated: true,
                  operator_connected: true,
                  is_working_hours: true,
                  working_hours_label: '10:00–22:00',
                  last_message_id: 12,
                  messages: [
                    {
                      id: 11,
                      role: 'operator',
                      sender: 'operator',
                      content: 'Добрий день, вже дивлюсь!',
                      created_at: '10:01',
                    },
                    {
                      id: 12,
                      role: 'user',
                      sender: 'user',
                      content: 'Замовлення не прийшло',
                      created_at: '23:11',
                    },
                  ],
                }),
              };
            }
            if (url === '/support/chat/reset/') {
              return {
                ok: true,
                status: 200,
                json: async () => ({
                  status: 'ok',
                  session_uuid: '22222222-2222-2222-2222-222222222222',
                  message: 'Сесію успішно скинуто.',
                }),
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };

          const { widget, doc, timers } = makeWidget(fetchImpl);
          await widget.init();
          widget.openChat();
          await widget.escalateToOperator();
          const timerCountAfterEscalate = timers.size;
          const timerDelay = Array.from(timers.values())[0]?.ms || 0;

          // Send message in escalated mode -> goes to /support/chat/message/
          widget.input = 'Замовлення не прийшло';
          await widget.sendMessage();

          // Hide tab -> polling stops
          doc.dispatchVisibility(true);
          const timersWhileHidden = timers.size;

          // Show tab -> immediate catch-up poll runs & deduplicates id=11, appends id=12
          doc.dispatchVisibility(false);
          await new Promise(r => setImmediate(r));
          const idsAfterPoll = widget.messages.map(m => m.id);
          const operatorConnected = widget.operatorConnected;

          // Request reset while escalated -> opens confirmation instead of calling /reset/
          widget.requestResetChat();
          const confirmOpened = widget.confirmResetOpen;
          const resetCalledBeforeConfirm = calls.some(c => c.url === '/support/chat/reset/');

          // Confirm reset -> calls /reset/ and stops polling
          await widget.resetChat();
          const timersAfterReset = timers.size;

          process.stdout.write(JSON.stringify({
            timerCountAfterEscalate,
            timerDelay,
            timersWhileHidden,
            idsAfterPoll,
            operatorConnected,
            confirmOpened,
            resetCalledBeforeConfirm,
            timersAfterReset,
            finalStatus: widget.status,
            finalEscalated: widget.isEscalated,
            urls: calls.map(c => c.url),
          }));
        """)

        self.assertEqual(result['timerCountAfterEscalate'], 1)
        self.assertEqual(result['timerDelay'], 3000)
        self.assertEqual(result['timersWhileHidden'], 0)
        self.assertEqual(result['idsAfterPoll'], [10, 11, 12])
        self.assertTrue(result['operatorConnected'])
        self.assertTrue(result['confirmOpened'])
        self.assertFalse(result['resetCalledBeforeConfirm'])
        self.assertEqual(result['timersAfterReset'], 0)
        self.assertEqual(result['finalStatus'], 'ACTIVE')
        self.assertFalse(result['finalEscalated'])
        self.assertNotIn('/support/chat/stream/', result['urls'])
        self.assertIn('/support/chat/message/', result['urls'])
        self.assertIn('/support/chat/history/?after_id=10', result['urls'])

    def test_js_proactive_takeover_409_retries_once_and_ai_error_shows_fallback(self):
        result = self._run_node("""
          const calls = [];
          const fetchImpl = async (url, opts = {}) => {
            calls.push({ url, method: opts.method || 'GET', body: opts.body || null });
            if (url === '/support/chat/stream/') {
              const streamCallNum = calls.filter(c => c.url === '/support/chat/stream/').length;
              if (streamCallNum === 1) {
                return {
                  ok: false,
                  status: 500,
                  json: async () => ({ error: 'Mistral тимчасово недоступний' }),
                };
              }
              return {
                ok: false,
                status: 409,
                json: async () => ({
                  error: 'Запит передано оператору',
                  is_escalated: true,
                  status: 'ESCALATED',
                }),
              };
            }
            if (url === '/support/chat/message/') {
              return {
                ok: true,
                status: 201,
                json: async () => ({
                  message: { id: 25, role: 'user', sender: 'user', content: 'Чи є суші?', created_at: '15:00' },
                  is_working_hours: true,
                  working_hours_label: '10:00–22:00',
                }),
              };
            }
            if (url === '/support/chat/history/') {
              return {
                ok: true,
                status: 200,
                json: async () => ({
                  session_uuid: '33333333-3333-3333-3333-333333333333',
                  status: 'ESCALATED',
                  is_escalated: true,
                  operator_connected: true,
                  is_working_hours: true,
                  working_hours_label: '10:00–22:00',
                  last_message_id: 25,
                  messages: [
                    {
                      id: 24,
                      role: 'system',
                      sender: 'system',
                      content: 'До діалогу підключився адміністратор ресторану.',
                      created_at: '15:00',
                    },
                    {
                      id: 25,
                      role: 'user',
                      sender: 'user',
                      content: 'Чи є суші?',
                      created_at: '15:00',
                    },
                  ],
                }),
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };

          const { widget, timers } = makeWidget(fetchImpl);
          widget.open = true;

          // 1. AI 500 error shows fallback button flag
          widget.input = 'Привіт';
          await widget.sendMessage();
          const fallbackFlag = widget.messages[widget.messages.length - 1].showOperatorFallback;

          // 2. Proactive takeover 409 from /stream/ -> retries ONCE to /message/, syncs history, starts polling
          widget.input = 'Чи є суші?';
          await widget.sendMessage();

          const messageCalls = calls.filter(c => c.url === '/support/chat/message/');
          process.stdout.write(JSON.stringify({
            fallbackFlag,
            messageCallCount: messageCalls.length,
            retriedBody: messageCalls[0]?.body || null,
            isEscalated: widget.isEscalated,
            operatorConnected: widget.operatorConnected,
            messageIds: widget.messages.map(m => m.id),
            pollingActive: timers.size > 0,
            loadingAfter: widget.loading,
          }));
        """)

        self.assertTrue(result['fallbackFlag'])
        self.assertEqual(result['messageCallCount'], 1)
        self.assertEqual(json.loads(result['retriedBody']), {'message': 'Чи є суші?'})
        self.assertTrue(result['isEscalated'])
        self.assertTrue(result['operatorConnected'])
        self.assertEqual(result['messageIds'], [24, 25])
        self.assertTrue(result['pollingActive'])
        self.assertFalse(result['loadingAfter'])

    def test_js_aborted_poll_does_not_rearm_timer_and_reset_recovers_on_error(self):
        result = self._run_node("""
          let abortReject = null;
          let resetResolve = null;
          let resetCalls = 0;
          const fetchImpl = (url, opts = {}) => {
            if (url.startsWith('/support/chat/history/?after_id=')) {
              return new Promise((_res, rej) => {
                abortReject = () => {
                  const err = new Error('Aborted');
                  err.name = 'AbortError';
                  rej(err);
                };
                if (opts.signal) {
                  opts.signal.addEventListener('abort', abortReject, { once: true });
                }
              });
            }
            if (url === '/support/chat/reset/') {
              resetCalls += 1;
              return new Promise(res => {
                resetResolve = () => res({
                  ok: false,
                  status: 429,
                  json: async () => ({ error: 'Забагато запитів. Спробуйте пізніше.' }),
                });
              });
            }
            throw new Error('Unexpected fetch: ' + url);
          };

          const { widget, timers } = makeWidget(fetchImpl);
          widget.open = true;
          widget.isEscalated = true;
          widget.status = 'ESCALATED';
          widget.sessionUuid = '44444444-4444-4444-4444-444444444444';

          // Start an in-flight poll
          const pollPromise = widget.pollHistory();
          // Start resetChat (which calls stopPolling -> aborts in-flight poll)
          const resetPromise1 = widget.resetChat();
          // Second concurrent resetChat click must be ignored by resetting guard
          const resetPromise2 = widget.resetChat();

          await pollPromise;
          // While reset is still in-flight, aborted poll MUST NOT have re-armed a timer!
          const timersDuringReset = timers.size;

          // Finish reset with 429 error -> error message added AND polling restarts!
          resetResolve();
          await Promise.all([resetPromise1, resetPromise2]);

          process.stdout.write(JSON.stringify({
            timersDuringReset,
            resetCalls,
            resettingAfter: widget.resetting,
            timersAfterFailedReset: timers.size,
            lastError: widget.messages[widget.messages.length - 1]?.error || '',
          }));
        """)

        self.assertEqual(result['timersDuringReset'], 0)
        self.assertEqual(result['resetCalls'], 1)
        self.assertFalse(result['resettingAfter'])
        self.assertEqual(result['timersAfterFailedReset'], 1)
        self.assertIn('Забагато запитів', result['lastError'])

    def test_js_proactive_takeover_409_starts_polling_even_if_load_history_fails(self):
        result = self._run_node("""
          const fetchImpl = async (url) => {
            if (url === '/support/chat/stream/') {
              return {
                ok: false,
                status: 409,
                json: async () => ({
                  error: 'Запит передано оператору',
                  is_escalated: true,
                  status: 'ESCALATED',
                }),
              };
            }
            if (url === '/support/chat/message/') {
              return {
                ok: true,
                status: 201,
                json: async () => ({
                  message: { id: 30, role: 'user', sender: 'user', content: 'Тест', created_at: '16:10' },
                  is_working_hours: true,
                  working_hours_label: '10:00–22:00',
                }),
              };
            }
            if (url === '/support/chat/history/') {
              throw new Error('Transient network failure');
            }
            throw new Error('Unexpected fetch: ' + url);
          };

          const { widget, timers } = makeWidget(fetchImpl);
          widget.open = true;
          widget.input = 'Тест';
          await widget.sendMessage();

          process.stdout.write(JSON.stringify({
            isEscalated: widget.isEscalated,
            pollingActive: timers.size === 1,
            savedMsgId: widget.messages[0]?.id || null,
            savedMsgTime: widget.messages[0]?.created_at || '',
          }));
        """)

        self.assertTrue(result['isEscalated'])
        self.assertTrue(result['pollingActive'])
        self.assertEqual(result['savedMsgId'], 30)
        self.assertEqual(result['savedMsgTime'], '16:10')

    def test_js_stream_escalation_preserves_cart_cta_and_open_chat_syncs_after_ai_turn(self):
        result = self._run_node("""
          const encoder = new TextEncoder();
          let historyCalls = 0;
          const fetchImpl = async (url) => {
            if (url === '/support/chat/stream/') {
              const chunks = [
                'data: ' + JSON.stringify({ cart_mutation: true, cart_items_count: 2 }) + '\\n\\n',
                'data: ' + JSON.stringify({ escalated: true, ticket_id: 7 }) + '\\n\\n',
                'data: ' + JSON.stringify({ token: 'Додав у кошик і покликав адміністратора.' }) + '\\n\\n',
                'data: ' + JSON.stringify({ done: true }) + '\\n\\n',
              ];
              let idx = 0;
              return {
                ok: true,
                status: 200,
                body: {
                  getReader: () => ({
                    read: async () => {
                      if (idx >= chunks.length) return { done: true, value: undefined };
                      return { done: false, value: encoder.encode(chunks[idx++]) };
                    },
                  }),
                },
              };
            }
            if (url === '/support/chat/history/') {
              historyCalls += 1;
              return {
                ok: true,
                status: 200,
                json: async () => ({
                  session_uuid: '55555555-5555-5555-5555-555555555555',
                  status: 'ESCALATED',
                  is_escalated: true,
                  operator_connected: false,
                  is_working_hours: true,
                  working_hours_label: '10:00–22:00',
                  last_message_id: 52,
                  messages: [
                    {
                      id: 50,
                      role: 'user',
                      sender: 'user',
                      content: 'Додай піцу і поклич менеджера',
                      created_at: '17:00',
                    },
                    {
                      id: 51,
                      role: 'system',
                      sender: 'system',
                      content: 'Звернення #7 передано адміністратору.',
                      created_at: '17:00',
                    },
                    {
                      id: 52,
                      role: 'assistant',
                      sender: 'ai',
                      content: 'Додав у кошик і покликав адміністратора.',
                      created_at: '17:00',
                    },
                  ],
                }),
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };

          const { widget } = makeWidget(fetchImpl);
          widget.open = true;
          widget.input = 'Додай піцу і поклич менеджера';
          await widget.sendMessage();

          const assistantMsg = widget.messages.find(m => m.role === 'assistant');

          // Also verify openChat() syncs after an AI message even if sessionUuid is null
          const { widget: widget2 } = makeWidget(fetchImpl);
          widget2.sessionUuid = null;
          widget2.messages = [{ _key: 'tmp-1', id: null, role: 'user', sender: 'user', content: 'Привіт' }];
          const beforeOpenCalls = historyCalls;
          widget2.openChat();
          await new Promise(r => setImmediate(r));
          await new Promise(r => setImmediate(r));

          process.stdout.write(JSON.stringify({
            preservedCartCta: Boolean(assistantMsg && assistantMsg.cartUpdated),
            msgIds: widget.messages.map(m => m.id),
            openChatTriggeredSync: historyCalls > beforeOpenCalls,
            widget2EscalatedAfterReopen: widget2.isEscalated,
          }));
        """)

        self.assertTrue(result['preservedCartCta'])
        self.assertEqual(result['msgIds'], [50, 51, 52])
        self.assertTrue(result['openChatTriggeredSync'])
        self.assertTrue(result['widget2EscalatedAfterReopen'])

    def test_js_multi_tab_session_reset_and_return_to_ai_during_polling(self):
        result = self._run_node("""
          let pollMode = 'return_to_ai';
          const fetchImpl = async (url) => {
            if (url.startsWith('/support/chat/history/?after_id=')) {
              if (pollMode === 'return_to_ai') {
                return {
                  ok: true,
                  status: 200,
                  json: async () => ({
                    session_uuid: '66666666-6666-6666-6666-666666666666',
                    status: 'ACTIVE',
                    is_escalated: false,
                    operator_connected: false,
                    is_working_hours: true,
                    working_hours_label: '10:00–22:00',
                    last_message_id: 62,
                    messages: [
                      {
                        id: 62,
                        role: 'system',
                        sender: 'system',
                        content: 'Адміністратор завершив діалог.',
                        created_at: '18:00',
                      },
                    ],
                  }),
                };
              }
              return {
                ok: true,
                status: 200,
                json: async () => ({
                  session_uuid: '77777777-7777-7777-7777-777777777777',
                  status: 'ACTIVE',
                  is_escalated: false,
                  operator_connected: false,
                  is_working_hours: true,
                  working_hours_label: '10:00–22:00',
                  last_message_id: 0,
                  messages: [],
                }),
              };
            }
            if (url === '/support/chat/history/') {
              return {
                ok: true,
                status: 200,
                json: async () => ({
                  session_uuid: '77777777-7777-7777-7777-777777777777',
                  status: 'ACTIVE',
                  is_escalated: false,
                  operator_connected: false,
                  is_working_hours: true,
                  working_hours_label: '10:00–22:00',
                  last_message_id: 0,
                  messages: [],
                }),
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };

          // Scenario 1: Operator clicks "Повернути AI" during polling
          const { widget: w1, timers: t1 } = makeWidget(fetchImpl);
          w1.open = true;
          w1.sessionUuid = '66666666-6666-6666-6666-666666666666';
          w1.status = 'ESCALATED';
          w1.isEscalated = true;
          w1.operatorConnected = true;
          w1.lastMessageId = 61;
          await w1.pollHistory();

          // Scenario 2: Tab 2 resets session while Tab 1 is polling
          pollMode = 'tab2_reset';
          const { widget: w2, timers: t2 } = makeWidget(fetchImpl);
          w2.open = true;
          w2.sessionUuid = '66666666-6666-6666-6666-666666666666';
          w2.status = 'ESCALATED';
          w2.isEscalated = true;
          w2.lastMessageId = 61;
          w2.messages = [{ _key: 'm-61', id: 61, role: 'user', sender: 'user', content: 'Стара сесія' }];
          await w2.pollHistory();

          process.stdout.write(JSON.stringify({
            w1Status: w1.status,
            w1Escalated: w1.isEscalated,
            w1Operator: w1.operatorConnected,
            w1Timers: t1.size,
            w1LastMsg: w1.messages[w1.messages.length - 1]?.content || '',
            w2SessionUuid: w2.sessionUuid,
            w2Status: w2.status,
            w2Escalated: w2.isEscalated,
            w2Timers: t2.size,
            w2HasOldMsg: w2.messages.some(m => m.content === 'Стара сесія'),
          }));
        """)

        self.assertEqual(result['w1Status'], 'ACTIVE')
        self.assertFalse(result['w1Escalated'])
        self.assertFalse(result['w1Operator'])
        self.assertEqual(result['w1Timers'], 0)
        self.assertIn('Адміністратор завершив діалог', result['w1LastMsg'])
        self.assertEqual(result['w2SessionUuid'], '77777777-7777-7777-7777-777777777777')
        self.assertEqual(result['w2Status'], 'ACTIVE')
        self.assertFalse(result['w2Escalated'])
        self.assertEqual(result['w2Timers'], 0)
        self.assertFalse(result['w2HasOldMsg'])

    def test_js_proxy_reactive_user_message_updates_and_rollback_by_key(self):
        result = self._run_node("""
          let callCount = 0;
          const fetchImpl = async (url) => {
            if (url === '/support/chat/message/') {
              callCount += 1;
              if (callCount === 1) {
                return {
                  ok: true,
                  status: 201,
                  json: async () => ({
                    message: {
                      id: 81,
                      role: 'user',
                      sender: 'user',
                      content: 'Реактивне повідомлення',
                      created_at: '19:15',
                    },
                    is_working_hours: true,
                    working_hours_label: '10:00–22:00',
                  }),
                };
              }
              return {
                ok: false,
                status: 429,
                json: async () => ({ error: 'Ліміт перевищено' }),
              };
            }
            throw new Error('Unexpected fetch: ' + url);
          };

          const { widget } = makeWidget(fetchImpl);
          const reactiveWrites = [];
          // Simulate Alpine 3 Proxy array: reading any element returns a Proxy
          // (so m !== rawObj and direct mutations on rawObj are invisible!)
          const rawArr = [];
          const wrapItem = (item) => new Proxy(item, {
            set(target, prop, value) {
              reactiveWrites.push({ key: target._key, prop: String(prop), value });
              target[prop] = value;
              return true;
            },
          });
          widget.messages = new Proxy(rawArr, {
            get(target, prop, receiver) {
              const val = Reflect.get(target, prop, receiver);
              if (typeof prop === 'string' && /^\\d+$/.test(prop) && val && typeof val === 'object') {
                return wrapItem(val);
              }
              if (prop === 'findIndex') {
                return (fn) => target.findIndex((item, i) => fn(wrapItem(item), i));
              }
              if (prop === 'indexOf') {
                // In real Proxy arrays, indexOf(rawObj) fails if proxies don't match
                return (needle) => target.findIndex((item) => wrapItem(item) === needle);
              }
              return typeof val === 'function' ? val.bind(target) : val;
            },
          });

          widget.open = true;
          widget.isEscalated = true;
          widget.status = 'ESCALATED';

          // 1. Successful send MUST write id/content/created_at through Proxy!
          widget.input = 'Реактивне повідомлення';
          await widget.sendMessage();

          // 2. Failed send (429) MUST remove optimistic message via _key (not indexOf)!
          widget.input = 'Невдале повідомлення';
          await widget.sendMessage();

          process.stdout.write(JSON.stringify({
            reactiveCreatedAtWrite: reactiveWrites.some(w => w.prop === 'created_at' && w.value === '19:15'),
            reactiveIdWrite: reactiveWrites.some(w => w.prop === 'id' && w.value === 81),
            ghostRemoved: !rawArr.some(m => m.content === 'Невдале повідомлення'),
            restoredInput: widget.input,
          }));
        """)

        self.assertTrue(result['reactiveCreatedAtWrite'])
        self.assertTrue(result['reactiveIdWrite'])
        self.assertTrue(result['ghostRemoved'])
        self.assertEqual(result['restoredInput'], 'Невдале повідомлення')
