import uuid
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from orders.models import Order, OrderStatus
from support.models import (
    ChatSession,
    ChatSessionStatus,
    SupportTicket,
    TicketReason,
    TicketStatus,
)
from support.services import (
    ChatSessionService,
    check_order_status_for_request,
    validate_order_access,
)

User = get_user_model()


class SessionIsolationAndIdorTest(TestCase):
    def setUp(self):
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
            session_key='guest_session_key_1',
            order_number='DB-2001',
            customer_name='Гість 1',
            customer_phone='+380673333333',
            delivery_address='пр. Перемоги, 5',
            status=OrderStatus.PENDING,
            total_amount=280.00,
            eta_minutes=35,
        )
        self.guest_order_2 = Order.objects.create(
            session_key='guest_session_key_2',
            order_number='DB-2002',
            customer_name='Гість 2',
            customer_phone='+380674444444',
            delivery_address='вул. Басейна, 3',
            status=OrderStatus.PAID,
            total_amount=510.00,
            eta_minutes=30,
        )

    def _create_authenticated_request(self, user):
        request = self.factory.post('/support/chat/stream/')
        request.user = user
        session_middleware = self.client.session
        request.session = session_middleware
        return request

    def _create_guest_request(self, session_key):
        request = self.factory.post('/support/chat/stream/')
        request.user = type('AnonymousUser', (), {'is_authenticated': False})()
        request.session = self.client.session
        request.session.save()
        request.session._session_key = session_key
        return request

    def test_authenticated_users_have_isolated_sessions(self):
        req_a = self._create_authenticated_request(self.user_a)
        session_a = ChatSessionService.get_or_create_active_session(req_a)

        req_b = self._create_authenticated_request(self.user_b)
        session_b = ChatSessionService.get_or_create_active_session(req_b)

        self.assertNotEqual(session_a.session_uuid, session_b.session_uuid)

        # User A cannot retrieve User B's session
        lookup_b_by_a = ChatSessionService.get_session_for_request(req_a, session_b.session_uuid)
        self.assertIsNone(lookup_b_by_a)

        # User B cannot retrieve User A's session
        lookup_a_by_b = ChatSessionService.get_session_for_request(req_b, session_a.session_uuid)
        self.assertIsNone(lookup_a_by_b)

        # Each user can retrieve their own session
        self.assertEqual(ChatSessionService.get_session_for_request(req_a, session_a.session_uuid), session_a)
        self.assertEqual(ChatSessionService.get_session_for_request(req_b, session_b.session_uuid), session_b)

    def test_guest_session_isolation(self):
        req_g1 = self._create_guest_request('guest_alpha')
        session_g1 = ChatSessionService.get_or_create_active_session(req_g1)

        req_g2 = self._create_guest_request('guest_beta')
        session_g2 = ChatSessionService.get_or_create_active_session(req_g2)

        self.assertNotEqual(session_g1.session_uuid, session_g2.session_uuid)

        # Guest 1 cannot access Guest 2's session
        self.assertIsNone(ChatSessionService.get_session_for_request(req_g1, session_g2.session_uuid))

        # Guest 2 cannot access Guest 1's session
        self.assertIsNone(ChatSessionService.get_session_for_request(req_g2, session_g1.session_uuid))

        # Guest cannot access authenticated session
        req_auth = self._create_authenticated_request(self.user_a)
        session_auth = ChatSessionService.get_or_create_active_session(req_auth)
        self.assertIsNone(ChatSessionService.get_session_for_request(req_g1, session_auth.session_uuid))

    def test_invalid_uuid_returns_none(self):
        req = self._create_authenticated_request(self.user_a)
        self.assertIsNone(ChatSessionService.get_session_for_request(req, 'not-a-valid-uuid'))
        self.assertIsNone(ChatSessionService.get_session_for_request(req, uuid.uuid4()))

    def test_guest_session_account_linking(self):
        # Guest starts chat
        req_guest = self._create_guest_request('guest_temp_key')
        guest_session = ChatSessionService.get_or_create_active_session(req_guest)
        self.assertEqual(guest_session.session_key, 'guest_temp_key')
        self.assertIsNone(guest_session.user)

        # Same guest now logs in with session key present
        req_logged_in = self.factory.get('/support/chat/')
        req_logged_in.user = self.user_a
        req_logged_in.session = self.client.session
        req_logged_in.session._session_key = 'guest_temp_key'

        linked_session = ChatSessionService.get_or_create_active_session(req_logged_in)
        self.assertEqual(linked_session.session_uuid, guest_session.session_uuid)
        self.assertEqual(linked_session.user, self.user_a)
        self.assertEqual(linked_session.session_key, '')

    def test_reset_active_session(self):
        req = self._create_authenticated_request(self.user_a)
        old_session = ChatSessionService.get_or_create_active_session(req)

        new_session = ChatSessionService.reset_active_session(req)
        self.assertNotEqual(old_session.session_uuid, new_session.session_uuid)

        old_session.refresh_from_db()
        self.assertEqual(old_session.status, ChatSessionStatus.RESOLVED)
        self.assertEqual(new_session.status, ChatSessionStatus.ACTIVE)

    def test_idor_protection_authenticated_cross_order(self):
        # User A attempts to view Order B -> must be denied
        req_a = self._create_authenticated_request(self.user_a)

        result_unauthorized = check_order_status_for_request(req_a, self.order_b.id)
        self.assertIn('error', result_unauthorized)
        self.assertEqual(result_unauthorized['error'], 'Замовлення не знайдено або доступ заборонено')

        # User A queries their own order -> authorized
        result_authorized = check_order_status_for_request(req_a, self.order_a.id)
        self.assertNotIn('error', result_authorized)
        self.assertEqual(result_authorized['order_number'], 'DB-1001')
        self.assertEqual(result_authorized['status'], OrderStatus.ON_WAY)
        self.assertEqual(result_authorized['eta_minutes'], 25)

        # User A queries using string order number DB-1001
        result_by_number = check_order_status_for_request(req_a, 'DB-1001')
        self.assertNotIn('error', result_by_number)
        self.assertEqual(result_by_number['order_id'], self.order_a.id)

    def test_idor_protection_guest_cross_order(self):
        req_g1 = self._create_guest_request('guest_session_key_1')

        # Guest 1 queries Guest Order 2 -> denied
        res_cross_guest = check_order_status_for_request(req_g1, self.guest_order_2.id)
        self.assertIn('error', res_cross_guest)

        # Guest 1 queries Authenticated Order A -> denied
        res_auth_order = check_order_status_for_request(req_g1, self.order_a.id)
        self.assertIn('error', res_auth_order)

        # Guest 1 queries own order -> authorized
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
            details='Кур\'єр запізнюється на 45 хвилин',
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
        self.assertEqual(session.escalation_reason, 'Кур\'єр запізнюється на 45 хвилин')
