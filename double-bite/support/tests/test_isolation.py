import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from orders.middleware import (
    GUEST_TOKEN_SESSION_KEY,
    PRE_LOGIN_GUEST_TOKEN_SESSION_KEY,
)
from orders.models import Order, OrderStatus

from support.models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    FAQKnowledge,
    MessageRole,
    SupportTicket,
    TicketReason,
    TicketStatus,
)
from support.services import (
    ChatSessionService,
    check_order_status_for_request,
    get_faq_answers,
    validate_order_access,
)

User = get_user_model()


class SessionIsolationAndIdorTest(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

        self.user_a = User.objects.create_user(
            email='user_a@example.com',
            password='password123',
            phone='+380501111111',
        )
        self.user_b = User.objects.create_user(
            email='user_b@example.com',
            password='password123',
            phone='+380502222222',
        )

        self.order_a = Order.objects.create(
            user=self.user_a,
            order_number='DB-1001',
            customer_name='Користувач А',
            customer_phone='+380501111111',
            delivery_address='вул. Хрещатик, 1',
            status=OrderStatus.ON_WAY,
            total_amount=450.00,
            eta_minutes=25,
        )
        self.order_b = Order.objects.create(
            user=self.user_b,
            order_number='DB-1002',
            customer_name='Користувач Б',
            customer_phone='+380502222222',
            delivery_address='вул. Володимирська, 10',
            status=OrderStatus.PREPARING,
            total_amount=620.00,
            eta_minutes=40,
        )

        self.guest_order_1 = Order.objects.create(
            guest_token='guest_session_key_1',
            order_number='DB-2001',
            customer_name='Гість 1',
            customer_phone='+380673333333',
            delivery_address='пр. Перемоги, 5',
            status=OrderStatus.PENDING,
            total_amount=280.00,
            eta_minutes=35,
        )
        self.guest_order_2 = Order.objects.create(
            guest_token='guest_session_key_2',
            order_number='DB-2002',
            customer_name='Гість 2',
            customer_phone='+380674444444',
            delivery_address='вул. Басейна, 3',
            status=OrderStatus.PAID,
            total_amount=510.00,
            eta_minutes=30,
        )

    def _create_authenticated_request(self, user, session_data=None):
        request = self.factory.post('/support/chat/stream/')
        request.user = user
        session = SessionStore()
        session.create()
        if session_data:
            session.update(session_data)
            session.save()
        request.session = session
        return request

    def _create_guest_request(self, guest_token):
        request = self.factory.post('/support/chat/stream/')
        request.user = AnonymousUser()
        session = SessionStore()
        session.create()
        if guest_token:
            session[GUEST_TOKEN_SESSION_KEY] = guest_token
            session.save()
        request.session = session
        return request

    def test_authenticated_users_have_isolated_sessions(self):
        req_a = self._create_authenticated_request(self.user_a)
        session_a = ChatSessionService.get_or_create_active_session(req_a)

        req_b = self._create_authenticated_request(self.user_b)
        session_b = ChatSessionService.get_or_create_active_session(req_b)

        self.assertNotEqual(session_a.session_uuid, session_b.session_uuid)

        lookup_b_by_a = ChatSessionService.get_session_for_request(
            req_a, session_b.session_uuid
        )
        self.assertIsNone(lookup_b_by_a)

        lookup_a_by_b = ChatSessionService.get_session_for_request(
            req_b, session_a.session_uuid
        )
        self.assertIsNone(lookup_a_by_b)

        self.assertEqual(
            ChatSessionService.get_session_for_request(req_a, session_a.session_uuid),
            session_a,
        )
        self.assertEqual(
            ChatSessionService.get_session_for_request(req_b, session_b.session_uuid),
            session_b,
        )

    def test_guest_session_isolation(self):
        req_g1 = self._create_guest_request('guest_alpha')
        session_g1 = ChatSessionService.get_or_create_active_session(req_g1)

        req_g2 = self._create_guest_request('guest_beta')
        session_g2 = ChatSessionService.get_or_create_active_session(req_g2)

        self.assertNotEqual(session_g1.session_uuid, session_g2.session_uuid)

        self.assertIsNone(
            ChatSessionService.get_session_for_request(req_g1, session_g2.session_uuid)
        )
        self.assertIsNone(
            ChatSessionService.get_session_for_request(req_g2, session_g1.session_uuid)
        )

        req_auth = self._create_authenticated_request(self.user_a)
        session_auth = ChatSessionService.get_or_create_active_session(req_auth)
        self.assertIsNone(
            ChatSessionService.get_session_for_request(req_g1, session_auth.session_uuid)
        )

    def test_invalid_uuid_returns_none(self):
        req = self._create_authenticated_request(self.user_a)
        self.assertIsNone(ChatSessionService.get_session_for_request(req, 'not-a-valid-uuid'))
        self.assertIsNone(ChatSessionService.get_session_for_request(req, uuid.uuid4()))

    def test_guest_session_account_linking(self):
        req_guest = self._create_guest_request('guest_temp_key')
        guest_session = ChatSessionService.get_or_create_active_session(req_guest)
        self.assertEqual(guest_session.guest_token, 'guest_temp_key')
        self.assertIsNone(guest_session.user)

        req_logged_in = self._create_authenticated_request(
            self.user_a,
            session_data={GUEST_TOKEN_SESSION_KEY: 'guest_temp_key'},
        )

        linked_session = ChatSessionService.get_or_create_active_session(req_logged_in)
        self.assertEqual(linked_session.session_uuid, guest_session.session_uuid)
        self.assertEqual(linked_session.user, self.user_a)
        self.assertEqual(linked_session.guest_token, '')

    def test_pre_login_guest_token_grants_order_access_and_links_chat_session(self):
        guest_tok = 'pre_login_guest_tok_xyz'
        guest_order = Order.objects.create(
            user=None,
            guest_token=guest_tok,
            order_number='DB-7777',
            customer_name='Гість до входу',
            customer_phone='+380675556677',
            delivery_address='вул. Липинського, 4',
            status=OrderStatus.PREPARING,
            total_amount=310.00,
        )
        guest_chat = ChatSession.objects.create(
            user=None,
            guest_token=guest_tok,
            status=ChatSessionStatus.ACTIVE,
        )
        ChatMessage.objects.create(
            session=guest_chat,
            role=MessageRole.USER,
            content='Моє передлогінне повідомлення',
        )

        req_after_login = self._create_authenticated_request(
            self.user_a,
            session_data={PRE_LOGIN_GUEST_TOKEN_SESSION_KEY: guest_tok},
        )

        self.assertTrue(validate_order_access(guest_order, req_after_login))
        order_res = check_order_status_for_request(req_after_login, 'DB-7777')
        self.assertNotIn('error', order_res)
        self.assertEqual(order_res['order_id'], guest_order.id)

        active_ro = ChatSessionService.get_active_session(req_after_login)
        self.assertIsNotNone(active_ro)
        self.assertEqual(active_ro.session_uuid, guest_chat.session_uuid)
        active_ro.refresh_from_db()
        self.assertIsNone(active_ro.user)

        linked = ChatSessionService.get_or_create_active_session(req_after_login)
        self.assertEqual(linked.session_uuid, guest_chat.session_uuid)
        self.assertEqual(linked.user, self.user_a)
        self.assertEqual(linked.guest_token, '')
        self.assertEqual(linked.messages.count(), 1)

    def test_validate_order_access_blocks_guest_on_user_owned_order_with_matching_token(self):
        user_order_with_token = Order.objects.create(
            user=self.user_a,
            guest_token='shared_guest_token_leak',
            order_number='DB-8888',
            customer_name='Авторизований',
            customer_phone='+380501111111',
            delivery_address='вул. Хрещатик, 1',
            status=OrderStatus.PAID,
            total_amount=400.00,
        )
        req_guest = self._create_guest_request('shared_guest_token_leak')
        self.assertFalse(validate_order_access(user_order_with_token, req_guest))
        res = check_order_status_for_request(req_guest, 'DB-8888')
        self.assertIn('error', res)

    def test_reset_active_session(self):
        req = self._create_authenticated_request(self.user_a)
        old_session = ChatSessionService.get_or_create_active_session(req)

        new_session = ChatSessionService.reset_active_session(req)
        self.assertNotEqual(old_session.session_uuid, new_session.session_uuid)

        old_session.refresh_from_db()
        self.assertEqual(old_session.status, ChatSessionStatus.RESOLVED)
        self.assertEqual(new_session.status, ChatSessionStatus.ACTIVE)

    def test_idor_protection_authenticated_cross_order(self):
        req_a = self._create_authenticated_request(self.user_a)

        result_unauthorized = check_order_status_for_request(req_a, self.order_b.id)
        self.assertIn('error', result_unauthorized)
        self.assertEqual(
            result_unauthorized['error'],
            'Замовлення не знайдено або доступ заборонено',
        )

        result_authorized = check_order_status_for_request(req_a, self.order_a.id)
        self.assertNotIn('error', result_authorized)
        self.assertEqual(result_authorized['order_number'], 'DB-1001')
        self.assertEqual(result_authorized['status'], OrderStatus.ON_WAY)
        self.assertEqual(result_authorized['eta_minutes'], 25)

        result_by_number = check_order_status_for_request(req_a, 'DB-1001')
        self.assertNotIn('error', result_by_number)
        self.assertEqual(result_by_number['order_id'], self.order_a.id)

    def test_idor_protection_guest_cross_order(self):
        req_g1 = self._create_guest_request('guest_session_key_1')

        res_cross_guest = check_order_status_for_request(req_g1, self.guest_order_2.id)
        self.assertIn('error', res_cross_guest)

        res_auth_order = check_order_status_for_request(req_g1, self.order_a.id)
        self.assertIn('error', res_auth_order)

        res_own_guest = check_order_status_for_request(req_g1, self.guest_order_1.id)
        self.assertNotIn('error', res_own_guest)
        self.assertEqual(res_own_guest['order_number'], 'DB-2001')

    def test_check_nonexistent_order(self):
        req = self._create_authenticated_request(self.user_a)
        result = check_order_status_for_request(req, 99999)
        self.assertIn('error', result)

    def test_escalate_session_creates_ticket_and_updates_status(self):
        req = self._create_authenticated_request(self.user_a)
        session = ChatSessionService.get_or_create_active_session(req)

        ticket = ChatSessionService.escalate_session(
            session=session,
            reason=TicketReason.COMPLAINT,
            details="Кур'єр запізнюється на 45 хвилин",
            order=self.order_a,
            customer_phone='+380501111111',
        )

        self.assertIsInstance(ticket, SupportTicket)
        self.assertEqual(ticket.session, session)
        self.assertEqual(ticket.order, self.order_a)
        self.assertEqual(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.reason, TicketReason.COMPLAINT)

        session.refresh_from_db()
        self.assertTrue(session.is_escalated)
        self.assertEqual(session.status, ChatSessionStatus.ESCALATED)
        self.assertEqual(
            session.escalation_reason, "Кур'єр запізнюється на 45 хвилин"
        )


class OrderNumberLookupAndFaqTest(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.user_a = User.objects.create_user(
            email='lookup_a@example.com',
            password='password123',
        )
        self.user_b = User.objects.create_user(
            email='lookup_b@example.com',
            password='password123',
        )

    def test_order_lookup_prioritizes_order_number_when_pk_differs_from_digits(self):
        order_1 = Order.objects.create(
            id=1,
            user=self.user_b,
            order_number='DB-9999',
            customer_name='User B',
            customer_phone='+380501110001',
            delivery_address='вул. А, 1',
            total_amount=250.00,
        )
        order_2 = Order.objects.create(
            id=2,
            user=self.user_a,
            order_number='DB-0001',
            customer_name='User A',
            customer_phone='+380501110002',
            delivery_address='вул. Б, 2',
            total_amount=350.00,
        )
        self.assertEqual(order_1.pk, 1)
        self.assertEqual(order_2.pk, 2)

        req_a = self.factory.post('/support/chat/stream/')
        req_a.user = self.user_a
        req_a.session = {}

        res_plain = check_order_status_for_request(req_a, 'DB-0001')
        self.assertNotIn('error', res_plain)
        self.assertEqual(res_plain['order_id'], 2)
        self.assertEqual(res_plain['order_number'], 'DB-0001')

        res_hash = check_order_status_for_request(req_a, '#DB-0001')
        self.assertNotIn('error', res_hash)
        self.assertEqual(res_hash['order_id'], 2)

        res_nonexistent_num = check_order_status_for_request(req_a, 'DB-0002')
        self.assertIn('error', res_nonexistent_num)

        req_b = self.factory.post('/support/chat/stream/')
        req_b.user = self.user_b
        req_b.session = {}
        res_cross = check_order_status_for_request(req_b, 'DB-0001')
        self.assertIn('error', res_cross)

    def test_get_faq_answers_cyrillic_case_insensitive_and_ranked(self):
        faq_low = FAQKnowledge.objects.create(
            question='Доставка в передмістя',
            answer='Можлива додаткова плата.',
            category='Логістика',
            is_active=True,
        )
        faq_high = FAQKnowledge.objects.create(
            question='Яка вартість доставки по Києву?',
            answer='Вартість доставки становить 70 грн, безкоштовно від 600 грн.',
            category='Доставка',
            is_active=True,
        )
        FAQKnowledge.objects.create(
            question='Вартість доставки стара архівна',
            answer='Архівний запис',
            category='Архів',
            is_active=False,
        )
        FAQKnowledge.objects.create(
            question='Графік роботи ресторану',
            answer='Щодня з 10:00 до 22:00.',
            category='Графік',
            is_active=True,
        )

        results = get_faq_answers('ВАРТІСТЬ ДОСТАВКИ')
        ids = [item['id'] for item in results]
        self.assertGreaterEqual(len(results), 2)
        self.assertEqual(ids[0], faq_high.id)
        self.assertIn(faq_low.id, ids)
        self.assertTrue(all(item['question'] != 'Вартість доставки стара архівна' for item in results))

        upper_single = get_faq_answers('ГРАФІК РОБОТИ')
        self.assertEqual(len(upper_single), 1)
        self.assertEqual(upper_single[0]['question'], 'Графік роботи ресторану')
