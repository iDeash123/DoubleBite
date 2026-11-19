from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.test import TestCase

from support.admin import (
    ChatMessageAdmin,
    SupportTicketAdmin,
)
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

User = get_user_model()


class SupportAdminTest(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            email='admin@example.com',
            password='adminpassword123',
        )
        self.session = ChatSession.objects.create(
            guest_token='adm_session_1',
            status=ChatSessionStatus.ESCALATED,
            is_escalated=True,
            escalation_reason='Бот не зрозумів питання про безлактозні десерти',
        )
        self.ticket = SupportTicket.objects.create(
            session=self.session,
            reason=TicketReason.BOT_CONFUSED,
            details='Бот не зрозумів питання про безлактозні десерти',
            status=TicketStatus.OPEN,
        )
        self.msg = ChatMessage.objects.create(
            session=self.session,
            role=MessageRole.USER,
            content='Чи є у вас безлактозні десерти?',
        )

    def test_admin_models_registered(self):
        self.assertIn(ChatSession, site._registry)
        self.assertIn(ChatMessage, site._registry)
        self.assertIn(SupportTicket, site._registry)
        self.assertIn(FAQKnowledge, site._registry)
        session_admin = site._registry[ChatSession]
        self.assertNotIn('session_key', session_admin.list_display)
        self.assertNotIn('session_key', session_admin.search_fields)
        self.assertNotIn('guest_token', session_admin.list_display)
        self.assertNotIn('guest_token', session_admin.search_fields)
        self.assertIn('guest_token', session_admin.exclude)

    def test_support_ticket_admin_actions_reset_chat_session(self):
        admin_obj = SupportTicketAdmin(SupportTicket, site)
        queryset = SupportTicket.objects.filter(id=self.ticket.id)

        admin_obj.mark_in_progress(None, queryset)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatus.IN_PROGRESS)
        self.session.refresh_from_db()
        self.assertTrue(self.session.is_escalated)
        self.assertEqual(self.session.status, ChatSessionStatus.ESCALATED)

        admin_obj.mark_resolved(None, queryset)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatus.RESOLVED)
        self.assertIsNotNone(self.ticket.resolved_at)

        self.session.refresh_from_db()
        self.assertFalse(self.session.is_escalated)
        self.assertEqual(self.session.status, ChatSessionStatus.RESOLVED)

    def test_chat_message_content_snippet(self):
        admin_obj = ChatMessageAdmin(ChatMessage, site)
        snippet = admin_obj.content_snippet(self.msg)
        self.assertEqual(snippet, 'Чи є у вас безлактозні десерти?')
