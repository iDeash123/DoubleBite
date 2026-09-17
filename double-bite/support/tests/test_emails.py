from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import RequestFactory, TestCase
from orders.models import Order

from support.agent.tools import execute_agent_tool
from support.models import ChatMessage, ChatSession, TicketReason
from support.services import ChatSessionService

User = get_user_model()


class TicketEscalationEmailTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            email='supportuser@example.com',
            password='TestPassword123!',
            first_name='Михайло',
            last_name='Грушевський',
            phone='+380509876543',
        )
        self.session = ChatSession.objects.create(
            user=self.user,
        )
        self.order = Order.objects.create(
            user=self.user,
            customer_name='Михайло Грушевський',
            customer_phone='+380509876543',
            delivery_address='Київ, вул. Володимирська, 54',
            total_amount=Decimal('420.00'),
        )
        mail.outbox.clear()

    def test_escalate_session_sends_admin_escalation_email(self):
        ticket = ChatSessionService.escalate_session(
            session=self.session,
            reason=TicketReason.COMPLAINT,
            details='Страва приїхала холодна',
            order=self.order,
            customer_phone='+380509876543',
        )

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        expected_admin = getattr(settings, 'ADMIN_EMAIL', 'admin@doublebite.ua')
        self.assertEqual(sent.to, [expected_admin])
        self.assertIn('[ТЕРМІНОВО]', sent.subject)
        self.assertIn(f'#{ticket.id}', sent.subject)
        self.assertIn('Страва приїхала холодна', sent.body)
        self.assertIn(self.order.order_number, sent.body)
        self.assertIn('+380509876543', sent.body)
        self.assertIn(f'/admin/support/supportticket/{ticket.id}/change/', sent.body)

        self.assertEqual(len(sent.alternatives), 1)
        self.assertEqual(sent.alternatives[0][1], 'text/html')
        html = sent.alternatives[0][0]
        self.assertIn('#F6F6F6', html)
        self.assertIn('#262626', html)
        self.assertIn('JetBrains Mono', html)
        self.assertIn(f'#{ticket.id}', html)

    def test_escalate_to_operator_tool_triggers_admin_email(self):
        request = self.factory.get('/support/chat/stream/')
        request.user = self.user
        request.session = {'session_key': 'tool-session-key'}

        result = execute_agent_tool(
            'escalate_to_operator',
            {
                'reason': TicketReason.HUMAN_REQUESTED,
                'details': 'Клієнт вимагає зв\'язатися з менеджером ресторану',
                'order_id': self.order.id,
            },
            request=request,
            session=self.session,
        )

        self.assertIsNotNone(result.get('ticket_id'))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertIn('[ТЕРМІНОВО]', sent.subject)
        self.assertIn('менеджером ресторану', sent.body)

    def test_guest_session_escalation_email(self):
        guest_session = ChatSession.objects.create(
            session_key='guest-sess-abc',
        )
        mail.outbox.clear()

        ticket = ChatSessionService.escalate_session(
            session=guest_session,
            reason=TicketReason.HUMAN_REQUESTED,
            details='Запит від неавторизованого клієнта',
            customer_phone='+380671112233',
        )

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertIn('Гість', sent.body)
        self.assertIn('+380671112233', sent.body)
        self.assertIn(f'#{ticket.id}', sent.body)

    def test_escalation_email_includes_dialogue_excerpt(self):
        ChatMessage.objects.create(
            session=self.session,
            role='user',
            content='Де моє замовлення?',
        )
        ChatMessage.objects.create(
            session=self.session,
            role='assistant',
            content='Зараз перевірю стан доставки.',
        )
        mail.outbox.clear()

        ChatSessionService.escalate_session(
            session=self.session,
            reason=TicketReason.ORDER_ISSUE,
            details='Затримка кур\'єра на 20 хвилин',
            order=self.order,
        )

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertIn('Витяг з діалогу', sent.body)
        self.assertIn('Де моє замовлення?', sent.body)
        self.assertIn('Зараз перевірю стан доставки.', sent.body)
        self.assertEqual(len(sent.alternatives), 1)
        html = sent.alternatives[0][0]
        self.assertIn('#F4F1EB', html)
        self.assertIn('Де моє замовлення?', html)

    def test_escalation_empty_email_and_phone_fallback(self):
        user_no_contact = User.objects.create_user(
            email='blankcontact@example.com',
            password='TestPassword123!',
            first_name='',
            last_name='',
            phone='',
        )
        session = ChatSession.objects.create(user=user_no_contact)
        mail.outbox.clear()

        ticket = ChatSessionService.escalate_session(
            session=session,
            reason=TicketReason.BOT_CONFUSED,
            details='Бот не зміг розпізнати запит',
            customer_phone='',
        )

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertIn(f'#{ticket.id}', sent.body)
        self.assertIn('Не вказано', sent.body)
