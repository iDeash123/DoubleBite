import json
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from accounts.models import Role
from django.contrib.admin.sites import site as admin_site
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import Client, RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from menu.models import Category, Dish
from orders.models import Cart, CartItem, Order, OrderItem, OrderStatus

from support.admin import ChatSessionAdmin, SupportTicketAdmin
from support.context_processors import support_context
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
from support.services import MAX_SUPPORT_MESSAGE_LENGTH

User = get_user_model()


class SupportDeskRBACAndSecurityTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.customer = User.objects.create_user(
            email='desk_customer@example.com',
            password='password123',
            role=Role.CUSTOMER,
        )
        self.courier = User.objects.create_user(
            email='desk_courier@example.com',
            password='password123',
            role=Role.COURIER,
        )
        self.operator = User.objects.create_user(
            email='desk_operator@example.com',
            password='password123',
            first_name='Олена',
            last_name='Коваль',
            role=Role.RESTAURANT_ADMIN,
        )
        self.inactive_operator = User.objects.create_user(
            email='desk_inactive_op@example.com',
            password='password123',
            role=Role.RESTAURANT_ADMIN,
            is_active=False,
        )
        self.superuser = User.objects.create_superuser(
            email='desk_super@example.com',
            password='password123',
            role=Role.CUSTOMER,
        )
        self.session = ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        self.past_session = ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.RESOLVED,
        )

    def _desk_endpoints(self):
        uid = self.session.session_uuid
        past_uid = self.past_session.session_uuid
        return [
            ('GET', reverse('support:desk_index')),
            ('GET', reverse('support:desk_session', args=[uid])),
            ('GET', reverse('support:desk_queue_partial')),
            ('GET', reverse('support:desk_thread_partial', args=[uid])),
            ('GET', reverse('support:desk_past_history_partial', args=[past_uid]) + f'?session_uuid={uid}'),
            ('GET', reverse('support:desk_messages_api', args=[uid])),
            ('POST', reverse('support:desk_claim', args=[uid])),
            ('POST', reverse('support:desk_takeover', args=[uid])),
            ('POST', reverse('support:desk_reply', args=[uid])),
            ('POST', reverse('support:desk_return_ai', args=[uid])),
            ('POST', reverse('support:desk_resolve', args=[uid])),
        ]

    def test_anonymous_redirects_to_login_for_all_desk_routes(self):
        for method, url in self._desk_endpoints():
            res = self.client.get(url) if method == 'GET' else self.client.post(url)
            self.assertEqual(res.status_code, 302, f'{method} {url}')
            self.assertIn(reverse('accounts:login'), res['Location'])

    def test_customer_and_courier_and_inactive_operator_get_403_for_all_desk_routes(self):
        for forbidden_user in (self.customer, self.courier):
            self.client.force_login(forbidden_user)
            for method, url in self._desk_endpoints():
                res = (
                    self.client.get(url)
                    if method == 'GET'
                    else self.client.post(
                        url,
                        data=json.dumps({'message': 'test'}),
                        content_type='application/json',
                    )
                )
                self.assertEqual(res.status_code, 403, f'{forbidden_user} {method} {url}')

        # Inactive user cannot stay logged in normally, force_login tests decorator directly
        self.client.force_login(self.inactive_operator)
        res = self.client.get(reverse('support:desk_index'))
        self.assertIn(res.status_code, (302, 403))

    def test_restaurant_admin_and_superuser_allowed(self):
        for allowed_user in (self.operator, self.superuser):
            self.client.force_login(allowed_user)
            res = self.client.get(reverse('support:desk_index'))
            self.assertEqual(res.status_code, 200)
            res_session = self.client.get(
                reverse('support:desk_session', args=[self.session.session_uuid])
            )
            self.assertEqual(res_session.status_code, 200)

    def test_http_method_restrictions_and_csrf(self):
        uid = self.session.session_uuid
        self.client.force_login(self.operator)
        self.assertEqual(self.client.post(reverse('support:desk_index')).status_code, 405)
        self.assertEqual(self.client.post(reverse('support:desk_queue_partial')).status_code, 405)
        self.assertEqual(
            self.client.post(reverse('support:desk_thread_partial', args=[uid])).status_code,
            405,
        )
        self.assertEqual(
            self.client.post(reverse('support:desk_messages_api', args=[uid])).status_code,
            405,
        )
        for name in (
            'support:desk_claim',
            'support:desk_takeover',
            'support:desk_reply',
            'support:desk_return_ai',
            'support:desk_resolve',
        ):
            self.assertEqual(
                self.client.get(reverse(name, args=[uid])).status_code,
                405,
                name,
            )

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.operator)
        res = csrf_client.post(
            reverse('support:desk_claim', args=[uid]),
            data='{}',
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 403)


class SupportDeskQueueSearchAndContextTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.operator = User.objects.create_user(
            email='op1@example.com',
            password='password123',
            first_name='Олена',
            last_name='Коваль',
            role=Role.RESTAURANT_ADMIN,
        )
        self.colleague = User.objects.create_user(
            email='op2@example.com',
            password='password123',
            first_name='Максим',
            last_name='Бондар',
            role=Role.RESTAURANT_ADMIN,
        )
        self.customer_1 = User.objects.create_user(
            email='maria@example.com',
            password='password123',
            first_name='Марія',
            last_name='Іваненко',
            phone='+380501112233',
            role=Role.CUSTOMER,
        )
        self.customer_2 = User.objects.create_user(
            email='taras@example.com',
            password='password123',
            first_name='Тарас',
            last_name='Петренко',
            phone='+380679998877',
            role=Role.CUSTOMER,
        )

        self.cat = Category.objects.create(name='Бургери', slug='burgers', is_active=True)
        self.dish = Dish.objects.create(
            category=self.cat,
            title='Трюфельний бургер',
            slug='truffle-burger',
            price=Decimal('320.00'),
            weight_grams=280,
            is_available=True,
        )
        self.client.force_login(self.operator)

    def test_queue_tabs_ordering_counts_and_unread(self):
        now = timezone.now()

        # Older waiting session (created earlier) vs newer waiting session
        s_wait_old = ChatSession.objects.create(
            user=self.customer_1,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
            last_message_at=now - timedelta(minutes=15),
        )
        t_wait_old = SupportTicket.objects.create(
            session=s_wait_old,
            reason=TicketReason.COMPLAINT,
            details='Холодна піца',
            status=TicketStatus.OPEN,
        )
        SupportTicket.objects.filter(pk=t_wait_old.pk).update(
            created_at=now - timedelta(minutes=25)
        )
        ChatMessage.objects.create(
            session=s_wait_old,
            role=MessageRole.USER,
            content='Очікую вже довго!',
        )

        s_wait_new = ChatSession.objects.create(
            guest_token='guesttoken123456',
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
            last_message_at=now - timedelta(minutes=2),
        )
        t_wait_new = SupportTicket.objects.create(
            session=s_wait_new,
            reason=TicketReason.HUMAN_REQUESTED,
            details='Потрібна допомога',
            status=TicketStatus.OPEN,
        )
        SupportTicket.objects.filter(pk=t_wait_new.pk).update(
            created_at=now - timedelta(minutes=3)
        )
        ChatMessage.objects.create(
            session=s_wait_new,
            role=MessageRole.USER,
            content='Нове питання від гостя',
        )

        # In progress session with unread customer messages
        s_prog = ChatSession.objects.create(
            user=self.customer_2,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
            assigned_operator=self.colleague,
            assigned_at=now - timedelta(minutes=10),
            last_message_at=now - timedelta(minutes=1),
        )
        SupportTicket.objects.create(
            session=s_prog,
            reason=TicketReason.ORDER_ISSUE,
            status=TicketStatus.IN_PROGRESS,
            assigned_operator=self.colleague,
        )
        m_read = ChatMessage.objects.create(
            session=s_prog,
            role=MessageRole.USER,
            content='Перше повідомлення',
        )
        ChatSession.objects.filter(pk=s_prog.pk).update(operator_last_read_id=m_read.id)
        ChatMessage.objects.create(
            session=s_prog,
            role=MessageRole.USER,
            content='Непрочитане повідомлення 1',
        )
        ChatMessage.objects.create(
            session=s_prog,
            role=MessageRole.USER,
            content='Непрочитане повідомлення 2',
        )
        # AI / SYSTEM messages must NOT count as unread
        ChatMessage.objects.create(
            session=s_prog,
            role=MessageRole.SYSTEM,
            source=MessageSource.SYSTEM,
            content='Системне повідомлення',
        )

        # Active AI session
        s_ai = ChatSession.objects.create(
            guest_token='activeai1234',
            status=ChatSessionStatus.ACTIVE,
            last_message_at=now - timedelta(minutes=4),
        )
        ChatMessage.objects.create(
            session=s_ai,
            role=MessageRole.USER,
            content='Що порадите на вечерю?',
        )

        # Resolved session
        s_res = ChatSession.objects.create(
            guest_token='resolved9999',
            status=ChatSessionStatus.RESOLVED,
            last_message_at=now - timedelta(hours=2),
        )
        ChatMessage.objects.create(
            session=s_res,
            role=MessageRole.USER,
            content='Дякую, все вирішено',
        )

        # 1) Default desk_index opens oldest waiting chat
        res_index = self.client.get(reverse('support:desk_index'))
        self.assertEqual(res_index.status_code, 200)
        html_index = res_index.content.decode('utf-8')
        self.assertIn('Очікую вже довго!', html_index)
        self.assertIn('Марія Іваненко', html_index)
        self.assertIn(f'#{t_wait_old.id}', html_index)
        # Oldest waiting appears before newer waiting in queue
        self.assertLess(
            html_index.index(str(s_wait_old.session_uuid)),
            html_index.index(str(s_wait_new.session_uuid)),
        )

        # 2) in_progress tab shows owner & unread count = 2
        res_prog = self.client.get(
            reverse('support:desk_queue_partial'),
            {'tab': 'in_progress'},
        )
        self.assertEqual(res_prog.status_code, 200)
        html_prog = res_prog.content.decode('utf-8')
        self.assertIn('Тарас Петренко', html_prog)
        self.assertIn('Максим Бондар', html_prog)
        self.assertIn('>2<', html_prog.replace(' ', '').replace('\n', ''))

        # 3) ai tab
        res_ai = self.client.get(
            reverse('support:desk_queue_partial'),
            {'tab': 'ai'},
        )
        self.assertEqual(res_ai.status_code, 200)
        self.assertIn('Гість (activeai)', res_ai.content.decode('utf-8'))

        # 4) resolved tab
        res_resolved = self.client.get(
            reverse('support:desk_queue_partial'),
            {'tab': 'resolved'},
        )
        self.assertEqual(res_resolved.status_code, 200)
        self.assertIn('Гість (resolved)', res_resolved.content.decode('utf-8'))

    def test_resolved_tab_caps_at_50_and_search_finds_older_archive(self):
        now = timezone.now()
        for i in range(55):
            sess = ChatSession.objects.create(
                guest_token=f'arch_{i:03d}',
                status=ChatSessionStatus.RESOLVED,
                last_message_at=now - timedelta(minutes=i + 1),
            )
            ChatMessage.objects.create(
                session=sess,
                role=MessageRole.USER,
                content=f'Архівне питання {i}' if i != 54 else 'УнікальнийЗапитТірамісу',
            )

        res_list = self.client.get(
            reverse('support:desk_queue_partial'),
            {'tab': 'resolved'},
        )
        self.assertEqual(res_list.status_code, 200)
        self.assertEqual(
            res_list.content.decode('utf-8').count('data-session-card='),
            50,
        )

        res_search = self.client.get(
            reverse('support:desk_queue_partial'),
            {'tab': 'resolved', 'q': 'УнікальнийЗапитТірамісу'},
        )
        self.assertEqual(res_search.status_code, 200)
        self.assertIn('arch_054', res_search.content.decode('utf-8'))

    def test_search_by_email_phone_ticket_id_order_number_and_message(self):
        s_auth = ChatSession.objects.create(
            user=self.customer_1,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        order_auth = Order.objects.create(
            user=self.customer_1,
            customer_name='Марія Іваненко',
            customer_phone='+380501112233',
            delivery_address='Київ, Хрещатик 1',
            total_amount=Decimal('320.00'),
        )
        ticket_auth = SupportTicket.objects.create(
            session=s_auth,
            order=order_auth,
            customer_phone='+380501112233',
            reason=TicketReason.ORDER_ISSUE,
            details='Проблема з соусом',
            status=TicketStatus.OPEN,
        )
        ChatMessage.objects.create(
            session=s_auth,
            role=MessageRole.USER,
            content='Забули додати кетчуп до бургера',
        )

        s_guest = ChatSession.objects.create(
            guest_token='guest_search_tok',
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        order_guest = Order.objects.create(
            guest_token='guest_search_tok',
            customer_name='Олег Гість',
            customer_phone='+380931234567',
            delivery_address='Київ, Поділ 5',
            total_amount=Decimal('450.00'),
        )
        SupportTicket.objects.create(
            session=s_guest,
            reason=TicketReason.HUMAN_REQUESTED,
            status=TicketStatus.OPEN,
        )

        queue_url = reverse('support:desk_queue_partial')
        for q, expected_uuid in [
            ('maria@example.com', str(s_auth.session_uuid)),
            ('Марія', str(s_auth.session_uuid)),
            ('Марія Іваненко', str(s_auth.session_uuid)),
            ('+380501112233', str(s_auth.session_uuid)),
            (f'#{ticket_auth.id}', str(s_auth.session_uuid)),
            (order_auth.order_number, str(s_auth.session_uuid)),
            ('кетчуп', str(s_auth.session_uuid)),
            (order_guest.order_number, str(s_guest.session_uuid)),
            ('guest_search', str(s_guest.session_uuid)),
        ]:
            res = self.client.get(queue_url, {'tab': 'waiting', 'q': q})
            self.assertEqual(res.status_code, 200)
            body = res.content.decode('utf-8')
            self.assertIn(expected_uuid, body, f'Failed query: {q}')

    def test_search_by_full_name_without_orders_and_guest_order_email(self):
        s_no_orders = ChatSession.objects.create(
            user=self.customer_2,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        s_guest = ChatSession.objects.create(
            guest_token='guest_email_tok',
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        Order.objects.create(
            guest_token='guest_email_tok',
            customer_name='Петро Гість',
            customer_email='guest-order@example.com',
            customer_phone='+380931110022',
            delivery_address='Київ, Андріївський узвіз 1',
            total_amount=Decimal('210.00'),
        )

        queue_url = reverse('support:desk_queue_partial')
        res_full_name = self.client.get(
            queue_url, {'tab': 'waiting', 'q': 'Тарас Петренко'}
        )
        self.assertEqual(res_full_name.status_code, 200)
        self.assertIn(
            str(s_no_orders.session_uuid), res_full_name.content.decode('utf-8')
        )

        res_guest_email = self.client.get(
            queue_url, {'tab': 'waiting', 'q': 'guest-order@example.com'}
        )
        self.assertEqual(res_guest_email.status_code, 200)
        self.assertIn(
            str(s_guest.session_uuid), res_guest_email.content.decode('utf-8')
        )

    def test_resolved_ticket_omits_waiting_timer_and_past_history_requires_session_uuid(self):
        s_resolved = ChatSession.objects.create(
            user=self.customer_1,
            status=ChatSessionStatus.RESOLVED,
        )
        SupportTicket.objects.create(
            session=s_resolved,
            reason=TicketReason.COMPLAINT,
            details='Архівна скарга',
            status=TicketStatus.RESOLVED,
            resolved_at=timezone.now() - timedelta(days=3),
        )
        ChatMessage.objects.create(
            session=s_resolved,
            role=MessageRole.USER,
            content='Архівне звернення',
        )
        s_current_same_user = ChatSession.objects.create(
            user=self.customer_1,
            status=ChatSessionStatus.ACTIVE,
        )
        s_active_other = ChatSession.objects.create(
            user=self.customer_2,
            status=ChatSessionStatus.ACTIVE,
        )

        res_session = self.client.get(
            reverse('support:desk_session', args=[s_resolved.session_uuid])
        )
        self.assertEqual(res_session.status_code, 200)
        html = res_session.content.decode('utf-8')
        self.assertIn('Архівна скарга', html)
        self.assertNotIn('· щойно', html)

        past_url = reverse(
            'support:desk_past_history_partial',
            args=[s_resolved.session_uuid],
        )
        # Missing, invalid, nonexistent, or self session_uuid -> 404
        self.assertEqual(self.client.get(past_url).status_code, 404)
        self.assertEqual(self.client.get(past_url, {'session_uuid': 'bad-uuid'}).status_code, 404)
        self.assertEqual(self.client.get(past_url, {'session_uuid': str(uuid.uuid4())}).status_code, 404)
        self.assertEqual(
            self.client.get(past_url, {'session_uuid': str(s_resolved.session_uuid)}).status_code,
            404,
        )

        # Valid owner session_uuid -> 200 for RESOLVED, 404 for ACTIVE past session
        res_valid = self.client.get(
            past_url, {'session_uuid': str(s_current_same_user.session_uuid)}
        )
        self.assertEqual(res_valid.status_code, 200)
        self.assertIn('Архівне звернення', res_valid.content.decode('utf-8'))

        res_active_past = self.client.get(
            reverse(
                'support:desk_past_history_partial',
                args=[s_active_other.session_uuid],
            ),
            {'session_uuid': str(s_active_other.session_uuid)},
        )
        self.assertEqual(res_active_past.status_code, 404)

    def test_desk_index_empty_state_and_session_view_marks_read(self):
        # Empty waiting queue renders empty workspace state
        res_empty = self.client.get(reverse('support:desk_index'))
        self.assertEqual(res_empty.status_code, 200)
        self.assertIn('Немає звернень в очікуванні', res_empty.content.decode('utf-8'))

        # Opening owned escalated session via desk_session marks messages read
        s_owned = ChatSession.objects.create(
            user=self.customer_1,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
            assigned_operator=self.operator,
        )
        m1 = ChatMessage.objects.create(
            session=s_owned,
            role=MessageRole.USER,
            content='Непрочитане запитання',
        )
        res_open = self.client.get(
            reverse('support:desk_session', args=[s_owned.session_uuid])
        )
        self.assertEqual(res_open.status_code, 200)
        s_owned.refresh_from_db()
        self.assertEqual(s_owned.operator_last_read_id, m1.id)

    def test_context_panel_orders_cart_and_past_history_isolation(self):
        # Current session for customer_1
        s_curr = ChatSession.objects.create(
            user=self.customer_1,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        ticket = SupportTicket.objects.create(
            session=s_curr,
            customer_phone='+380501112233',
            reason=TicketReason.COMPLAINT,
            details='Потрібен чек',
            status=TicketStatus.OPEN,
        )
        ChatMessage.objects.create(
            session=s_curr,
            role=MessageRole.USER,
            content='Привіт, підкажіть по замовленню',
        )
        ChatMessage.objects.create(
            session=s_curr,
            role=MessageRole.ASSISTANT,
            source=MessageSource.AI,
            content='Зараз перевірю!',
        )
        ChatMessage.objects.create(
            session=s_curr,
            role=MessageRole.TOOL,
            source=MessageSource.SYSTEM,
            content='INTERNAL_TOOL_CALL_SECRET',
        )
        ChatMessage.objects.create(
            session=s_curr,
            role=MessageRole.SYSTEM,
            source=MessageSource.SYSTEM,
            content=f'Звернення #{ticket.id} передано адміністратору.',
        )

        # Create 6 orders for customer_1 (context panel must show up to 5) + 1 for customer_2 (must NOT leak)
        for _i in range(6):
            o = Order.objects.create(
                user=self.customer_1,
                customer_name='Марія Іваненко',
                customer_phone='+380501112233',
                delivery_address='Київ, вул. Січових Стрільців 10',
                status=OrderStatus.PREPARING,
                total_amount=Decimal('320.00'),
            )
            OrderItem.objects.create(
                order=o,
                dish=self.dish,
                dish_title=self.dish.title,
                price=self.dish.price,
                quantity=1,
            )
        foreign_order = Order.objects.create(
            user=self.customer_2,
            customer_name='Тарас Петренко',
            customer_phone='+380679998877',
            delivery_address='Київ, Чужа адреса',
            total_amount=Decimal('999.00'),
        )

        # Cart for customer_1
        cart = Cart.objects.create(user=self.customer_1)
        CartItem.objects.create(cart=cart, dish=self.dish, quantity=2)

        # Past resolved session for customer_1 + foreign resolved session for customer_2
        s_past_own = ChatSession.objects.create(
            user=self.customer_1,
            status=ChatSessionStatus.RESOLVED,
        )
        SupportTicket.objects.create(
            session=s_past_own,
            reason=TicketReason.HUMAN_REQUESTED,
            status=TicketStatus.RESOLVED,
            resolved_at=timezone.now() - timedelta(days=1),
        )
        ChatMessage.objects.create(
            session=s_past_own,
            role=MessageRole.USER,
            content='Попереднє питання Марії',
        )
        s_past_foreign = ChatSession.objects.create(
            user=self.customer_2,
            status=ChatSessionStatus.RESOLVED,
        )
        ChatMessage.objects.create(
            session=s_past_foreign,
            role=MessageRole.USER,
            content='Приватне питання Тараса',
        )

        res = self.client.get(
            reverse('support:desk_session', args=[s_curr.session_uuid])
        )
        self.assertEqual(res.status_code, 200)
        html = res.content.decode('utf-8')

        # Thread messages & escalation divider (TOOL message excluded)
        self.assertIn('Привіт, підкажіть по замовленню', html)
        self.assertIn('Зараз перевірю!', html)
        self.assertNotIn('INTERNAL_TOOL_CALL_SECRET', html)
        self.assertIn('Передано оператору', html)

        # Customer card, ticket, cart, orders, past session
        self.assertIn('maria@example.com', html)
        self.assertIn('+380501112233', html)
        self.assertIn('Потрібен чек', html)
        self.assertIn('Трюфельний бургер', html)
        self.assertIn('640 ₴', html)
        self.assertIn('Попереднє питання Марії', html)
        self.assertIn('Вирішено', html)
        self.assertNotIn(foreign_order.order_number, html)
        self.assertNotIn(str(s_past_foreign.session_uuid), html)
        self.assertNotIn('Приватне питання Тараса', html)

        # Past history partial: own succeeds, foreign with session_uuid returns 404
        res_own_past = self.client.get(
            reverse('support:desk_past_history_partial', args=[s_past_own.session_uuid]),
            {'session_uuid': str(s_curr.session_uuid)},
        )
        self.assertEqual(res_own_past.status_code, 200)
        self.assertIn('Попереднє питання Марії', res_own_past.content.decode('utf-8'))

        res_foreign_past = self.client.get(
            reverse('support:desk_past_history_partial', args=[s_past_foreign.session_uuid]),
            {'session_uuid': str(s_curr.session_uuid)},
        )
        self.assertEqual(res_foreign_past.status_code, 404)

    def test_guest_with_empty_token_does_not_leak_other_empty_token_orders_or_carts(self):
        s_empty = ChatSession.objects.create(
            guest_token='',
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        Order.objects.create(
            guest_token='',
            customer_name='Анонімний чужий',
            customer_phone='+380991112233',
            delivery_address='Чужа адреса',
            total_amount=Decimal('555.00'),
        )
        other_cart = Cart.objects.create(guest_token='')
        CartItem.objects.create(cart=other_cart, dish=self.dish, quantity=3)

        res = self.client.get(
            reverse('support:desk_session', args=[s_empty.session_uuid])
        )
        self.assertEqual(res.status_code, 200)
        html = res.content.decode('utf-8')
        self.assertNotIn('Анонімний чужий', html)
        self.assertNotIn('555', html)
        self.assertIn('Кошик порожній', html)

    def test_guest_context_panel_isolation_and_tracking_link_and_404(self):
        s_guest = ChatSession.objects.create(
            guest_token='guest_alpha_1234',
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        guest_order = Order.objects.create(
            guest_token='guest_alpha_1234',
            customer_name='Світлана Гість',
            customer_phone='+380631112233',
            delivery_address='Київ, вул. Саксаганського 12',
            total_amount=Decimal('320.00'),
        )
        other_guest_order = Order.objects.create(
            guest_token='guest_beta_9999',
            customer_name='Чужий Гість',
            customer_phone='+380639998877',
            delivery_address='Київ, Чужа',
            total_amount=Decimal('777.00'),
        )
        guest_cart = Cart.objects.create(guest_token='guest_alpha_1234')
        CartItem.objects.create(cart=guest_cart, dish=self.dish, quantity=1)

        s_guest_past = ChatSession.objects.create(
            guest_token='guest_alpha_1234',
            status=ChatSessionStatus.RESOLVED,
        )
        ChatMessage.objects.create(
            session=s_guest_past,
            role=MessageRole.USER,
            content='Минуле звернення альфа',
        )
        s_other_guest_past = ChatSession.objects.create(
            guest_token='guest_beta_9999',
            status=ChatSessionStatus.RESOLVED,
        )
        ChatMessage.objects.create(
            session=s_other_guest_past,
            role=MessageRole.USER,
            content='Минуле звернення бета',
        )

        res = self.client.get(
            reverse('support:desk_session', args=[s_guest.session_uuid])
        )
        self.assertEqual(res.status_code, 200)
        html = res.content.decode('utf-8')
        self.assertIn('Гість (guest_al)', html)
        self.assertIn('Світлана Гість', html)
        self.assertIn(guest_order.order_number, html)
        self.assertIn('320 ₴', html)
        self.assertIn('Минуле звернення альфа', html)
        self.assertNotIn(other_guest_order.order_number, html)
        self.assertNotIn('Чужий Гість', html)
        self.assertNotIn('Минуле звернення бета', html)

        # Cross-guest past history blocked with 404
        res_cross = self.client.get(
            reverse(
                'support:desk_past_history_partial',
                args=[s_other_guest_past.session_uuid],
            ),
            {'session_uuid': str(s_guest.session_uuid)},
        )
        self.assertEqual(res_cross.status_code, 404)

        # Operator can open the guest order tracking page
        res_tracking = self.client.get(
            reverse('orders:tracking', args=[guest_order.order_number])
        )
        self.assertEqual(res_tracking.status_code, 200)

        # Nonexistent session UUID -> 404
        self.assertEqual(
            self.client.get(
                reverse('support:desk_session', args=[uuid.uuid4()])
            ).status_code,
            404,
        )

    def test_context_panel_displays_unavailable_cart_items(self):
        unavailable_dish = Dish.objects.create(
            category=self.cat,
            title='Сезонний суп',
            slug='seasonal-soup',
            price=Decimal('180.00'),
            weight_grams=300,
            is_available=False,
        )
        session = ChatSession.objects.create(
            user=self.customer_1,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        cart = Cart.objects.create(user=self.customer_1)
        CartItem.objects.create(cart=cart, dish=unavailable_dish, quantity=2)

        res = self.client.get(
            reverse('support:desk_session', args=[session.session_uuid])
        )
        self.assertEqual(res.status_code, 200)
        html = res.content.decode('utf-8')
        self.assertIn('Сезонний суп', html)
        self.assertIn('Недоступно', html)
        self.assertNotIn('Кошик порожній.', html)


class SupportDeskActionsApiTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client_op1 = Client()
        self.client_op2 = Client()
        self.op1 = User.objects.create_user(
            email='action_op1@example.com',
            password='password123',
            first_name='Олена',
            last_name='Коваль',
            role=Role.RESTAURANT_ADMIN,
        )
        self.op2 = User.objects.create_user(
            email='action_op2@example.com',
            password='password123',
            first_name='Максим',
            last_name='Бондар',
            role=Role.RESTAURANT_ADMIN,
        )
        self.customer = User.objects.create_user(
            email='action_customer@example.com',
            password='password123',
            first_name='Іван',
            last_name='Франко',
            role=Role.CUSTOMER,
        )
        self.client_op1.force_login(self.op1)
        self.client_op2.force_login(self.op2)

    def test_claim_conflict_reply_return_ai_and_resolve_lifecycle(self):
        session = ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        ticket = SupportTicket.objects.create(
            session=session,
            reason=TicketReason.HUMAN_REQUESTED,
            status=TicketStatus.OPEN,
        )

        # Reply before claim -> 409
        res_reply_unclaimed = self.client_op1.post(
            reverse('support:desk_reply', args=[session.session_uuid]),
            data=json.dumps({'message': 'Рано відповідаю'}),
            content_type='application/json',
        )
        self.assertEqual(res_reply_unclaimed.status_code, 409)

        # Op1 claims -> 200
        res_claim = self.client_op1.post(
            reverse('support:desk_claim', args=[session.session_uuid])
        )
        self.assertEqual(res_claim.status_code, 200)
        claim_data = res_claim.json()
        self.assertEqual(claim_data['assigned_operator_id'], self.op1.id)
        self.assertTrue(claim_data['is_owned_by_me'])
        self.assertEqual(claim_data['ticket_status'], TicketStatus.IN_PROGRESS)

        # Op2 tries to claim -> 409 with owner name
        res_claim_conflict = self.client_op2.post(
            reverse('support:desk_claim', args=[session.session_uuid])
        )
        self.assertEqual(res_claim_conflict.status_code, 409)
        self.assertIn('Олена Коваль', res_claim_conflict.json()['error'])

        # Op2 tries to reply -> 409
        res_reply_other = self.client_op2.post(
            reverse('support:desk_reply', args=[session.session_uuid]),
            data=json.dumps({'message': 'Чужий оператор'}),
            content_type='application/json',
        )
        self.assertEqual(res_reply_other.status_code, 409)

        # Op1 replies -> 201
        res_reply = self.client_op1.post(
            reverse('support:desk_reply', args=[session.session_uuid]),
            data=json.dumps({'message': 'Добрий день! Вже перевіряю замовлення.'}),
            content_type='application/json',
        )
        self.assertEqual(res_reply.status_code, 201)
        reply_data = res_reply.json()
        self.assertEqual(reply_data['message']['role'], MessageRole.OPERATOR)
        self.assertEqual(reply_data['message']['source'], MessageSource.WEB)
        self.assertEqual(reply_data['message']['operator_name'], 'Олена Коваль')

        # Empty / overlong message -> 400
        res_empty = self.client_op1.post(
            reverse('support:desk_reply', args=[session.session_uuid]),
            data=json.dumps({'message': '   '}),
            content_type='application/json',
        )
        self.assertEqual(res_empty.status_code, 400)

        res_long = self.client_op1.post(
            reverse('support:desk_reply', args=[session.session_uuid]),
            data=json.dumps({'message': 'а' * (MAX_SUPPORT_MESSAGE_LENGTH + 1)}),
            content_type='application/json',
        )
        self.assertEqual(res_long.status_code, 400)

        # Op2 takes over -> 200
        res_takeover = self.client_op2.post(
            reverse('support:desk_takeover', args=[session.session_uuid]),
            data='{}',
            content_type='application/json',
        )
        self.assertEqual(res_takeover.status_code, 200)
        self.assertEqual(res_takeover.json()['assigned_operator_id'], self.op2.id)

        # Return to AI -> 200, session becomes ACTIVE, ticket RESOLVED
        res_return = self.client_op2.post(
            reverse('support:desk_return_ai', args=[session.session_uuid])
        )
        self.assertEqual(res_return.status_code, 200)
        session.refresh_from_db()
        ticket.refresh_from_db()
        self.assertEqual(session.status, ChatSessionStatus.ACTIVE)
        self.assertFalse(session.is_escalated)
        self.assertIsNone(session.assigned_operator)
        self.assertEqual(ticket.status, TicketStatus.RESOLVED)

        # Proactive takeover on ACTIVE AI session -> 200, creates OPERATOR_TAKEOVER ticket
        res_proactive = self.client_op1.post(
            reverse('support:desk_takeover', args=[session.session_uuid]),
            data=json.dumps({'details': 'Клієнт потребує знижки'}),
            content_type='application/json',
        )
        self.assertEqual(res_proactive.status_code, 200)
        new_ticket_id = res_proactive.json()['ticket_id']
        new_ticket = SupportTicket.objects.get(pk=new_ticket_id)
        self.assertEqual(new_ticket.reason, TicketReason.OPERATOR_TAKEOVER)
        self.assertEqual(new_ticket.status, TicketStatus.IN_PROGRESS)
        self.assertEqual(new_ticket.assigned_operator, self.op1)

        # Resolve session -> 200
        res_resolve = self.client_op1.post(
            reverse('support:desk_resolve', args=[session.session_uuid])
        )
        self.assertEqual(res_resolve.status_code, 200)
        session.refresh_from_db()
        new_ticket.refresh_from_db()
        self.assertEqual(session.status, ChatSessionStatus.RESOLVED)
        self.assertEqual(new_ticket.status, TicketStatus.RESOLVED)

        # Actions on RESOLVED -> 409
        self.assertEqual(
            self.client_op1.post(
                reverse('support:desk_takeover', args=[session.session_uuid])
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client_op1.post(
                reverse('support:desk_resolve', args=[session.session_uuid])
            ).status_code,
            409,
        )

    def test_reply_rate_limit_60_per_minute(self):
        session = ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
            assigned_operator=self.op1,
        )
        url = reverse('support:desk_reply', args=[session.session_uuid])
        for i in range(60):
            res = self.client_op1.post(
                url,
                data=json.dumps({'message': f'Відповідь {i}'}),
                content_type='application/json',
            )
            self.assertEqual(res.status_code, 201)

        blocked = self.client_op1.post(
            url,
            data=json.dumps({'message': 'Повідомлення 61'}),
            content_type='application/json',
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('Retry-After', blocked.headers)

    def test_desk_messages_api_after_id_and_read_tracking(self):
        session = ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
            assigned_operator=self.op1,
        )
        m1 = ChatMessage.objects.create(
            session=session,
            role=MessageRole.USER,
            content='Перше питання',
        )
        m2 = ChatMessage.objects.create(
            session=session,
            role=MessageRole.USER,
            content='Друге питання',
        )
        url = reverse('support:desk_messages_api', args=[session.session_uuid])

        # Colleague reading does NOT mark session read for op1
        res_op2 = self.client_op2.get(url)
        self.assertEqual(res_op2.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.operator_last_read_id, 0)

        # Op1 incremental fetch marks read
        res_inc = self.client_op1.get(url, {'after_id': m1.id})
        self.assertEqual(res_inc.status_code, 200)
        data = res_inc.json()
        self.assertEqual(len(data['messages']), 1)
        self.assertEqual(data['messages'][0]['id'], m2.id)
        self.assertEqual(data['last_message_id'], m2.id)
        session.refresh_from_db()
        self.assertEqual(session.operator_last_read_id, m2.id)

        # Invalid after_id -> 400
        self.assertEqual(
            self.client_op1.get(url, {'after_id': 'bad'}).status_code,
            400,
        )

        # Nonexistent UUID -> 404
        self.assertEqual(
            self.client_op1.get(
                reverse('support:desk_messages_api', args=[uuid.uuid4()])
            ).status_code,
            404,
        )


class SupportEntryPointsEmailsAndContextProcessorTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.factory = RequestFactory()
        self.customer = User.objects.create_user(
            email='entry_customer@example.com',
            password='password123',
            role=Role.CUSTOMER,
        )
        self.operator = User.objects.create_user(
            email='entry_operator@example.com',
            password='password123',
            role=Role.RESTAURANT_ADMIN,
        )

    def test_context_processor_does_zero_support_queries_for_customer_and_anonymous(self):
        ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )

        req_anon = self.factory.get('/')
        req_anon.user = type('Anon', (), {'is_authenticated': False})()
        with CaptureQueriesContext(connection) as ctx_anon:
            ctx = support_context(req_anon)
            self.assertFalse(bool(ctx['is_support_operator']))
            self.assertEqual(int(ctx['support_waiting_count']), 0)
        self.assertEqual(len(ctx_anon), 0)

        req_cust = self.factory.get('/')
        req_cust.user = self.customer
        with CaptureQueriesContext(connection) as ctx_cust:
            ctx = support_context(req_cust)
            self.assertFalse(bool(ctx['is_support_operator']))
            self.assertEqual(int(ctx['support_waiting_count']), 0)
        self.assertEqual(len(ctx_cust), 0)

        req_op = self.factory.get('/')
        req_op.user = self.operator
        ctx = support_context(req_op)
        self.assertTrue(bool(ctx['is_support_operator']))
        self.assertEqual(int(ctx['support_waiting_count']), 1)

    def test_base_and_profile_entry_points_and_widget_hidden_on_desk(self):
        ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )

        # Customer: sees chat widget, does NOT see Підтримка link
        self.client.force_login(self.customer)
        res_home_cust = self.client.get('/')
        self.assertEqual(res_home_cust.status_code, 200)
        html_cust = res_home_cust.content.decode('utf-8')
        self.assertIn('id="ai-chat-widget"', html_cust)
        self.assertNotIn('/support/desk/', html_cust)

        res_prof_cust = self.client.get(reverse('accounts:profile'))
        self.assertEqual(res_prof_cust.status_code, 200)
        self.assertNotIn('/support/desk/', res_prof_cust.content.decode('utf-8'))

        # Operator: sees Підтримка link + waiting count on home & profile, and chat widget is hidden on /support/desk/
        self.client.force_login(self.operator)
        res_home_op = self.client.get('/')
        self.assertEqual(res_home_op.status_code, 200)
        html_op = res_home_op.content.decode('utf-8')
        self.assertIn('/support/desk/', html_op)
        self.assertIn('Підтримка', html_op)
        self.assertIn('id="ai-chat-widget"', html_op)

        res_prof_op = self.client.get(reverse('accounts:profile'))
        self.assertEqual(res_prof_op.status_code, 200)
        self.assertIn('Відкрити пульт підтримки', res_prof_op.content.decode('utf-8'))

        res_desk = self.client.get(reverse('support:desk_index'))
        self.assertEqual(res_desk.status_code, 200)
        self.assertNotIn('id="ai-chat-widget"', res_desk.content.decode('utf-8'))

    def test_escalation_email_contains_desk_session_url_and_admin_url(self):
        from support.emails import send_ticket_escalated_email

        session = ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        ticket = SupportTicket.objects.create(
            session=session,
            reason=TicketReason.COMPLAINT,
            details='Тестова ескалація',
            status=TicketStatus.OPEN,
        )
        mail.outbox.clear()
        with patch('support.emails.resolve_site_domain', return_value='doublebite.ua'):
            send_ticket_escalated_email(ticket, recipient_email='admin@doublebite.ua')

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        expected_desk_path = f'/support/desk/{session.session_uuid}/'
        expected_admin_path = f'/admin/support/supportticket/{ticket.id}/change/'
        self.assertIn(expected_desk_path, sent.body)
        self.assertIn(expected_admin_path, sent.body)
        html = sent.alternatives[0][0]
        self.assertIn(expected_desk_path, html)
        self.assertIn('Відкрити чат у пульті', html)
        self.assertIn(expected_admin_path, html)
        self.assertIn('Переглянути тікет в адмінці', html)

    def test_admin_desk_links(self):
        session = ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
        )
        ticket = SupportTicket.objects.create(
            session=session,
            reason=TicketReason.HUMAN_REQUESTED,
            status=TicketStatus.OPEN,
        )
        session_admin = ChatSessionAdmin(ChatSession, admin_site)
        ticket_admin = SupportTicketAdmin(SupportTicket, admin_site)
        expected_url = reverse('support:desk_session', args=[session.session_uuid])
        self.assertIn(expected_url, session_admin.desk_link(session))
        self.assertIn('Відкрити чат у пульті', session_admin.desk_link(session))
        self.assertIn(expected_url, ticket_admin.desk_link(ticket))
        self.assertIn('Відкрити чат у пульті', ticket_admin.desk_link(ticket))

    def test_support_ticket_admin_mark_resolved_clears_operator_and_resolves_session(self):
        session = ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
            assigned_operator=self.operator,
            assigned_at=timezone.now(),
        )
        ticket = SupportTicket.objects.create(
            session=session,
            reason=TicketReason.COMPLAINT,
            status=TicketStatus.IN_PROGRESS,
            assigned_operator=self.operator,
        )
        session_with_other_open = ChatSession.objects.create(
            guest_token='multi_ticket_guest',
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
            assigned_operator=self.operator,
            assigned_at=timezone.now(),
        )
        ticket_resolved_in_multi = SupportTicket.objects.create(
            session=session_with_other_open,
            reason=TicketReason.ORDER_ISSUE,
            status=TicketStatus.RESOLVED,
        )
        ticket_still_open = SupportTicket.objects.create(
            session=session_with_other_open,
            reason=TicketReason.HUMAN_REQUESTED,
            status=TicketStatus.OPEN,
        )

        ticket_admin = SupportTicketAdmin(SupportTicket, admin_site)
        req = self.factory.post('/admin/support/supportticket/')
        req.user = self.operator
        qs = SupportTicket.objects.filter(
            pk__in=[ticket.pk, ticket_resolved_in_multi.pk]
        )
        ticket_admin.mark_resolved(req, qs)

        ticket.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatus.RESOLVED)
        self.assertIsNotNone(ticket.resolved_at)
        self.assertEqual(session.status, ChatSessionStatus.RESOLVED)
        self.assertFalse(session.is_escalated)
        self.assertIsNone(session.assigned_operator)
        self.assertIsNone(session.assigned_at)

        # Session with another still-open ticket stays ESCALATED with operator
        session_with_other_open.refresh_from_db()
        ticket_still_open.refresh_from_db()
        self.assertEqual(ticket_still_open.status, TicketStatus.OPEN)
        self.assertEqual(session_with_other_open.status, ChatSessionStatus.ESCALATED)
        self.assertTrue(session_with_other_open.is_escalated)
        self.assertEqual(session_with_other_open.assigned_operator, self.operator)

        # Already-RESOLVED ticket on an ACTIVE AI session (after return_to_ai)
        # must NOT resolve the ACTIVE session or overwrite resolved_at.
        old_resolved_at = timezone.now() - timedelta(days=2)
        active_session = ChatSession.objects.create(
            guest_token='returned_to_ai_guest',
            status=ChatSessionStatus.ACTIVE,
            is_escalated=False,
        )
        old_resolved_ticket = SupportTicket.objects.create(
            session=active_session,
            reason=TicketReason.BOT_CONFUSED,
            status=TicketStatus.RESOLVED,
            resolved_at=old_resolved_at,
        )
        ticket_admin.mark_resolved(
            req, SupportTicket.objects.filter(pk=old_resolved_ticket.pk)
        )
        active_session.refresh_from_db()
        old_resolved_ticket.refresh_from_db()
        self.assertEqual(active_session.status, ChatSessionStatus.ACTIVE)
        self.assertEqual(old_resolved_ticket.resolved_at, old_resolved_at)
