import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from accounts.models import Role
from config import constants as config_constants
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpResponse
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.utils import timezone
from django.utils.functional import SimpleLazyObject
from orders.middleware import (
    GUEST_TOKEN_SESSION_KEY,
    PRE_LOGIN_GUEST_TOKEN_SESSION_KEY,
)

from support.agent.client import GeminiSupportAgent
from support.agent.prompts import SYSTEM_PROMPT
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
from support.permissions import is_support_operator, support_operator_required
from support.services import (
    MAX_SUPPORT_MESSAGE_LENGTH,
    ChatSessionService,
    DeskActionResult,
    InvalidMessageError,
    InvalidSessionStateError,
    OperatorPermissionError,
    SessionNotFoundError,
    SessionOwnershipConflictError,
    SupportDeskService,
    format_off_hours_notice,
    get_working_hours_label,
    is_within_working_hours,
)
from support.views import OPERATOR_HISTORY_PREFIX, _get_history, _save_message

User = get_user_model()
KYIV_TZ = ZoneInfo('Europe/Kyiv')


class WorkingHoursAndSettingsTest(SimpleTestCase):
    def test_default_working_hours_settings_and_constants_hygiene(self):
        self.assertEqual(config_constants.DEFAULT_SUPPORT_WORK_START_HOUR, 10)
        self.assertEqual(config_constants.DEFAULT_SUPPORT_WORK_END_HOUR, 22)
        self.assertFalse(hasattr(config_constants, 'SUPPORT_WORK_START_HOUR'))
        self.assertFalse(hasattr(config_constants, 'SUPPORT_WORK_END_HOUR'))
        self.assertEqual(settings.SUPPORT_WORK_START_HOUR, 10)
        self.assertEqual(settings.SUPPORT_WORK_END_HOUR, 22)
        self.assertEqual(get_working_hours_label(), '10:00–22:00')

    def test_is_within_working_hours_kyiv_boundaries_summer_and_winter(self):
        # Summer in Europe/Kyiv (UTC+3)
        self.assertFalse(is_within_working_hours(datetime(2026, 7, 15, 9, 59, tzinfo=KYIV_TZ)))
        self.assertTrue(is_within_working_hours(datetime(2026, 7, 15, 10, 0, tzinfo=KYIV_TZ)))
        self.assertTrue(is_within_working_hours(datetime(2026, 7, 15, 21, 59, tzinfo=KYIV_TZ)))
        self.assertFalse(is_within_working_hours(datetime(2026, 7, 15, 22, 0, tzinfo=KYIV_TZ)))

        # Equivalent UTC timestamps in summer (UTC+3)
        self.assertFalse(is_within_working_hours(datetime(2026, 7, 15, 6, 59, tzinfo=UTC)))
        self.assertTrue(is_within_working_hours(datetime(2026, 7, 15, 7, 0, tzinfo=UTC)))
        self.assertTrue(is_within_working_hours(datetime(2026, 7, 15, 18, 59, tzinfo=UTC)))
        self.assertFalse(is_within_working_hours(datetime(2026, 7, 15, 19, 0, tzinfo=UTC)))

        # Winter in Europe/Kyiv (UTC+2)
        self.assertFalse(is_within_working_hours(datetime(2026, 1, 15, 7, 59, tzinfo=UTC)))
        self.assertTrue(is_within_working_hours(datetime(2026, 1, 15, 8, 0, tzinfo=UTC)))
        self.assertTrue(is_within_working_hours(datetime(2026, 1, 15, 19, 59, tzinfo=UTC)))
        self.assertFalse(is_within_working_hours(datetime(2026, 1, 15, 20, 0, tzinfo=UTC)))

        # Naive datetime fallback treated as Europe/Kyiv local time
        self.assertFalse(is_within_working_hours(datetime(2026, 7, 15, 9, 59)))
        self.assertTrue(is_within_working_hours(datetime(2026, 7, 15, 10, 0)))

    @override_settings(SUPPORT_WORK_START_HOUR=9, SUPPORT_WORK_END_HOUR=21)
    def test_working_hours_respects_overridden_settings(self):
        self.assertEqual(get_working_hours_label(), '09:00–21:00')
        self.assertTrue(is_within_working_hours(datetime(2026, 7, 15, 9, 0, tzinfo=KYIV_TZ)))
        self.assertFalse(is_within_working_hours(datetime(2026, 7, 15, 8, 59, tzinfo=KYIV_TZ)))
        self.assertTrue(is_within_working_hours(datetime(2026, 7, 15, 20, 59, tzinfo=KYIV_TZ)))
        self.assertFalse(is_within_working_hours(datetime(2026, 7, 15, 21, 0, tzinfo=KYIV_TZ)))

        notice = format_off_hours_notice()
        self.assertIn('09:00–21:00', notice)
        self.assertIn('з 09:00', notice)

    def test_invalid_working_hours_env_raises_improperly_configured(self):
        invalid_cases = [
            {'SUPPORT_WORK_START_HOUR': '-1', 'SUPPORT_WORK_END_HOUR': '22'},
            {'SUPPORT_WORK_START_HOUR': '24', 'SUPPORT_WORK_END_HOUR': '24'},
            {'SUPPORT_WORK_START_HOUR': '10', 'SUPPORT_WORK_END_HOUR': '25'},
            {'SUPPORT_WORK_START_HOUR': '22', 'SUPPORT_WORK_END_HOUR': '10'},
            {'SUPPORT_WORK_START_HOUR': '12', 'SUPPORT_WORK_END_HOUR': '12'},
            {'SUPPORT_WORK_START_HOUR': 'abc', 'SUPPORT_WORK_END_HOUR': '22'},
        ]
        base_env = os.environ.copy()
        base_env.update({
            'DEBUG': 'True',
            'DJANGO_TESTING': '1',
            'DB_ENGINE': 'django.db.backends.sqlite3',
        })
        for overrides in invalid_cases:
            env = base_env.copy()
            env.update(overrides)
            proc = subprocess.run(
                [sys.executable, '-c', 'import config.settings'],
                cwd=str(settings.BASE_DIR),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0, overrides)
            self.assertIn('ImproperlyConfigured', proc.stderr)

    def test_is_within_working_hours_dst_transitions_and_24_7_env(self):
        # Spring-forward DST Sunday in Europe/Kyiv (2026-03-29: UTC+2 -> UTC+3)
        self.assertFalse(is_within_working_hours(datetime(2026, 3, 29, 6, 59, tzinfo=UTC)))
        self.assertTrue(is_within_working_hours(datetime(2026, 3, 29, 7, 0, tzinfo=UTC)))
        self.assertTrue(is_within_working_hours(datetime(2026, 3, 29, 18, 59, tzinfo=UTC)))
        self.assertFalse(is_within_working_hours(datetime(2026, 3, 29, 19, 0, tzinfo=UTC)))

        # Fall-back DST Sunday in Europe/Kyiv (2026-10-25: UTC+3 -> UTC+2)
        self.assertFalse(is_within_working_hours(datetime(2026, 10, 25, 7, 59, tzinfo=UTC)))
        self.assertTrue(is_within_working_hours(datetime(2026, 10, 25, 8, 0, tzinfo=UTC)))
        self.assertTrue(is_within_working_hours(datetime(2026, 10, 25, 19, 59, tzinfo=UTC)))
        self.assertFalse(is_within_working_hours(datetime(2026, 10, 25, 20, 0, tzinfo=UTC)))

        # 24/7 operation via env (0..24) loads cleanly and is always within working hours
        env = os.environ.copy()
        env.update({
            'DEBUG': 'True',
            'DJANGO_TESTING': '1',
            'DB_ENGINE': 'django.db.backends.sqlite3',
            'SUPPORT_WORK_START_HOUR': '0',
            'SUPPORT_WORK_END_HOUR': '24',
        })
        proc = subprocess.run(
            [
                sys.executable,
                '-c',
                (
                    'import os, django; '
                    'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings"); '
                    'django.setup(); '
                    'from datetime import datetime; '
                    'from zoneinfo import ZoneInfo; '
                    'from support.services import get_working_hours_label, is_within_working_hours; '
                    'tz = ZoneInfo("Europe/Kyiv"); '
                    'assert get_working_hours_label() == "00:00–24:00"; '
                    'assert is_within_working_hours(datetime(2026, 7, 15, 0, 0, tzinfo=tz)); '
                    'assert is_within_working_hours(datetime(2026, 7, 15, 23, 59, tzinfo=tz)); '
                    'print("OK")'
                ),
            ],
            cwd=str(settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), 'OK')


class SupportPermissionsTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.customer = User.objects.create_user(
            email='perm_customer@example.com',
            password='password123',
            role=Role.CUSTOMER,
        )
        self.staff_customer = User.objects.create_user(
            email='perm_staff_customer@example.com',
            password='password123',
            role=Role.CUSTOMER,
            is_staff=True,
        )
        self.courier = User.objects.create_user(
            email='perm_courier@example.com',
            password='password123',
            role=Role.COURIER,
        )
        self.manager = User.objects.create_user(
            email='perm_manager@example.com',
            password='password123',
            role=Role.RESTAURANT_ADMIN,
        )
        self.inactive_manager = User.objects.create_user(
            email='perm_inactive_manager@example.com',
            password='password123',
            role=Role.RESTAURANT_ADMIN,
            is_active=False,
        )
        self.superuser = User.objects.create_superuser(
            email='perm_superuser@example.com',
            password='password123',
            role=Role.CUSTOMER,
        )
        self.inactive_superuser = User.objects.create_superuser(
            email='perm_inactive_super@example.com',
            password='password123',
            is_active=False,
        )

    def test_is_support_operator_matrix(self):
        self.assertFalse(is_support_operator(None))
        self.assertFalse(is_support_operator(AnonymousUser()))
        self.assertFalse(is_support_operator(self.customer))
        self.assertFalse(is_support_operator(self.staff_customer))
        self.assertFalse(is_support_operator(self.courier))
        self.assertFalse(is_support_operator(self.inactive_manager))
        self.assertFalse(is_support_operator(self.inactive_superuser))
        self.assertTrue(is_support_operator(self.manager))
        self.assertTrue(is_support_operator(self.superuser))

    def test_support_operator_required_sync_view(self):
        @support_operator_required
        def dummy_view(request):
            return HttpResponse('ok')

        req_anon = self.factory.get('/support/desk/')
        req_anon.user = AnonymousUser()
        resp_anon = dummy_view(req_anon)
        self.assertEqual(resp_anon.status_code, 302)
        self.assertIn('/accounts/login/', resp_anon['Location'])
        self.assertIn('next=/support/desk/', resp_anon['Location'])

        for forbidden_user in (
            self.customer,
            self.staff_customer,
            self.courier,
            self.inactive_manager,
        ):
            req = self.factory.get('/support/desk/')
            req.user = forbidden_user
            resp = dummy_view(req)
            self.assertEqual(resp.status_code, 403)

        for allowed_user in (self.manager, self.superuser):
            req = self.factory.get('/support/desk/')
            req.user = allowed_user
            resp = dummy_view(req)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.content, b'ok')

    async def test_support_operator_required_async_view_with_and_without_auser(self):
        @support_operator_required
        async def async_dummy_view(request):
            return HttpResponse('async-ok')

        # Without request.auser (RequestFactory pattern)
        req_anon = self.factory.get('/support/desk/events/')
        req_anon.user = AnonymousUser()
        resp_anon = await async_dummy_view(req_anon)
        self.assertEqual(resp_anon.status_code, 302)
        self.assertIn('/accounts/login/', resp_anon['Location'])

        req_customer = self.factory.get('/support/desk/events/')
        req_customer.user = self.customer
        resp_customer = await async_dummy_view(req_customer)
        self.assertEqual(resp_customer.status_code, 403)

        req_mgr = self.factory.get('/support/desk/events/')
        req_mgr.user = self.manager
        resp_mgr = await async_dummy_view(req_mgr)
        self.assertEqual(resp_mgr.status_code, 200)
        self.assertEqual(resp_mgr.content, b'async-ok')

        # With request.auser (ASGI AuthenticationMiddleware pattern)
        req_asgi = self.factory.get('/support/desk/events/')

        async def _auser():
            return self.superuser

        req_asgi.auser = _auser
        resp_asgi = await async_dummy_view(req_asgi)
        self.assertEqual(resp_asgi.status_code, 200)
        self.assertEqual(resp_asgi.content, b'async-ok')

    async def test_support_operator_required_async_view_evaluates_simple_lazy_user_safely(self):
        @support_operator_required
        async def async_dummy_view(request):
            return HttpResponse('lazy-ok')

        # DB-backed SimpleLazyObject without request.auser must not raise SynchronousOnlyOperation
        req_mgr = self.factory.get('/support/desk/events/')
        req_mgr.user = SimpleLazyObject(lambda: User.objects.get(pk=self.manager.pk))
        resp_mgr = await async_dummy_view(req_mgr)
        self.assertEqual(resp_mgr.status_code, 200)
        self.assertEqual(resp_mgr.content, b'lazy-ok')

        req_cust = self.factory.get('/support/desk/events/')
        req_cust.user = SimpleLazyObject(lambda: User.objects.get(pk=self.customer.pk))
        resp_cust = await async_dummy_view(req_cust)
        self.assertEqual(resp_cust.status_code, 403)

        req_anon = self.factory.get('/support/desk/events/')
        req_anon.user = SimpleLazyObject(AnonymousUser)
        resp_anon = await async_dummy_view(req_anon)
        self.assertEqual(resp_anon.status_code, 302)
        self.assertIn('/accounts/login/', resp_anon['Location'])


class SupportDeskServiceTest(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.customer = User.objects.create_user(
            email='desk_customer@example.com',
            password='password123',
            first_name='Іван',
            last_name='Петренко',
            phone='+380501234567',
            role=Role.CUSTOMER,
        )
        self.courier = User.objects.create_user(
            email='desk_courier@example.com',
            password='password123',
            role=Role.COURIER,
        )
        self.operator_a = User.objects.create_user(
            email='op_a@example.com',
            password='password123',
            first_name='Олена',
            last_name='Коваль',
            role=Role.RESTAURANT_ADMIN,
        )
        self.operator_b = User.objects.create_user(
            email='op_b@example.com',
            password='password123',
            first_name='Максим',
            last_name='Бондар',
            role=Role.RESTAURANT_ADMIN,
        )
        self.superuser = User.objects.create_superuser(
            email='desk_super@example.com',
            password='password123',
            first_name='Тарас',
            last_name='Шевченко',
        )

    def _make_auth_request(self, user, session_data=None):
        req = self.factory.post('/support/chat/reset/')
        req.user = user
        store = SessionStore()
        store.create()
        if session_data:
            store.update(session_data)
            store.save()
        req.session = store
        return req

    def _make_guest_request(self, guest_token: str):
        req = self.factory.post('/support/chat/reset/')
        req.user = AnonymousUser()
        store = SessionStore()
        store.create()
        store[GUEST_TOKEN_SESSION_KEY] = guest_token
        store.save()
        req.session = store
        return req

    def test_escalate_session_creates_system_message_in_and_out_of_working_hours(self):
        session_day = ChatSession.objects.create(user=self.customer)
        day_time = datetime(2026, 7, 15, 14, 30, tzinfo=KYIV_TZ)
        ticket_day = ChatSessionService.escalate_session(
            session=session_day,
            reason=TicketReason.HUMAN_REQUESTED,
            details='Потрібна допомога менеджера',
            now=day_time,
        )

        session_day.refresh_from_db()
        self.assertEqual(session_day.status, ChatSessionStatus.ESCALATED)
        self.assertTrue(session_day.is_escalated)
        self.assertIsNone(session_day.assigned_operator)
        self.assertIsNone(session_day.assigned_at)
        self.assertIsNotNone(session_day.last_message_at)

        sys_msgs_day = list(session_day.messages.filter(role=MessageRole.SYSTEM))
        self.assertEqual(len(sys_msgs_day), 1)
        self.assertEqual(sys_msgs_day[0].source, MessageSource.SYSTEM)
        self.assertEqual(
            sys_msgs_day[0].content,
            f'Звернення #{ticket_day.id} передано адміністратору. Ви можете дописати деталі прямо тут.',
        )
        self.assertNotIn('неробочий час', sys_msgs_day[0].content)
        self.assertEqual(session_day.last_message_at, sys_msgs_day[0].created_at)

        # Repeated escalation while ticket open is idempotent and adds no second system message
        same_ticket = ChatSessionService.escalate_session(
            session=session_day,
            reason=TicketReason.COMPLAINT,
            details='Повторний виклик',
            now=day_time,
        )
        self.assertEqual(same_ticket.id, ticket_day.id)
        self.assertEqual(session_day.messages.filter(role=MessageRole.SYSTEM).count(), 1)

        # Off-hours escalation appends off-hours notice
        session_night = ChatSession.objects.create(guest_token='night-guest-token')
        night_time = datetime(2026, 7, 15, 22, 0, tzinfo=KYIV_TZ)
        ticket_night = ChatSessionService.escalate_session(
            session=session_night,
            reason=TicketReason.COMPLAINT,
            details='Замовлення запізнюється ввечері',
            now=night_time,
        )
        sys_msg_night = session_night.messages.get(role=MessageRole.SYSTEM)
        self.assertIn(f'Звернення #{ticket_night.id} передано адміністратору.', sys_msg_night.content)
        self.assertIn(format_off_hours_notice(), sys_msg_night.content)

    def test_escalate_resolved_session_raises_invalid_state(self):
        resolved = ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.RESOLVED,
        )
        with self.assertRaises(InvalidSessionStateError):
            ChatSessionService.escalate_session(
                session=resolved,
                reason=TicketReason.HUMAN_REQUESTED,
                details='Спроба ескалювати закритий чат',
            )

    def test_reset_active_session_closes_open_and_in_progress_tickets_for_user_and_guest(self):
        # Authenticated user with IN_PROGRESS ticket assigned to operator
        req_auth = self._make_auth_request(self.customer)
        old_session = ChatSessionService.get_or_create_active_session(req_auth)
        ticket = ChatSessionService.escalate_session(
            session=old_session,
            reason=TicketReason.ORDER_ISSUE,
            details='Питання по замовленню',
        )
        SupportDeskService.claim_session(old_session, self.operator_a)

        new_session = ChatSessionService.reset_active_session(req_auth)
        self.assertNotEqual(new_session.session_uuid, old_session.session_uuid)
        self.assertEqual(new_session.status, ChatSessionStatus.ACTIVE)

        old_session.refresh_from_db()
        ticket.refresh_from_db()
        self.assertEqual(old_session.status, ChatSessionStatus.RESOLVED)
        self.assertFalse(old_session.is_escalated)
        self.assertIsNone(old_session.assigned_operator)
        self.assertIsNone(old_session.assigned_at)
        self.assertEqual(ticket.status, TicketStatus.RESOLVED)
        self.assertIsNotNone(ticket.resolved_at)
        # Ticket keeps assigned_operator for historical audit
        self.assertEqual(ticket.assigned_operator, self.operator_a)

        # Guest session with OPEN ticket
        req_guest = self._make_guest_request('reset-guest-tok-1')
        guest_session = ChatSessionService.get_or_create_active_session(req_guest)
        guest_ticket = ChatSessionService.escalate_session(
            session=guest_session,
            reason=TicketReason.HUMAN_REQUESTED,
            details='Гість кличе оператора',
        )
        new_guest_session = ChatSessionService.reset_active_session(req_guest)
        self.assertNotEqual(new_guest_session.session_uuid, guest_session.session_uuid)
        guest_session.refresh_from_db()
        guest_ticket.refresh_from_db()
        self.assertEqual(guest_session.status, ChatSessionStatus.RESOLVED)
        self.assertEqual(guest_ticket.status, TicketStatus.RESOLVED)
        self.assertIsNotNone(guest_ticket.resolved_at)

    def test_reset_active_session_closes_unlinked_pre_login_guest_ticket(self):
        pre_tok = 'pre-login-escalated-tok'
        guest_session = ChatSession.objects.create(
            user=None,
            guest_token=pre_tok,
            status=ChatSessionStatus.ACTIVE,
        )
        guest_ticket = ChatSessionService.escalate_session(
            session=guest_session,
            reason=TicketReason.COMPLAINT,
            details='Скарга до входу',
        )
        req_after_login = self._make_auth_request(
            self.customer,
            session_data={PRE_LOGIN_GUEST_TOKEN_SESSION_KEY: pre_tok},
        )
        new_session = ChatSessionService.reset_active_session(req_after_login)
        self.assertEqual(new_session.user, self.customer)
        self.assertEqual(new_session.status, ChatSessionStatus.ACTIVE)

        guest_session.refresh_from_db()
        guest_ticket.refresh_from_db()
        self.assertEqual(guest_session.status, ChatSessionStatus.RESOLVED)
        self.assertEqual(guest_ticket.status, TicketStatus.RESOLVED)
        self.assertIsNotNone(guest_ticket.resolved_at)

    def test_claim_session_happy_path_idempotency_conflict_and_errors(self):
        session = ChatSession.objects.create(user=self.customer)
        ticket = ChatSessionService.escalate_session(
            session=session,
            reason=TicketReason.COMPLAINT,
            details='Холодна піца',
        )

        # Non-operator forbidden
        with self.assertRaises(OperatorPermissionError):
            SupportDeskService.claim_session(session, self.customer)
        with self.assertRaises(OperatorPermissionError):
            SupportDeskService.claim_session(session, self.courier)

        # Happy claim by operator A
        result = SupportDeskService.claim_session(session, self.operator_a)
        self.assertIsInstance(result, DeskActionResult)
        self.assertEqual(result.session.assigned_operator, self.operator_a)
        self.assertIsNotNone(result.session.assigned_at)
        self.assertEqual(result.ticket.id, ticket.id)
        self.assertEqual(result.ticket.status, TicketStatus.IN_PROGRESS)
        self.assertEqual(result.ticket.assigned_operator, self.operator_a)
        self.assertIsNotNone(result.system_message)
        self.assertEqual(
            result.system_message.content,
            'До діалогу підключився адміністратор ресторану.',
        )
        self.assertEqual(session.assigned_operator, self.operator_a)

        # Same operator re-claiming is idempotent (no duplicate system message)
        msg_count_before = session.messages.count()
        reclaim = SupportDeskService.claim_session(session, self.operator_a)
        self.assertIsNone(reclaim.system_message)
        self.assertEqual(session.messages.count(), msg_count_before)

        # Different operator gets SessionOwnershipConflictError with owner
        with self.assertRaises(SessionOwnershipConflictError) as ctx:
            SupportDeskService.claim_session(session, self.operator_b)
        self.assertEqual(ctx.exception.owner, self.operator_a)
        self.assertIn('Олена Коваль', str(ctx.exception))

        # Active and resolved sessions cannot be claimed
        active_session = ChatSession.objects.create(guest_token='active-no-claim')
        with self.assertRaises(InvalidSessionStateError):
            SupportDeskService.claim_session(active_session, self.operator_a)

        resolved_session = ChatSession.objects.create(
            guest_token='resolved-no-claim',
            status=ChatSessionStatus.RESOLVED,
        )
        with self.assertRaises(InvalidSessionStateError):
            SupportDeskService.claim_session(resolved_session, self.operator_a)

        # Nonexistent session raises SessionNotFoundError
        with self.assertRaises(SessionNotFoundError):
            SupportDeskService.claim_session(uuid.uuid4(), self.operator_a)
        with self.assertRaises(SessionNotFoundError):
            SupportDeskService.claim_session('not-a-uuid', self.operator_a)

    def test_proactive_takeover_from_active_ai_session(self):
        session = ChatSession.objects.create(user=self.customer)
        result = SupportDeskService.proactive_takeover(session, self.operator_a)

        session.refresh_from_db()
        self.assertEqual(session.status, ChatSessionStatus.ESCALATED)
        self.assertTrue(session.is_escalated)
        self.assertEqual(session.assigned_operator, self.operator_a)
        self.assertIsNotNone(session.assigned_at)
        self.assertEqual(session.last_message_at, result.system_message.created_at)

        self.assertIsNotNone(result.ticket)
        self.assertEqual(result.ticket.reason, TicketReason.OPERATOR_TAKEOVER)
        self.assertEqual(result.ticket.status, TicketStatus.IN_PROGRESS)
        self.assertEqual(result.ticket.assigned_operator, self.operator_a)
        self.assertEqual(result.ticket.customer_phone, '+380501234567')
        self.assertEqual(
            result.system_message.content,
            'До діалогу підключився адміністратор ресторану.',
        )

        # Calling proactive_takeover again on already ESCALATED session fails
        with self.assertRaises(InvalidSessionStateError):
            SupportDeskService.proactive_takeover(session, self.operator_b)

    def test_takeover_session_from_colleague_and_proactive_flag(self):
        # Active AI session rejected by takeover_session unless allow_proactive=True
        ai_session = ChatSession.objects.create(guest_token='ai-takeover-tok')
        with self.assertRaises(InvalidSessionStateError):
            SupportDeskService.takeover_session(ai_session, self.operator_a)

        proactive_res = SupportDeskService.takeover_session(
            ai_session,
            self.operator_a,
            allow_proactive=True,
        )
        self.assertEqual(proactive_res.session.status, ChatSessionStatus.ESCALATED)
        self.assertEqual(proactive_res.session.assigned_operator, self.operator_a)
        self.assertEqual(proactive_res.ticket.reason, TicketReason.OPERATOR_TAKEOVER)

        # Operator B takes over from Operator A
        takeover_res = SupportDeskService.takeover_session(ai_session, self.operator_b)
        ai_session.refresh_from_db()
        proactive_res.ticket.refresh_from_db()
        self.assertEqual(ai_session.assigned_operator, self.operator_b)
        self.assertEqual(proactive_res.ticket.assigned_operator, self.operator_b)
        self.assertEqual(proactive_res.ticket.status, TicketStatus.IN_PROGRESS)
        self.assertEqual(
            takeover_res.system_message.content,
            'Діалог продовжить інший адміністратор ресторану.',
        )

        # Operator B taking over their own session is idempotent
        msg_count = ai_session.messages.count()
        idem_res = SupportDeskService.takeover_session(ai_session, self.operator_b)
        self.assertIsNone(idem_res.system_message)
        self.assertEqual(ai_session.messages.count(), msg_count)

        # Takeover of unassigned ESCALATED session uses joined text
        unassigned = ChatSession.objects.create(guest_token='unassigned-takeover-tok')
        ChatSessionService.escalate_session(
            unassigned,
            TicketReason.HUMAN_REQUESTED,
            'Чекаю оператора',
        )
        unassigned_res = SupportDeskService.takeover_session(unassigned, self.superuser)
        self.assertEqual(unassigned_res.session.assigned_operator, self.superuser)
        self.assertEqual(
            unassigned_res.system_message.content,
            'До діалогу підключився адміністратор ресторану.',
        )

    def test_send_customer_message_validation_and_state_guards(self):
        session = ChatSession.objects.create(user=self.customer)
        # Blocked in ACTIVE session
        with self.assertRaises(InvalidSessionStateError):
            SupportDeskService.send_customer_message(session, 'Привіт')

        ChatSessionService.escalate_session(
            session, TicketReason.HUMAN_REQUESTED, 'Хочу поговорити з менеджером'
        )
        # Validation errors
        for bad_content in (None, '', '   \n\t  ', 'x' * (MAX_SUPPORT_MESSAGE_LENGTH + 1)):
            with self.assertRaises(InvalidMessageError):
                SupportDeskService.send_customer_message(session, bad_content)

        # Exact max length boundary succeeds
        max_text = 'А' * MAX_SUPPORT_MESSAGE_LENGTH
        msg = SupportDeskService.send_customer_message(session, f'  {max_text}  ')
        self.assertEqual(msg.role, MessageRole.USER)
        self.assertEqual(msg.source, MessageSource.WEB)
        self.assertEqual(msg.content, max_text)
        session.refresh_from_db()
        self.assertEqual(session.last_message_at, msg.created_at)

        # Blocked after resolve
        SupportDeskService.resolve_session(session, self.operator_a)
        with self.assertRaises(InvalidSessionStateError):
            SupportDeskService.send_customer_message(session, 'Ще питання')

    def test_send_operator_message_ownership_validation_and_telegram_auto_claim(self):
        session = ChatSession.objects.create(user=self.customer)
        with self.assertRaises(InvalidSessionStateError):
            SupportDeskService.send_operator_message(session, self.operator_a, 'Привіт')

        ticket = ChatSessionService.escalate_session(
            session, TicketReason.ORDER_ISSUE, 'Немає соусу'
        )

        # Web reply requires session to be claimed first
        with self.assertRaises(SessionOwnershipConflictError) as unclaimed_ctx:
            SupportDeskService.send_operator_message(session, self.operator_a, 'Відповідь')
        self.assertIsNone(unclaimed_ctx.exception.owner)

        # Claim by operator A -> A can reply, B gets conflict
        SupportDeskService.claim_session(session, self.operator_a)
        with self.assertRaises(SessionOwnershipConflictError) as other_ctx:
            SupportDeskService.send_operator_message(session, self.operator_b, 'Втручаюсь')
        self.assertEqual(other_ctx.exception.owner, self.operator_a)

        # Validation errors
        with self.assertRaises(InvalidMessageError):
            SupportDeskService.send_operator_message(session, self.operator_a, '   ')
        with self.assertRaises(InvalidMessageError):
            SupportDeskService.send_operator_message(
                session, self.operator_a, 'B' * (MAX_SUPPORT_MESSAGE_LENGTH + 1)
            )
        with self.assertRaises(OperatorPermissionError):
            SupportDeskService.send_operator_message(session, self.customer, 'Я не адмін')

        # Valid reply by owner
        reply = SupportDeskService.send_operator_message(
            session,
            self.operator_a,
            '  Вже передали кухні, дошлемо соус!  ',
        )
        self.assertEqual(reply.role, MessageRole.OPERATOR)
        self.assertEqual(reply.sender_user, self.operator_a)
        self.assertEqual(reply.source, MessageSource.WEB)
        self.assertEqual(reply.content, 'Вже передали кухні, дошлемо соус!')
        session.refresh_from_db()
        self.assertEqual(session.last_message_at, reply.created_at)
        self.assertEqual(session.operator_last_read_id, reply.id)

        # Telegram first reply auto-transitions OPEN ticket -> IN_PROGRESS
        tg_session = ChatSession.objects.create(guest_token='tg-auto-claim-tok')
        tg_ticket = ChatSessionService.escalate_session(
            tg_session, TicketReason.HUMAN_REQUESTED, 'Запит для TG'
        )
        self.assertEqual(tg_ticket.status, TicketStatus.OPEN)

        tg_msg_1 = SupportDeskService.send_operator_message(
            tg_session,
            None,
            'Добрий день, відповідаємо з Telegram!',
            source=MessageSource.TELEGRAM,
        )
        tg_ticket.refresh_from_db()
        self.assertEqual(tg_ticket.status, TicketStatus.IN_PROGRESS)
        self.assertEqual(tg_msg_1.source, MessageSource.TELEGRAM)
        self.assertIsNone(tg_msg_1.sender_user)
        joined_count = tg_session.messages.filter(
            role=MessageRole.SYSTEM,
            content='До діалогу підключився адміністратор ресторану.',
        ).count()
        self.assertEqual(joined_count, 1)

        # Second Telegram reply from linked operator assigns both session and ticket
        # without duplicating the joined system message
        tg_msg_2 = SupportDeskService.send_operator_message(
            tg_session,
            self.operator_b,
            'Друге повідомлення з Telegram',
            source=MessageSource.TELEGRAM,
        )
        tg_session.refresh_from_db()
        tg_ticket.refresh_from_db()
        self.assertEqual(tg_msg_2.sender_user, self.operator_b)
        self.assertEqual(tg_session.assigned_operator, self.operator_b)
        self.assertEqual(tg_ticket.assigned_operator, self.operator_b)
        self.assertEqual(
            tg_session.messages.filter(
                role=MessageRole.SYSTEM,
                content='До діалогу підключився адміністратор ресторану.',
            ).count(),
            1,
        )
        self.assertEqual(ticket.session_id, session.session_uuid)

    def test_return_to_ai_resolves_ticket_and_allows_clean_re_escalation(self):
        session = ChatSession.objects.create(user=self.customer)
        ticket_1 = ChatSessionService.escalate_session(
            session, TicketReason.COMPLAINT, 'Перша проблема'
        )
        SupportDeskService.claim_session(session, self.operator_a)
        SupportDeskService.send_operator_message(
            session, self.operator_a, 'Питання вирішено, дарую промокод!'
        )

        # Non-operator cannot return to AI
        with self.assertRaises(OperatorPermissionError):
            SupportDeskService.return_to_ai(session, self.customer)

        res = SupportDeskService.return_to_ai(session, self.operator_a)
        session.refresh_from_db()
        ticket_1.refresh_from_db()

        self.assertEqual(session.status, ChatSessionStatus.ACTIVE)
        self.assertFalse(session.is_escalated)
        self.assertIsNone(session.assigned_operator)
        self.assertIsNone(session.assigned_at)
        self.assertEqual(ticket_1.status, TicketStatus.RESOLVED)
        self.assertIsNotNone(ticket_1.resolved_at)
        self.assertEqual(ticket_1.assigned_operator, self.operator_a)
        self.assertEqual(
            res.system_message.content,
            'Адміністратор завершив діалог. Далі вам допомагає AI-консьєрж Double Bite.',
        )

        # Returning an already ACTIVE session raises InvalidSessionStateError
        with self.assertRaises(InvalidSessionStateError):
            SupportDeskService.return_to_ai(session, self.operator_a)

        # Cooldown was cleared on return_to_ai, so customer can re-escalate immediately
        ticket_2 = ChatSessionService.escalate_session(
            session, TicketReason.HUMAN_REQUESTED, 'Ще одне питання до менеджера'
        )
        self.assertNotEqual(ticket_2.id, ticket_1.id)
        self.assertEqual(ticket_2.status, TicketStatus.OPEN)
        session.refresh_from_db()
        self.assertEqual(session.status, ChatSessionStatus.ESCALATED)

    def test_resolve_session_from_escalated_and_active_states(self):
        esc_session = ChatSession.objects.create(user=self.customer)
        ticket = ChatSessionService.escalate_session(
            esc_session, TicketReason.ORDER_ISSUE, 'Змінити адресу'
        )
        SupportDeskService.claim_session(esc_session, self.operator_a)

        res = SupportDeskService.resolve_session(esc_session, self.operator_a)
        esc_session.refresh_from_db()
        ticket.refresh_from_db()
        self.assertEqual(esc_session.status, ChatSessionStatus.RESOLVED)
        self.assertFalse(esc_session.is_escalated)
        self.assertIsNone(esc_session.assigned_operator)
        self.assertIsNone(esc_session.assigned_at)
        self.assertEqual(ticket.status, TicketStatus.RESOLVED)
        self.assertIsNotNone(ticket.resolved_at)
        self.assertEqual(ticket.assigned_operator, self.operator_a)
        self.assertEqual(
            res.system_message.content,
            'Звернення завершено. Якщо виникнуть нові запитання — натисніть «Новий діалог».',
        )

        # Double resolve rejected
        with self.assertRaises(InvalidSessionStateError):
            SupportDeskService.resolve_session(esc_session, self.operator_a)

        # Resolve directly from ACTIVE session works too
        active_session = ChatSession.objects.create(guest_token='resolve-active-tok')
        res_active = SupportDeskService.resolve_session(active_session, self.superuser)
        active_session.refresh_from_db()
        self.assertEqual(active_session.status, ChatSessionStatus.RESOLVED)
        self.assertIsNone(res_active.ticket)
        self.assertIsNotNone(res_active.system_message)

    def test_mark_session_read_monotonic_and_clamped(self):
        session = ChatSession.objects.create(user=self.customer)
        self.assertEqual(
            SupportDeskService.mark_session_read(session, self.operator_a),
            0,
        )

        m1 = ChatMessage.objects.create(
            session=session, role=MessageRole.USER, content='Перше'
        )
        m2 = ChatMessage.objects.create(
            session=session, role=MessageRole.USER, content='Друге'
        )
        m3 = ChatMessage.objects.create(
            session=session, role=MessageRole.USER, content='Третє'
        )
        original_last_msg_at = session.last_message_at

        # Partial read up to m2
        read_id = SupportDeskService.mark_session_read(session, self.operator_a, m2.id)
        self.assertEqual(read_id, m2.id)
        session.refresh_from_db()
        self.assertEqual(session.operator_last_read_id, m2.id)
        self.assertEqual(session.last_message_at, original_last_msg_at)

        # Never moves backwards
        self.assertEqual(
            SupportDeskService.mark_session_read(session, self.operator_a, m1.id),
            m2.id,
        )

        # Clamped to actual latest message id in session (cannot poison with future ID)
        self.assertEqual(
            SupportDeskService.mark_session_read(session, self.operator_a, m3.id + 99999),
            m3.id,
        )
        session.refresh_from_db()
        self.assertEqual(session.operator_last_read_id, m3.id)

        # Invalid values rejected
        with self.assertRaises(ValueError):
            SupportDeskService.mark_session_read(session, self.operator_a, -1)
        with self.assertRaises(ValueError):
            SupportDeskService.mark_session_read(session, self.operator_a, True)
        with self.assertRaises(OperatorPermissionError):
            SupportDeskService.mark_session_read(session, self.customer)

    def test_escalate_session_rollback_after_return_to_ai_does_not_return_resolved_ticket(self):
        session = ChatSession.objects.create(user=self.customer)
        ticket_1 = ChatSessionService.escalate_session(
            session, TicketReason.COMPLAINT, 'Перше звернення'
        )
        SupportDeskService.return_to_ai(session, self.operator_a)

        # Simulate rolled-back re-escalation transaction
        try:
            with transaction.atomic():
                ChatSessionService.escalate_session(
                    session, TicketReason.COMPLAINT, 'Відкочена спроба'
                )
                raise RuntimeError('Simulated DB rollback')
        except RuntimeError:
            pass

        session.refresh_from_db()
        self.assertEqual(session.status, ChatSessionStatus.ACTIVE)
        self.assertFalse(session.is_escalated)

        # Retry must create a fresh OPEN ticket and transition session to ESCALATED
        ticket_2 = ChatSessionService.escalate_session(
            session, TicketReason.HUMAN_REQUESTED, 'Повторне звернення'
        )
        self.assertNotEqual(ticket_2.id, ticket_1.id)
        self.assertEqual(ticket_2.status, TicketStatus.OPEN)
        session.refresh_from_db()
        self.assertEqual(session.status, ChatSessionStatus.ESCALATED)
        self.assertTrue(session.is_escalated)

        # Nonexistent session raises SessionNotFoundError consistently
        with self.assertRaises(SessionNotFoundError):
            ChatSessionService.escalate_session(
                uuid.uuid4(), TicketReason.HUMAN_REQUESTED, 'Неіснуюча сесія'
            )

        # Self-heals drifted ACTIVE session when an OPEN ticket already exists
        drifted = ChatSession.objects.create(guest_token='drifted-active-with-ticket')
        open_ticket = SupportTicket.objects.create(
            session=drifted,
            reason=TicketReason.ORDER_ISSUE,
            details='Існуючий відкритий тікет',
            status=TicketStatus.OPEN,
        )
        healed_ticket = ChatSessionService.escalate_session(
            drifted, TicketReason.HUMAN_REQUESTED, 'Повторний запит'
        )
        self.assertEqual(healed_ticket.id, open_ticket.id)
        drifted.refresh_from_db()
        self.assertEqual(drifted.status, ChatSessionStatus.ESCALATED)
        self.assertTrue(drifted.is_escalated)

    def test_escalate_to_operator_tool_returns_error_dict_on_resolved_session(self):
        resolved_session = ChatSession.objects.create(
            user=self.customer,
            status=ChatSessionStatus.RESOLVED,
        )
        result = execute_agent_tool(
            'escalate_to_operator',
            {'reason': TicketReason.COMPLAINT, 'details': 'Сесію вже закрито'},
            session=resolved_session,
        )
        self.assertIn('error', result)
        self.assertNotIn('ticket_id', result)
        self.assertEqual(SupportTicket.objects.filter(session=resolved_session).count(), 0)

    def test_message_validation_rejects_non_string_nul_invalid_sources_and_unsaved_operator(self):
        session = ChatSession.objects.create(user=self.customer)
        ChatSessionService.escalate_session(
            session, TicketReason.HUMAN_REQUESTED, 'Валідація'
        )

        # Unsaved operator User cannot bypass permission check to crash during save
        unsaved_op = User(
            email='unsaved_op@example.com',
            role=Role.RESTAURANT_ADMIN,
        )
        with self.assertRaises(OperatorPermissionError):
            SupportDeskService.claim_session(session, unsaved_op)

        SupportDeskService.claim_session(session, self.operator_a)

        # Non-string JSON payloads and NUL bytes rejected for customer & operator
        for invalid_payload in (['list'], {'dict': 1}, 12345, True, 'Привіт\x00світ'):
            with self.assertRaises(InvalidMessageError):
                SupportDeskService.send_customer_message(session, invalid_payload)
            with self.assertRaises(InvalidMessageError):
                SupportDeskService.send_operator_message(
                    session, self.operator_a, invalid_payload
                )

        # Role-appropriate MessageSource validation
        for bad_customer_source in (
            MessageSource.AI,
            MessageSource.SYSTEM,
            MessageSource.TELEGRAM,
            'UNKNOWN',
        ):
            with self.assertRaises(ValueError):
                SupportDeskService.send_customer_message(
                    session, 'Привіт', source=bad_customer_source
                )

        for bad_op_source in (MessageSource.AI, MessageSource.SYSTEM, 'UNKNOWN'):
            with self.assertRaises(ValueError):
                SupportDeskService.send_operator_message(
                    session, self.operator_a, 'Відповідь', source=bad_op_source
                )
            with self.assertRaises(ValueError):
                SupportDeskService.return_to_ai(
                    session, self.operator_a, source=bad_op_source
                )
            with self.assertRaises(ValueError):
                SupportDeskService.resolve_session(
                    session, self.operator_a, source=bad_op_source
                )


class OperatorHistoryAndPromptTest(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            email='hist_customer@example.com',
            password='password123',
            role=Role.CUSTOMER,
        )
        self.operator = User.objects.create_user(
            email='hist_operator@example.com',
            password='password123',
            role=Role.RESTAURANT_ADMIN,
        )

    def test_get_history_includes_operator_messages_with_llm_marker_and_excludes_tool_system(self):
        session = ChatSession.objects.create(user=self.customer)
        ChatMessage.objects.create(
            session=session, role=MessageRole.USER, content='Піца приїхала холодна'
        )
        ChatMessage.objects.create(
            session=session, role=MessageRole.TOOL, content='{"ticket_id": 1}'
        )
        ChatMessage.objects.create(
            session=session, role=MessageRole.SYSTEM, content='Звернення #1 передано адміністратору.'
        )
        ChatMessage.objects.create(
            session=session, role=MessageRole.ASSISTANT, content='Передаю адміністратору.'
        )
        ChatMessage.objects.create(
            session=session,
            role=MessageRole.OPERATOR,
            sender_user=self.operator,
            content='Ми вже готуємо нову гарячу піцу безкоштовно.',
        )
        ChatMessage.objects.create(
            session=session, role=MessageRole.USER, content='Дякую! А скільки чекати?'
        )

        history = _get_history(session.session_uuid)
        self.assertEqual(len(history), 4)
        self.assertEqual(
            [item['role'] for item in history],
            [
                MessageRole.USER,
                MessageRole.ASSISTANT,
                MessageRole.OPERATOR,
                MessageRole.USER,
            ],
        )
        self.assertEqual(
            history[2]['content'],
            f'{OPERATOR_HISTORY_PREFIX}Ми вже готуємо нову гарячу піцу безкоштовно.',
        )

    @patch('support.agent.client.GeminiClient')
    async def test_gemini_agent_receives_operator_context_mapped_to_assistant(self, mock_gemini_cls):
        from asgiref.sync import sync_to_async

        def setup_session():
            session = ChatSession.objects.create(user=self.customer)
            ChatMessage.objects.create(
                session=session, role=MessageRole.USER, content='Піца холодна'
            )
            ChatMessage.objects.create(
                session=session,
                role=MessageRole.OPERATOR,
                sender_user=self.operator,
                content='Відправили заміну курʼєром.',
            )
            ChatMessage.objects.create(
                session=session, role=MessageRole.USER, content='Коли буде заміна?'
            )
            return _get_history(session.session_uuid)

        history = await sync_to_async(setup_session, thread_sensitive=True)()

        captured_messages = []

        class _MockStream:
            def __aiter__(self):
                self._done = False
                return self

            async def __anext__(self):
                if self._done:
                    raise StopAsyncIteration
                self._done = True
                chunk = MagicMock()
                delta = MagicMock()
                delta.content = 'Заміна вже у дорозі!'
                delta.tool_calls = None
                choice = MagicMock()
                choice.delta = delta
                chunk.data = MagicMock(choices=[choice], usage=None)
                return chunk

            async def close(self):
                return None

        async def fake_stream_async(**kwargs):
            captured_messages.extend(kwargs['messages'])
            return _MockStream()

        mock_client = AsyncMock()
        mock_client.chat.stream_async.side_effect = fake_stream_async
        mock_gemini_cls.return_value.__aenter__.return_value = mock_client

        agent = GeminiSupportAgent(api_key='fake-key')
        events = [ev async for ev in agent.stream_chat_response(history)]
        self.assertEqual(events, [{'token': 'Заміна вже у дорозі!'}])

        # messages[0] is system prompt; messages[2] is operator reply mapped to assistant
        self.assertEqual(captured_messages[2]['role'], 'assistant')
        self.assertIn(OPERATOR_HISTORY_PREFIX, captured_messages[2]['content'])
        self.assertIn('Відправили заміну курʼєром.', captured_messages[2]['content'])

    @patch('support.agent.client.GeminiClient')
    async def test_gemini_agent_coalesces_consecutive_roles_and_drops_leading_assistant(
        self, mock_gemini_cls
    ):
        class _MockStream:
            def __aiter__(self):
                self._done = False
                return self

            async def __anext__(self):
                if self._done:
                    raise StopAsyncIteration
                self._done = True
                chunk = MagicMock()
                delta = MagicMock()
                delta.content = 'Все під контролем!'
                delta.tool_calls = None
                choice = MagicMock()
                choice.delta = delta
                chunk.data = MagicMock(choices=[choice], usage=None)
                return chunk

            async def close(self):
                return None

        captured_payloads = []

        async def fake_stream_async(**kwargs):
            captured_payloads.append(list(kwargs['messages']))
            return _MockStream()

        mock_client = AsyncMock()
        mock_client.chat.stream_async.side_effect = fake_stream_async
        mock_gemini_cls.return_value.__aenter__.return_value = mock_client

        agent = GeminiSupportAgent(api_key='fake-key')

        # 1) Realistic AI-escalation -> assistant reply -> 2 operator replies -> return_to_ai -> user
        history_after_handoff = [
            {'role': MessageRole.USER, 'content': 'Піца приїхала холодна'},
            {'role': MessageRole.ASSISTANT, 'content': 'Передаю адміністратору.'},
            {
                'role': MessageRole.OPERATOR,
                'content': f'{OPERATOR_HISTORY_PREFIX}Готуємо нову гарячу піцу.',
            },
            {
                'role': MessageRole.OPERATOR,
                'content': f'{OPERATOR_HISTORY_PREFIX}Додамо десерт у подарунок.',
            },
            {'role': MessageRole.USER, 'content': 'А коли приїде заміна?'},
        ]
        _ = [ev async for ev in agent.stream_chat_response(history_after_handoff)]
        payload_1 = captured_payloads[-1]
        self.assertEqual(
            [m['role'] for m in payload_1],
            ['system', 'user', 'assistant', 'user'],
        )
        self.assertIn('Передаю адміністратору.', payload_1[2]['content'])
        self.assertIn(f'{OPERATOR_HISTORY_PREFIX}Готуємо нову гарячу піцу.', payload_1[2]['content'])
        self.assertIn(f'{OPERATOR_HISTORY_PREFIX}Додамо десерт у подарунок.', payload_1[2]['content'])

        # 2) Leading assistant/operator from 20-message window slice + consecutive user messages
        history_sliced = [
            {'role': MessageRole.ASSISTANT, 'content': 'Стара обрізана репліка'},
            {'role': MessageRole.OPERATOR, 'content': f'{OPERATOR_HISTORY_PREFIX}Ще одна стара'},
            {'role': MessageRole.USER, 'content': 'Перше повідомлення клієнта'},
            {'role': MessageRole.USER, 'content': 'Уточнення: замовлення DB-1001'},
            {'role': MessageRole.OPERATOR, 'content': f'{OPERATOR_HISTORY_PREFIX}Вже перевіряю!'},
            {'role': MessageRole.USER, 'content': 'Дякую!'},
        ]
        _ = [ev async for ev in agent.stream_chat_response(history_sliced)]
        payload_2 = captured_payloads[-1]
        self.assertEqual(
            [m['role'] for m in payload_2],
            ['system', 'user', 'assistant', 'user'],
        )
        self.assertIn('Перше повідомлення клієнта', payload_2[1]['content'])
        self.assertIn('Уточнення: замовлення DB-1001', payload_2[1]['content'])
        self.assertIn('Вже перевіряю!', payload_2[2]['content'])

    def test_save_message_updates_source_and_last_message_at(self):
        session = ChatSession.objects.create(user=self.customer)
        self.assertIsNone(session.last_message_at)

        user_msg = _save_message(session.session_uuid, MessageRole.USER, 'Привіт')
        self.assertEqual(user_msg.source, MessageSource.WEB)
        session.refresh_from_db()
        self.assertEqual(session.last_message_at, user_msg.created_at)

        ai_msg = _save_message(session.session_uuid, MessageRole.ASSISTANT, 'Вітаю!')
        self.assertEqual(ai_msg.source, MessageSource.AI)
        session.refresh_from_db()
        self.assertEqual(session.last_message_at, ai_msg.created_at)

        tool_msg = _save_message(session.session_uuid, MessageRole.TOOL, '{"ok": true}')
        self.assertEqual(tool_msg.source, MessageSource.SYSTEM)

    def test_system_prompt_mentions_live_operator_in_same_chat_and_hotline(self):
        self.assertIn('прямо в цей чат', SYSTEM_PROMPT)
        self.assertIn('+380 44 123 45 67', SYSTEM_PROMPT)
        self.assertIn('escalate_to_operator', SYSTEM_PROMPT)


class SupportDeskConcurrencyAndMigrationTest(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.customer = User.objects.create_user(
            email='conc_customer@example.com',
            password='password123',
            role=Role.CUSTOMER,
        )
        self.operators = [
            User.objects.create_user(
                email=f'conc_op_{i}@example.com',
                password='password123',
                first_name=f'Оператор{i}',
                role=Role.RESTAURANT_ADMIN,
            )
            for i in range(6)
        ]

    def test_concurrent_claim_allows_single_winner_and_conflicts_others(self):
        session = ChatSession.objects.create(user=self.customer)
        ticket = ChatSessionService.escalate_session(
            session,
            reason=TicketReason.COMPLAINT,
            details='Одночасне взяття в роботу',
        )
        workers = len(self.operators)
        barrier = threading.Barrier(workers)

        def worker(op):
            try:
                barrier.wait(timeout=10)
                res = SupportDeskService.claim_session(session.session_uuid, op)
                return ('ok', op.id, res.session.assigned_operator_id)
            except SessionOwnershipConflictError as exc:
                return ('conflict', op.id, exc.owner.id if exc.owner else None)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(worker, self.operators))

        winners = [o for o in outcomes if o[0] == 'ok']
        conflicts = [o for o in outcomes if o[0] == 'conflict']

        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), workers - 1)
        winner_id = winners[0][1]
        self.assertTrue(all(c[2] == winner_id for c in conflicts))

        session.refresh_from_db()
        ticket.refresh_from_db()
        self.assertEqual(session.assigned_operator_id, winner_id)
        self.assertEqual(ticket.status, TicketStatus.IN_PROGRESS)
        self.assertEqual(ticket.assigned_operator_id, winner_id)
        joined_msgs = session.messages.filter(
            role=MessageRole.SYSTEM,
            content='До діалогу підключився адміністратор ресторану.',
        )
        self.assertEqual(joined_msgs.count(), 1)

    def test_concurrent_telegram_replies_on_open_ticket_emit_single_joined_notice(self):
        session = ChatSession.objects.create(user=self.customer)
        ticket = ChatSessionService.escalate_session(
            session,
            reason=TicketReason.HUMAN_REQUESTED,
            details='Одночасні відповіді з Telegram',
        )
        senders = [None, self.operators[0], None, self.operators[1]]
        barrier = threading.Barrier(len(senders))

        def tg_worker(idx_sender):
            idx, sender = idx_sender
            try:
                barrier.wait(timeout=10)
                msg = SupportDeskService.send_operator_message(
                    session.session_uuid,
                    sender,
                    f'TG відповідь #{idx}',
                    source=MessageSource.TELEGRAM,
                )
                return msg.id
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(senders)) as pool:
            msg_ids = list(pool.map(tg_worker, enumerate(senders)))

        self.assertEqual(len(set(msg_ids)), len(senders))
        ticket.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatus.IN_PROGRESS)
        self.assertIsNotNone(session.assigned_operator_id)
        self.assertEqual(ticket.assigned_operator_id, session.assigned_operator_id)
        self.assertEqual(
            session.messages.filter(
                role=MessageRole.OPERATOR,
                source=MessageSource.TELEGRAM,
            ).count(),
            len(senders),
        )
        self.assertEqual(
            session.messages.filter(
                role=MessageRole.SYSTEM,
                content='До діалогу підключився адміністратор ресторану.',
            ).count(),
            1,
        )

    def test_concurrent_proactive_takeover_and_escalate_create_single_ticket(self):
        session = ChatSession.objects.create(user=self.customer)
        op = self.operators[0]
        barrier = threading.Barrier(2)

        def run_takeover():
            try:
                barrier.wait(timeout=10)
                return SupportDeskService.takeover_session(
                    session.session_uuid,
                    op,
                    allow_proactive=True,
                )
            finally:
                connections.close_all()

        def run_escalate():
            try:
                barrier.wait(timeout=10)
                return ChatSessionService.escalate_session(
                    session.session_uuid,
                    reason=TicketReason.HUMAN_REQUESTED,
                    details='Одночасна ескалація клієнта',
                )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_take = pool.submit(run_takeover)
            f_esc = pool.submit(run_escalate)
            take_res = f_take.result()
            esc_ticket = f_esc.result()

        session.refresh_from_db()
        open_tickets = list(
            SupportTicket.objects.filter(
                session=session,
                status__in=(TicketStatus.OPEN, TicketStatus.IN_PROGRESS),
            )
        )
        self.assertEqual(len(open_tickets), 1)
        self.assertEqual(take_res.ticket.id, open_tickets[0].id)
        self.assertEqual(esc_ticket.id, open_tickets[0].id)
        self.assertEqual(open_tickets[0].status, TicketStatus.IN_PROGRESS)
        self.assertEqual(open_tickets[0].assigned_operator_id, op.id)
        self.assertEqual(session.status, ChatSessionStatus.ESCALATED)
        self.assertTrue(session.is_escalated)
        self.assertEqual(session.assigned_operator_id, op.id)

    def test_migration_0003_backfills_last_message_at_and_message_source(self):
        target_0002 = [('support', '0002_guest_token_constraints_and_message_ordering')]
        target_0003 = [('support', '0003_live_operator_support')]

        executor = MigrationExecutor(connection)
        leaf_targets = executor.loader.graph.leaf_nodes('support')
        executor.migrate(target_0002)
        try:
            old_apps = executor.loader.project_state(target_0002).apps
            OldChatSession = old_apps.get_model('support', 'ChatSession')
            OldChatMessage = old_apps.get_model('support', 'ChatMessage')

            sess_with_msgs = OldChatSession.objects.create(
                guest_token='mig0003-with-msgs',
                status='ACTIVE',
            )
            sess_empty = OldChatSession.objects.create(
                guest_token='mig0003-empty',
                status='ACTIVE',
            )
            t1 = timezone.now() - timedelta(minutes=10)
            t2 = timezone.now() - timedelta(minutes=2)
            m_user = OldChatMessage.objects.create(
                session=sess_with_msgs,
                role='user',
                content='Питання',
            )
            m_ai = OldChatMessage.objects.create(
                session=sess_with_msgs,
                role='assistant',
                content='Відповідь AI',
            )
            m_sys = OldChatMessage.objects.create(
                session=sess_with_msgs,
                role='system',
                content='Системна подія',
            )
            m_tool = OldChatMessage.objects.create(
                session=sess_with_msgs,
                role='tool',
                content='{"ok": true}',
            )
            OldChatMessage.objects.filter(pk=m_user.pk).update(created_at=t1)
            OldChatMessage.objects.filter(pk=m_ai.pk).update(created_at=t1)
            OldChatMessage.objects.filter(pk=m_tool.pk).update(created_at=t1)
            OldChatMessage.objects.filter(pk=m_sys.pk).update(created_at=t2)

            executor_fwd = MigrationExecutor(connection)
            executor_fwd.migrate(target_0003)
            new_apps = executor_fwd.loader.project_state(target_0003).apps
            NewChatSession = new_apps.get_model('support', 'ChatSession')
            NewChatMessage = new_apps.get_model('support', 'ChatMessage')

            migrated_sess = NewChatSession.objects.get(pk=sess_with_msgs.pk)
            migrated_empty = NewChatSession.objects.get(pk=sess_empty.pk)
            self.assertEqual(migrated_sess.last_message_at, t2)
            self.assertEqual(migrated_sess.operator_last_read_id, 0)
            self.assertIsNone(migrated_empty.last_message_at)

            self.assertEqual(NewChatMessage.objects.get(pk=m_user.pk).source, 'WEB')
            self.assertEqual(NewChatMessage.objects.get(pk=m_ai.pk).source, 'AI')
            self.assertEqual(NewChatMessage.objects.get(pk=m_sys.pk).source, 'SYSTEM')
            self.assertEqual(NewChatMessage.objects.get(pk=m_tool.pk).source, 'SYSTEM')
        finally:
            MigrationExecutor(connection).migrate(leaf_targets)
