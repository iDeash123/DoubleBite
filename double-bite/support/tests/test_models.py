import uuid
from datetime import timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from orders.models import Order, OrderStatus

from support.models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    FAQKnowledge,
    Message,
    MessageRole,
    SupportTicket,
    TicketReason,
    TicketStatus,
)

User = get_user_model()


class SupportConfigTest(TestCase):
    def test_app_is_installed(self):
        self.assertTrue(apps.is_installed('support'))
        config = apps.get_app_config('support')
        self.assertEqual(config.verbose_name, 'Служба підтримки')


class ChatSessionModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='client@example.com',
            password='testpassword123',
            phone='+380501234567',
        )

    def test_create_authenticated_chat_session(self):
        session = ChatSession.objects.create(
            user=self.user,
            status=ChatSessionStatus.ACTIVE,
        )
        self.assertIsInstance(session.session_uuid, uuid.UUID)
        self.assertEqual(session.user, self.user)
        self.assertEqual(session.guest_token, '')
        self.assertEqual(session.status, ChatSessionStatus.ACTIVE)
        self.assertFalse(session.is_escalated)
        self.assertIn('client@example.com', str(session))

    def test_create_guest_chat_session(self):
        session = ChatSession.objects.create(
            guest_token='guest_session_12345',
            status=ChatSessionStatus.ACTIVE,
        )
        self.assertIsNone(session.user)
        self.assertEqual(session.guest_token, 'guest_session_12345')
        self.assertIn('guest_se', str(session))

    def test_partial_unique_constraints_on_active_sessions(self):
        ChatSession.objects.create(user=self.user, status=ChatSessionStatus.ACTIVE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ChatSession.objects.create(user=self.user, status=ChatSessionStatus.ACTIVE)

        with self.assertRaises(IntegrityError), transaction.atomic():
            ChatSession.objects.create(user=self.user, status=ChatSessionStatus.ESCALATED)

        resolved_user_session = ChatSession.objects.create(
            user=self.user,
            status=ChatSessionStatus.RESOLVED,
        )
        self.assertEqual(resolved_user_session.status, ChatSessionStatus.RESOLVED)

        ChatSession.objects.create(
            user=None,
            guest_token='guest-tok-unique',
            status=ChatSessionStatus.ACTIVE,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ChatSession.objects.create(
                user=None,
                guest_token='guest-tok-unique',
                status=ChatSessionStatus.ESCALATED,
            )

        resolved_guest = ChatSession.objects.create(
            user=None,
            guest_token='guest-tok-unique',
            status=ChatSessionStatus.RESOLVED,
        )
        self.assertEqual(resolved_guest.status, ChatSessionStatus.RESOLVED)

        empty_a = ChatSession.objects.create(user=None, guest_token='', status=ChatSessionStatus.ACTIVE)
        empty_b = ChatSession.objects.create(user=None, guest_token='', status=ChatSessionStatus.ACTIVE)
        self.assertNotEqual(empty_a.pk, empty_b.pk)


class ChatMessageModelTest(TestCase):
    def setUp(self):
        self.session = ChatSession.objects.create(guest_token='test_guest_session')

    def test_message_alias_is_chat_message(self):
        self.assertIs(Message, ChatMessage)

    def test_create_messages_and_operator_role(self):
        user_msg = ChatMessage.objects.create(
            session=self.session,
            role=MessageRole.USER,
            content='Доброго дня! Підкажіть статус замовлення.',
        )
        assistant_msg = ChatMessage.objects.create(
            session=self.session,
            role=MessageRole.ASSISTANT,
            content='Доброго дня! Зараз перевірю ваше замовлення.',
        )
        operator_msg = ChatMessage.objects.create(
            session=self.session,
            role=MessageRole.OPERATOR,
            content='Адміністратор підключився до діалогу.',
        )
        tool_msg = ChatMessage.objects.create(
            session=self.session,
            role=MessageRole.TOOL,
            content='{"status": "ON_WAY"}',
            tool_calls={'name': 'check_order_status', 'id': 'call_1'},
        )

        self.assertEqual(self.session.messages.count(), 4)
        self.assertIn('[user]', str(user_msg))
        self.assertIn('[assistant]', str(assistant_msg))
        self.assertIn('[operator]', str(operator_msg))
        self.assertIn('[tool]', str(tool_msg))
        self.assertEqual(tool_msg.tool_calls['id'], 'call_1')

    def test_chat_message_ordering_uses_created_at_and_id_tiebreaker(self):
        self.assertEqual(ChatMessage._meta.ordering, ('created_at', 'id'))
        fixed_time = timezone.now()
        msg1 = ChatMessage.objects.create(session=self.session, role=MessageRole.USER, content='First')
        msg2 = ChatMessage.objects.create(session=self.session, role=MessageRole.ASSISTANT, content='Second')
        msg3 = ChatMessage.objects.create(session=self.session, role=MessageRole.OPERATOR, content='Third')
        ChatMessage.objects.filter(pk__in=[msg1.pk, msg2.pk, msg3.pk]).update(created_at=fixed_time)

        ordered = list(self.session.messages.all())
        self.assertEqual([m.pk for m in ordered], [msg1.pk, msg2.pk, msg3.pk])


class SupportTicketModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='ticket_user@example.com',
            password='testpassword123',
            phone='+380509998877',
        )
        self.session = ChatSession.objects.create(user=self.user)
        self.order = Order.objects.create(
            user=self.user,
            customer_name='Олександр',
            customer_phone='+380509998877',
            delivery_address='вул. Хрещатик, 1',
            status=OrderStatus.PENDING,
            total_amount=350.00,
        )

    def test_create_support_ticket(self):
        ticket = SupportTicket.objects.create(
            session=self.session,
            order=self.order,
            customer_phone='+380509998877',
            reason=TicketReason.COMPLAINT,
            details='Клієнт скаржиться на холодну піцу',
            status=TicketStatus.OPEN,
        )
        self.assertEqual(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.reason, TicketReason.COMPLAINT)
        self.assertEqual(ticket.order, self.order)
        self.assertIn('Скарга', str(ticket))

    def test_unique_open_ticket_per_session_constraint(self):
        SupportTicket.objects.create(
            session=self.session,
            reason=TicketReason.COMPLAINT,
            status=TicketStatus.OPEN,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SupportTicket.objects.create(
                session=self.session,
                reason=TicketReason.HUMAN_REQUESTED,
                status=TicketStatus.IN_PROGRESS,
            )

        resolved_ticket = SupportTicket.objects.create(
            session=self.session,
            reason=TicketReason.BOT_CONFUSED,
            status=TicketStatus.RESOLVED,
        )
        self.assertEqual(resolved_ticket.status, TicketStatus.RESOLVED)


class FAQKnowledgeModelTest(TestCase):
    def test_create_faq_entry(self):
        faq = FAQKnowledge.objects.create(
            question='Який графік роботи ресторану?',
            answer='Ми працюємо щодня з 10:00 до 22:00 без вихідних.',
            category='Графік роботи',
            is_active=True,
        )
        self.assertEqual(str(faq), 'Який графік роботи ресторану?')
        self.assertTrue(faq.is_active)


class SupportMigration0002Test(TransactionTestCase):
    def test_migration_0002_migrates_tokens_and_deduplicates_sessions_and_tickets(self):
        target_0001 = [('support', '0001_initial')]
        target_0002 = [('support', '0002_guest_token_constraints_and_message_ordering')]

        executor = MigrationExecutor(connection)
        executor.migrate(target_0001)
        try:
            old_apps = executor.loader.project_state(target_0001).apps
            OldUser = old_apps.get_model('accounts', 'User')
            OldChatSession = old_apps.get_model('support', 'ChatSession')
            OldSupportTicket = old_apps.get_model('support', 'SupportTicket')

            user = OldUser.objects.create(email='mig0002@doublebite.ua', password='x')

            user_old_sess = OldChatSession.objects.create(
                user=user,
                session_key='stale_auth_sess_key',
                status='ACTIVE',
                is_escalated=False,
            )
            user_new_sess = OldChatSession.objects.create(
                user=user,
                session_key='newer_auth_sess_key',
                status='ESCALATED',
                is_escalated=True,
            )
            older_time = timezone.now() - timedelta(hours=2)
            OldChatSession.objects.filter(pk=user_old_sess.pk).update(
                created_at=older_time,
                updated_at=older_time,
            )

            guest_old_sess = OldChatSession.objects.create(
                user=None,
                session_key='legacy_guest_session_key',
                status='ACTIVE',
                is_escalated=True,
            )
            guest_new_sess = OldChatSession.objects.create(
                user=None,
                session_key='legacy_guest_session_key',
                status='ACTIVE',
                is_escalated=False,
            )
            OldChatSession.objects.filter(pk=guest_old_sess.pk).update(
                created_at=older_time,
                updated_at=older_time,
            )

            ticket_old = OldSupportTicket.objects.create(
                session=user_new_sess,
                reason='COMPLAINT',
                details='Older ticket',
                status='OPEN',
            )
            ticket_new = OldSupportTicket.objects.create(
                session=user_new_sess,
                reason='ORDER_ISSUE',
                details='Newer ticket',
                status='IN_PROGRESS',
            )
            OldSupportTicket.objects.filter(pk=ticket_old.pk).update(created_at=older_time)

            executor_fwd = MigrationExecutor(connection)
            executor_fwd.migrate(target_0002)
            new_apps = executor_fwd.loader.project_state(target_0002).apps

            NewChatSession = new_apps.get_model('support', 'ChatSession')
            NewSupportTicket = new_apps.get_model('support', 'SupportTicket')

            user_old_after = NewChatSession.objects.get(pk=user_old_sess.pk)
            user_new_after = NewChatSession.objects.get(pk=user_new_sess.pk)
            self.assertEqual(user_old_after.status, 'RESOLVED')
            self.assertFalse(user_old_after.is_escalated)
            self.assertEqual(user_old_after.guest_token, '')
            self.assertEqual(user_new_after.status, 'ESCALATED')
            self.assertTrue(user_new_after.is_escalated)
            self.assertEqual(user_new_after.guest_token, '')

            guest_old_after = NewChatSession.objects.get(pk=guest_old_sess.pk)
            guest_new_after = NewChatSession.objects.get(pk=guest_new_sess.pk)
            self.assertTrue(bool(guest_new_after.guest_token))
            self.assertNotEqual(guest_new_after.guest_token, 'legacy_guest_session_key')
            self.assertEqual(guest_old_after.guest_token, guest_new_after.guest_token)
            self.assertEqual(guest_old_after.status, 'RESOLVED')
            self.assertFalse(guest_old_after.is_escalated)
            self.assertEqual(guest_new_after.status, 'ACTIVE')

            ticket_old_after = NewSupportTicket.objects.get(pk=ticket_old.pk)
            ticket_new_after = NewSupportTicket.objects.get(pk=ticket_new.pk)
            self.assertEqual(ticket_old_after.status, 'RESOLVED')
            self.assertIsNotNone(ticket_old_after.resolved_at)
            self.assertEqual(ticket_new_after.status, 'IN_PROGRESS')
        finally:
            MigrationExecutor(connection).migrate(target_0002)
