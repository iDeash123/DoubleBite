import uuid

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
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
        self.assertEqual(session.status, ChatSessionStatus.ACTIVE)
        self.assertFalse(session.is_escalated)
        self.assertIn('client@example.com', str(session))

    def test_create_guest_chat_session(self):
        session = ChatSession.objects.create(
            session_key='guest_session_12345',
            status=ChatSessionStatus.ACTIVE,
        )
        self.assertIsNone(session.user)
        self.assertEqual(session.session_key, 'guest_session_12345')
        self.assertIn('guest_se', str(session))


class ChatMessageModelTest(TestCase):
    def setUp(self):
        self.session = ChatSession.objects.create(session_key='test_guest_session')

    def test_message_alias_is_chat_message(self):
        self.assertIs(Message, ChatMessage)

    def test_create_messages(self):
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
        tool_msg = ChatMessage.objects.create(
            session=self.session,
            role=MessageRole.TOOL,
            content='{"status": "ON_WAY"}',
            tool_calls={'name': 'check_order_status', 'id': 'call_1'},
        )

        self.assertEqual(self.session.messages.count(), 3)
        self.assertIn('[user]', str(user_msg))
        self.assertIn('[assistant]', str(assistant_msg))
        self.assertIn('[tool]', str(tool_msg))
        self.assertEqual(tool_msg.tool_calls['id'], 'call_1')


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
