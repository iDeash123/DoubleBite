import logging
import re
import secrets
import threading
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config.constants import (
    DEFAULT_SUPPORT_WORK_END_HOUR,
    DEFAULT_SUPPORT_WORK_START_HOUR,
)
from config.transactions import schedule_on_commit
from config.validators import extract_phone_digits
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils import timezone
from orders.middleware import get_guest_token, get_pre_login_guest_token
from orders.models import Order

from support.models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    FAQKnowledge,
    MessageRole,
    MessageSource,
    SupportTicket,
    TicketReason,
    TicketStatus,
)
from support.permissions import is_support_operator
from support.realtime import (
    build_message_event_data,
    build_status_event_data,
    publish_desk_event,
    publish_session_event,
)

logger = logging.getLogger('support')

_INT32_MAX = 2**31 - 1
_ACTIVE_SESSION_STATUSES = (ChatSessionStatus.ACTIVE, ChatSessionStatus.ESCALATED)
_OPEN_TICKET_STATUSES = (TicketStatus.OPEN, TicketStatus.IN_PROGRESS)
_SQLITE_SUPPORT_LOCK = threading.RLock()
MAX_SUPPORT_MESSAGE_LENGTH = 2000


class SupportDeskError(Exception):
    """Base domain error for live operator support desk operations."""


class SessionNotFoundError(SupportDeskError):
    """Raised when a target chat session cannot be found."""


class OperatorPermissionError(SupportDeskError):
    """Raised when a caller does not have support operator privileges."""


class InvalidSessionStateError(SupportDeskError):
    """Raised when a state transition is invalid for the current session status."""


class SessionOwnershipConflictError(SupportDeskError):
    """Raised when another operator owns the session or it has not been claimed."""

    def __init__(self, message: str, *, owner: Any = None) -> None:
        super().__init__(message)
        self.owner = owner


class InvalidMessageError(SupportDeskError):
    """Raised when a chat message is empty or exceeds length limits."""


@dataclass(frozen=True)
class DeskActionResult:
    session: ChatSession
    ticket: SupportTicket | None = None
    system_message: ChatMessage | None = None


def get_support_working_hours() -> tuple[int, int]:
    start = int(
        getattr(settings, 'SUPPORT_WORK_START_HOUR', DEFAULT_SUPPORT_WORK_START_HOUR)
    )
    end = int(
        getattr(settings, 'SUPPORT_WORK_END_HOUR', DEFAULT_SUPPORT_WORK_END_HOUR)
    )
    return start, end


def get_working_hours_label() -> str:
    start_hour, end_hour = get_support_working_hours()
    return f'{start_hour:02d}:00–{end_hour:02d}:00'


def is_within_working_hours(now: datetime | None = None) -> bool:
    tz_name = getattr(settings, 'TIME_ZONE', 'Europe/Kyiv') or 'Europe/Kyiv'
    tz = ZoneInfo(tz_name)
    current = timezone.now() if now is None else now
    if timezone.is_naive(current):
        local_dt = current.replace(tzinfo=tz)
    else:
        local_dt = current.astimezone(tz)
    start_hour, end_hour = get_support_working_hours()
    return start_hour <= local_dt.hour < end_hour


def format_off_hours_notice() -> str:
    start_hour, _ = get_support_working_hours()
    label = get_working_hours_label()
    return (
        f'Зараз неробочий час ({label}). Ваше звернення збережено — '
        f'залиште деталі, і адміністратор відповість з {start_hour:02d}:00.'
    )


def get_operator_display_name(user: Any) -> str:
    if user is None:
        return 'Адміністратор ресторану'
    get_full_name = getattr(user, 'get_full_name', None)
    full_name = str(get_full_name() or '').strip() if callable(get_full_name) else ''
    if full_name:
        return full_name
    first_name = str(getattr(user, 'first_name', '') or '').strip()
    last_name = str(getattr(user, 'last_name', '') or '').strip()
    combined = f'{first_name} {last_name}'.strip()
    if combined:
        return combined
    return str(getattr(user, 'email', '') or 'Адміністратор ресторану')

_APOSTROPHES_RE = re.compile(r'[’ʼ`]')
_WORD_RE = re.compile(r"[0-9a-zа-яіїєґ']+")
_FAQ_STOPWORDS = frozenset({
    'яка', 'які', 'який', 'яке', 'що', 'як', 'чи', 'де', 'коли', 'скільки',
    'хто', 'чому', 'куди', 'звідки', 'ви', 'вас', 'вам', 'ваші', 'ваша', 'ваше',
    'ваш', 'на', 'у', 'в', 'з', 'із', 'зі', 'до', 'від', 'за', 'по', 'про',
    'для', 'при', 'під', 'над', 'без', 'та', 'і', 'й', 'або', 'але', 'не',
    'це', 'є', 'буде', 'можна', 'треба', 'потрібно', 'скажіть', 'підкажіть',
})
_UKRAINIAN_SUFFIXES = (
    'енням', 'енню', 'ення', 'анням', 'анню', 'ання',
    'ості', 'істю', 'ість',
    'ами', 'ями', 'ою', 'ею', 'єю',
    'ої', 'ій', 'ий', 'их', 'им',
    'ом', 'ем', 'єм',
    'ів', 'їв', 'ей', 'ень', 'ань',
    'ах', 'ях', 'ам', 'ям',
    'єте', 'ємо', 'ють', 'уть', 'ати', 'яти', 'ити', 'іти',
    'а', 'я', 'у', 'ю', 'и', 'і', 'ї', 'о', 'е', 'є',
)


def _sqlite_support_guard() -> Any:
    db_conn = transaction.get_connection()
    if db_conn.vendor == 'sqlite' and not db_conn.in_atomic_block:
        return _SQLITE_SUPPORT_LOCK
    return nullcontext()


def _get_authenticated_user(request: HttpRequest | None) -> Any | None:
    if request is None:
        return None
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return user
    return None


def _get_candidate_guest_tokens(request: HttpRequest | None) -> list[str]:
    if request is None:
        return []
    tokens: list[str] = []
    guest_token = get_guest_token(request, create=False)
    if guest_token:
        tokens.append(guest_token)
    pre_token = get_pre_login_guest_token(request)
    if pre_token and pre_token not in tokens:
        tokens.append(pre_token)
    return tokens


def _normalize_text(text: str) -> str:
    return _APOSTROPHES_RE.sub("'", text.casefold())


def _ukrainian_stem(word: str) -> str:
    if len(word) < 4:
        return word
    for suffix in _UKRAINIAN_SUFFIXES:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            min_stem_len = 4 if len(suffix) == 1 and len(word) >= 5 else 3
            if len(stem) >= min_stem_len:
                return stem
    return word


def _tokenize_with_stems(text: str) -> tuple[set[str], set[str]]:
    tokens = set(_WORD_RE.findall(_normalize_text(text)))
    stems = {_ukrainian_stem(tok) for tok in tokens}
    return tokens, stems


def _extract_query_keywords(normalized_query: str) -> list[tuple[str, str]]:
    raw_tokens = _WORD_RE.findall(normalized_query)
    if not raw_tokens:
        return []
    seen: set[str] = set()
    filtered: list[str] = []
    for tok in raw_tokens:
        if tok in seen:
            continue
        if len(tok) > 2 and tok not in _FAQ_STOPWORDS:
            seen.add(tok)
            filtered.append(tok)
    if not filtered:
        for tok in raw_tokens:
            if tok not in seen and tok not in _FAQ_STOPWORDS:
                seen.add(tok)
                filtered.append(tok)
    if not filtered:
        for tok in raw_tokens:
            if tok not in seen:
                seen.add(tok)
                filtered.append(tok)
    return [(tok, _ukrainian_stem(tok)) for tok in filtered]


def _score_faq(
    faq: FAQKnowledge,
    query_cf: str,
    keywords: list[tuple[str, str]],
) -> int:
    q_cf = _normalize_text(faq.question)
    a_cf = _normalize_text(faq.answer)
    c_cf = _normalize_text(faq.category)

    q_tokens, q_stems = _tokenize_with_stems(faq.question)
    a_tokens, a_stems = _tokenize_with_stems(faq.answer)
    c_tokens, c_stems = _tokenize_with_stems(faq.category)

    score = 0
    if len(query_cf) >= 3:
        if query_cf in q_cf:
            score += 20
        if query_cf in a_cf:
            score += 10
        if query_cf in c_cf:
            score += 8

    matched_keywords = 0
    for kw, kw_stem in keywords:
        kw_matched = False

        if kw in q_tokens or (len(kw) >= 4 and kw in q_cf):
            score += 6
            kw_matched = True
        elif kw_stem in q_stems or (
            len(kw_stem) >= 4 and any(t.startswith(kw_stem) for t in q_tokens)
        ):
            score += 4
            kw_matched = True

        if kw in c_tokens or (len(kw) >= 4 and kw in c_cf):
            score += 4
            kw_matched = True
        elif kw_stem in c_stems or (
            len(kw_stem) >= 4 and any(t.startswith(kw_stem) for t in c_tokens)
        ):
            score += 2
            kw_matched = True

        if kw in a_tokens or (len(kw) >= 4 and kw in a_cf):
            score += 3
            kw_matched = True
        elif kw_stem in a_stems or (
            len(kw_stem) >= 4 and any(t.startswith(kw_stem) for t in a_tokens)
        ):
            score += 2
            kw_matched = True

        if kw_matched:
            matched_keywords += 1

    if matched_keywords > 0:
        score += matched_keywords * 4
        if len(keywords) > 1 and matched_keywords == len(keywords):
            score += 5

    return score


class ChatSessionService:
    @staticmethod
    def get_active_session(request: HttpRequest) -> ChatSession | None:
        user = _get_authenticated_user(request)
        if user is not None:
            active = (
                ChatSession.objects.filter(
                    user=user,
                    status__in=_ACTIVE_SESSION_STATUSES,
                )
                .order_by('-updated_at', '-created_at')
                .first()
            )
            if active is not None:
                return active

            for token in _get_candidate_guest_tokens(request):
                guest_session = (
                    ChatSession.objects.filter(
                        guest_token=token,
                        user__isnull=True,
                        status__in=_ACTIVE_SESSION_STATUSES,
                    )
                    .order_by('-updated_at', '-created_at')
                    .first()
                )
                if guest_session is not None:
                    return guest_session
            return None

        guest_token = get_guest_token(request, create=False)
        if not guest_token:
            return None
        return (
            ChatSession.objects.filter(
                guest_token=guest_token,
                user__isnull=True,
                status__in=_ACTIVE_SESSION_STATUSES,
            )
            .order_by('-updated_at', '-created_at')
            .first()
        )

    @staticmethod
    def get_latest_session(request: HttpRequest) -> ChatSession | None:
        active = ChatSessionService.get_active_session(request)
        if active is not None:
            return active

        user = _get_authenticated_user(request)
        if user is not None:
            candidates: list[ChatSession] = []
            resolved = (
                ChatSession.objects.filter(
                    user=user,
                    status=ChatSessionStatus.RESOLVED,
                )
                .order_by('-updated_at', '-created_at')
                .first()
            )
            if resolved is not None:
                candidates.append(resolved)

            candidate_tokens = _get_candidate_guest_tokens(request)
            if candidate_tokens:
                guest_resolved = (
                    ChatSession.objects.filter(
                        guest_token__in=candidate_tokens,
                        user__isnull=True,
                        status=ChatSessionStatus.RESOLVED,
                    )
                    .order_by('-updated_at', '-created_at')
                    .first()
                )
                if guest_resolved is not None:
                    candidates.append(guest_resolved)

            if not candidates:
                return None
            return max(candidates, key=lambda s: (s.updated_at, s.created_at))

        guest_token = get_guest_token(request, create=False)
        if not guest_token:
            return None
        return (
            ChatSession.objects.filter(
                guest_token=guest_token,
                user__isnull=True,
                status=ChatSessionStatus.RESOLVED,
            )
            .order_by('-updated_at', '-created_at')
            .first()
        )

    @staticmethod
    def attach_guest_session_to_user(
        request: HttpRequest,
        session: ChatSession,
    ) -> ChatSession:
        """Link an existing active/escalated guest session to request.user without creating new sessions."""
        user = _get_authenticated_user(request)
        if user is None or session.user_id is not None:
            return session

        candidate_tokens = _get_candidate_guest_tokens(request)
        if not session.guest_token or session.guest_token not in candidate_tokens:
            return session

        with _sqlite_support_guard(), transaction.atomic():
            active_user_session = (
                ChatSession.objects.select_for_update()
                .filter(user=user, status__in=_ACTIVE_SESSION_STATUSES)
                .first()
            )
            if active_user_session is not None:
                return active_user_session

            locked_guest = (
                ChatSession.objects.select_for_update()
                .filter(pk=session.pk)
                .first()
            )
            if locked_guest is None:
                return session
            if (
                locked_guest.user_id is not None
                or locked_guest.status not in _ACTIVE_SESSION_STATUSES
            ):
                _sync_session_attrs(session, locked_guest)
                return locked_guest

            try:
                with transaction.atomic():
                    locked_guest.user = user
                    locked_guest.guest_token = ''
                    locked_guest.save(
                        update_fields=['user', 'guest_token', 'updated_at']
                    )
                    _sync_session_attrs(session, locked_guest)
                    return locked_guest
            except IntegrityError:
                existing = (
                    ChatSession.objects.select_for_update()
                    .filter(user=user, status__in=_ACTIVE_SESSION_STATUSES)
                    .first()
                )
                if existing is not None:
                    return existing
                raise

    @staticmethod
    def get_or_create_active_session(request: HttpRequest) -> ChatSession:
        user = _get_authenticated_user(request)
        if user is not None:
            candidate_tokens = _get_candidate_guest_tokens(request)
            with _sqlite_support_guard(), transaction.atomic():
                active_session = (
                    ChatSession.objects.select_for_update()
                    .filter(user=user, status__in=_ACTIVE_SESSION_STATUSES)
                    .first()
                )
                if active_session is not None:
                    return active_session

                for token in candidate_tokens:
                    guest_session = (
                        ChatSession.objects.select_for_update()
                        .filter(
                            guest_token=token,
                            user__isnull=True,
                            status__in=_ACTIVE_SESSION_STATUSES,
                        )
                        .first()
                    )
                    if guest_session is not None:
                        try:
                            with transaction.atomic():
                                guest_session.user = user
                                guest_session.guest_token = ''
                                guest_session.save(
                                    update_fields=['user', 'guest_token', 'updated_at']
                                )
                                return guest_session
                        except IntegrityError:
                            existing = (
                                ChatSession.objects.select_for_update()
                                .filter(user=user, status__in=_ACTIVE_SESSION_STATUSES)
                                .first()
                            )
                            if existing is not None:
                                return existing
                            raise

                try:
                    with transaction.atomic():
                        created_session = ChatSession.objects.create(
                            user=user,
                            status=ChatSessionStatus.ACTIVE,
                        )
                    schedule_on_commit(
                        publish_desk_event,
                        'queue_update',
                        {
                            'reason': 'session_created',
                            'session_uuid': str(created_session.session_uuid),
                            'status': ChatSessionStatus.ACTIVE,
                        },
                        session_uuid=created_session.session_uuid,
                    )
                    return created_session
                except IntegrityError:
                    existing = (
                        ChatSession.objects.select_for_update()
                        .filter(user=user, status__in=_ACTIVE_SESSION_STATUSES)
                        .first()
                    )
                    if existing is not None:
                        return existing
                    raise

        guest_token = get_guest_token(request, create=True)
        with _sqlite_support_guard(), transaction.atomic():
            if not guest_token:
                created_session = ChatSession.objects.create(
                    guest_token='',
                    status=ChatSessionStatus.ACTIVE,
                )
                schedule_on_commit(
                    publish_desk_event,
                    'queue_update',
                    {
                        'reason': 'session_created',
                        'session_uuid': str(created_session.session_uuid),
                        'status': ChatSessionStatus.ACTIVE,
                    },
                    session_uuid=created_session.session_uuid,
                )
                return created_session

            active_session = (
                ChatSession.objects.select_for_update()
                .filter(
                    guest_token=guest_token,
                    user__isnull=True,
                    status__in=_ACTIVE_SESSION_STATUSES,
                )
                .first()
            )
            if active_session is not None:
                return active_session

            try:
                with transaction.atomic():
                    created_session = ChatSession.objects.create(
                        guest_token=guest_token,
                        status=ChatSessionStatus.ACTIVE,
                    )
                schedule_on_commit(
                    publish_desk_event,
                    'queue_update',
                    {
                        'reason': 'session_created',
                        'session_uuid': str(created_session.session_uuid),
                        'status': ChatSessionStatus.ACTIVE,
                    },
                    session_uuid=created_session.session_uuid,
                )
                return created_session
            except IntegrityError:
                existing = (
                    ChatSession.objects.select_for_update()
                    .filter(
                        guest_token=guest_token,
                        user__isnull=True,
                        status__in=_ACTIVE_SESSION_STATUSES,
                    )
                    .first()
                )
                if existing is not None:
                    return existing
                raise

    @staticmethod
    def get_session_for_request(
        request: HttpRequest,
        session_uuid: str | uuid.UUID,
    ) -> ChatSession | None:
        try:
            if isinstance(session_uuid, str):
                session_uuid = uuid.UUID(session_uuid)
            elif not isinstance(session_uuid, uuid.UUID):
                return None
        except (ValueError, TypeError, AttributeError):
            return None

        user = _get_authenticated_user(request)
        if user is not None:
            return ChatSession.objects.filter(
                session_uuid=session_uuid,
                user=user,
            ).first()

        guest_token = get_guest_token(request, create=False)
        if not guest_token:
            return None

        return ChatSession.objects.filter(
            session_uuid=session_uuid,
            user__isnull=True,
            guest_token=guest_token,
        ).first()

    @staticmethod
    def reset_active_session(request: HttpRequest) -> ChatSession:
        user = _get_authenticated_user(request)

        if user is not None:
            candidate_tokens = _get_candidate_guest_tokens(request)
            with _sqlite_support_guard(), transaction.atomic():
                locked_sessions = list(
                    ChatSession.objects.select_for_update()
                    .filter(
                        user=user,
                        status__in=_ACTIVE_SESSION_STATUSES,
                    )
                    .order_by('pk')
                )
                if candidate_tokens:
                    locked_guest_sessions = list(
                        ChatSession.objects.select_for_update()
                        .filter(
                            user__isnull=True,
                            guest_token__in=candidate_tokens,
                            status__in=_ACTIVE_SESSION_STATUSES,
                        )
                        .order_by('pk')
                    )
                    locked_sessions.extend(locked_guest_sessions)
                _resolve_sessions_and_tickets(locked_sessions, timezone.now())
                for old_session in locked_sessions:
                    schedule_on_commit(
                        publish_session_event,
                        old_session.session_uuid,
                        'session_resolved',
                        {
                            'status': ChatSessionStatus.RESOLVED,
                            'is_escalated': False,
                            'operator_connected': False,
                            'assigned_operator_id': None,
                            'assigned_operator_name': None,
                            'reason': 'reset',
                        },
                    )
                try:
                    with transaction.atomic():
                        new_session = ChatSession.objects.create(
                            user=user,
                            status=ChatSessionStatus.ACTIVE,
                        )
                    schedule_on_commit(
                        publish_desk_event,
                        'queue_update',
                        {
                            'reason': 'reset',
                            'session_uuid': str(new_session.session_uuid),
                            'status': ChatSessionStatus.ACTIVE,
                        },
                        session_uuid=new_session.session_uuid,
                    )
                    return new_session
                except IntegrityError:
                    existing = (
                        ChatSession.objects.select_for_update()
                        .filter(user=user, status__in=_ACTIVE_SESSION_STATUSES)
                        .first()
                    )
                    if existing is not None:
                        return existing
                    raise

        guest_token = get_guest_token(request, create=True)
        with _sqlite_support_guard(), transaction.atomic():
            if not guest_token:
                new_session = ChatSession.objects.create(
                    guest_token='',
                    status=ChatSessionStatus.ACTIVE,
                )
                schedule_on_commit(
                    publish_desk_event,
                    'queue_update',
                    {
                        'reason': 'reset',
                        'session_uuid': str(new_session.session_uuid),
                        'status': ChatSessionStatus.ACTIVE,
                    },
                    session_uuid=new_session.session_uuid,
                )
                return new_session

            locked_sessions = list(
                ChatSession.objects.select_for_update()
                .filter(
                    guest_token=guest_token,
                    user__isnull=True,
                    status__in=_ACTIVE_SESSION_STATUSES,
                )
                .order_by('pk')
            )
            _resolve_sessions_and_tickets(locked_sessions, timezone.now())
            for old_session in locked_sessions:
                schedule_on_commit(
                    publish_session_event,
                    old_session.session_uuid,
                    'session_resolved',
                    {
                        'status': ChatSessionStatus.RESOLVED,
                        'is_escalated': False,
                        'operator_connected': False,
                        'assigned_operator_id': None,
                        'assigned_operator_name': None,
                        'reason': 'reset',
                    },
                )
            try:
                with transaction.atomic():
                    new_session = ChatSession.objects.create(
                        guest_token=guest_token,
                        status=ChatSessionStatus.ACTIVE,
                    )
                schedule_on_commit(
                    publish_desk_event,
                    'queue_update',
                    {
                        'reason': 'reset',
                        'session_uuid': str(new_session.session_uuid),
                        'status': ChatSessionStatus.ACTIVE,
                    },
                    session_uuid=new_session.session_uuid,
                )
                return new_session
            except IntegrityError:
                existing = (
                    ChatSession.objects.select_for_update()
                    .filter(
                        guest_token=guest_token,
                        user__isnull=True,
                        status__in=_ACTIVE_SESSION_STATUSES,
                    )
                    .first()
                )
                if existing is not None:
                    return existing
                raise

    @staticmethod
    def escalate_session(
        session: ChatSession | uuid.UUID | str,
        reason: str,
        details: str,
        order: Order | None = None,
        customer_phone: str = '',
        *,
        now: datetime | None = None,
    ) -> SupportTicket:
        valid_reasons = {choice[0] for choice in TicketReason.choices}
        normalized_reason = reason if reason in valid_reasons else TicketReason.HUMAN_REQUESTED
        cleaned_details = str(details or '')[:1000]

        with _sqlite_support_guard(), transaction.atomic():
            locked_session = _lock_session(session)
            if locked_session.status == ChatSessionStatus.RESOLVED:
                raise InvalidSessionStateError(
                    'Неможливо ескалювати завершену сесію чату.'
                )

            existing_ticket = (
                SupportTicket.objects.select_for_update()
                .filter(
                    session=locked_session,
                    status__in=_OPEN_TICKET_STATUSES,
                )
                .order_by('-created_at', '-id')
                .first()
            )
            if existing_ticket is not None:
                if (
                    locked_session.status != ChatSessionStatus.ESCALATED
                    or not locked_session.is_escalated
                ):
                    locked_session.status = ChatSessionStatus.ESCALATED
                    locked_session.is_escalated = True
                    locked_session.save(
                        update_fields=['status', 'is_escalated', 'updated_at']
                    )
                _sync_session_attrs(session, locked_session)
                return existing_ticket

            phone = _resolve_customer_phone(
                locked_session,
                order=order,
                customer_phone=customer_phone,
            )

            try:
                with transaction.atomic():
                    ticket = SupportTicket.objects.create(
                        session=locked_session,
                        order=order,
                        customer_phone=phone,
                        reason=normalized_reason,
                        details=cleaned_details,
                        status=TicketStatus.OPEN,
                    )
            except IntegrityError:
                existing_ticket = (
                    SupportTicket.objects.filter(
                        session=locked_session,
                        status__in=_OPEN_TICKET_STATUSES,
                    )
                    .order_by('-created_at', '-id')
                    .first()
                )
                if existing_ticket is not None:
                    if (
                        locked_session.status != ChatSessionStatus.ESCALATED
                        or not locked_session.is_escalated
                    ):
                        locked_session.status = ChatSessionStatus.ESCALATED
                        locked_session.is_escalated = True
                        locked_session.save(
                            update_fields=['status', 'is_escalated', 'updated_at']
                        )
                    _sync_session_attrs(session, locked_session)
                    return existing_ticket
                raise

            system_text = (
                f'Звернення #{ticket.id} передано адміністратору. '
                'Ви можете дописати деталі прямо тут.'
            )
            working_hours_now = is_within_working_hours(now=now)
            working_hours_info = {
                'is_working_hours': working_hours_now,
                'working_hours_label': get_working_hours_label(),
                'off_hours_notice': format_off_hours_notice(),
            }
            if not working_hours_now:
                system_text = f"{system_text}\n{working_hours_info['off_hours_notice']}"

            sys_msg = ChatMessage.objects.create(
                session=locked_session,
                role=MessageRole.SYSTEM,
                source=MessageSource.SYSTEM,
                content=system_text,
            )
            locked_session.is_escalated = True
            locked_session.status = ChatSessionStatus.ESCALATED
            locked_session.escalation_reason = cleaned_details
            locked_session.assigned_operator = None
            locked_session.assigned_at = None
            locked_session.last_message_at = sys_msg.created_at
            locked_session.updated_at = sys_msg.created_at
            locked_session.save(
                update_fields=[
                    'is_escalated',
                    'status',
                    'escalation_reason',
                    'assigned_operator',
                    'assigned_at',
                    'last_message_at',
                    'updated_at',
                ]
            )
            _sync_session_attrs(session, locked_session)

            from support.emails import send_ticket_escalated_email

            schedule_on_commit(send_ticket_escalated_email, ticket)
            schedule_on_commit(
                publish_session_event,
                locked_session.session_uuid,
                'message',
                build_message_event_data(sys_msg, session=locked_session),
                event_id=sys_msg.id,
            )
            schedule_on_commit(
                publish_session_event,
                locked_session.session_uuid,
                'status_changed',
                build_status_event_data(
                    locked_session,
                    ticket=ticket,
                    operator_connected=False,
                    reason='escalated',
                    working_hours=working_hours_info,
                ),
            )
            schedule_on_commit(
                publish_desk_event,
                'queue_update',
                {
                    'reason': 'escalated',
                    'session_uuid': str(locked_session.session_uuid),
                    'ticket_id': ticket.id,
                    'status': ChatSessionStatus.ESCALATED,
                },
                session_uuid=locked_session.session_uuid,
            )
            return ticket


def _resolve_sessions_and_tickets(
    locked_sessions: list[ChatSession],
    now: datetime,
) -> None:
    if not locked_sessions:
        return
    session_pks = [s.pk for s in locked_sessions]
    open_tickets = list(
        SupportTicket.objects.select_for_update()
        .filter(
            session_id__in=session_pks,
            status__in=_OPEN_TICKET_STATUSES,
        )
        .order_by('pk')
    )
    if open_tickets:
        SupportTicket.objects.filter(
            pk__in=[t.pk for t in open_tickets]
        ).update(
            status=TicketStatus.RESOLVED,
            resolved_at=now,
        )
    ChatSession.objects.filter(pk__in=session_pks).update(
        status=ChatSessionStatus.RESOLVED,
        is_escalated=False,
        assigned_operator=None,
        assigned_at=None,
        updated_at=now,
    )


def _resolve_customer_phone(
    session: ChatSession,
    order: Order | None = None,
    customer_phone: str = '',
) -> str:
    raw_phone = str(customer_phone or '').strip()
    if not raw_phone and session.user and hasattr(session.user, 'phone'):
        raw_phone = str(session.user.phone or '').strip()
    if not raw_phone and order:
        raw_phone = str(order.customer_phone or '').strip()

    digits = extract_phone_digits(raw_phone, max_len=20)
    if 7 <= len(digits) <= 15:
        return f'+{digits}' if raw_phone.startswith('+') else digits
    return ''


def _resolve_session_uuid(session_or_uuid: ChatSession | uuid.UUID | str) -> uuid.UUID:
    if isinstance(session_or_uuid, ChatSession):
        return session_or_uuid.session_uuid
    if isinstance(session_or_uuid, uuid.UUID):
        return session_or_uuid
    if isinstance(session_or_uuid, str):
        try:
            return uuid.UUID(session_or_uuid)
        except ValueError as exc:
            raise SessionNotFoundError('Сесію чату не знайдено.') from exc
    raise SessionNotFoundError('Сесію чату не знайдено.')


def _lock_session(session_or_uuid: ChatSession | uuid.UUID | str) -> ChatSession:
    session_uuid = _resolve_session_uuid(session_or_uuid)
    try:
        return (
            ChatSession.objects.select_for_update(of=('self',))
            .select_related('user', 'assigned_operator')
            .get(pk=session_uuid)
        )
    except ChatSession.DoesNotExist as exc:
        raise SessionNotFoundError('Сесію чату не знайдено.') from exc


def _require_operator(operator: Any, *, allow_none: bool = False) -> None:
    if operator is None and allow_none:
        return
    if not is_support_operator(operator) or getattr(operator, 'pk', None) is None:
        raise OperatorPermissionError('Доступ дозволено лише менеджерам підтримки.')


def validate_message_content(content: Any) -> str:
    if content is None:
        raise InvalidMessageError('Повідомлення не може бути порожнім.')
    if not isinstance(content, str):
        raise InvalidMessageError('Повідомлення має бути текстом.')
    if '\x00' in content:
        raise InvalidMessageError('Повідомлення містить неприпустимі символи.')
    cleaned = content.strip()
    if not cleaned:
        raise InvalidMessageError('Повідомлення не може бути порожнім.')
    if len(cleaned) > MAX_SUPPORT_MESSAGE_LENGTH:
        raise InvalidMessageError(
            f'Повідомлення не може перевищувати {MAX_SUPPORT_MESSAGE_LENGTH} символів.'
        )
    return cleaned


_validate_message_content = validate_message_content


_VALID_MESSAGE_SOURCES = frozenset(choice[0] for choice in MessageSource.choices)
_CUSTOMER_MESSAGE_SOURCES = frozenset({MessageSource.WEB})
_OPERATOR_MESSAGE_SOURCES = frozenset({MessageSource.WEB, MessageSource.TELEGRAM})


def _validate_message_source(
    source: str,
    *,
    allowed: frozenset[str] = _VALID_MESSAGE_SOURCES,
) -> str:
    if source not in allowed:
        raise ValueError(f'Некоректне джерело повідомлення: {source!r}')
    return source


def _proactive_takeover_locked(
    locked_session: ChatSession,
    operator: Any,
    details: str = '',
) -> DeskActionResult:
    now = timezone.now()
    cleaned_details = (
        str(details or '').strip()[:1000] or 'Менеджер перехопив діалог із AI.'
    )

    ticket = (
        SupportTicket.objects.select_for_update()
        .filter(session=locked_session, status__in=_OPEN_TICKET_STATUSES)
        .order_by('-created_at', '-id')
        .first()
    )
    if ticket is not None:
        ticket.status = TicketStatus.IN_PROGRESS
        ticket.assigned_operator = operator
        ticket.save(update_fields=['status', 'assigned_operator'])
    else:
        ticket = SupportTicket.objects.create(
            session=locked_session,
            customer_phone=_resolve_customer_phone(locked_session),
            reason=TicketReason.OPERATOR_TAKEOVER,
            details=cleaned_details,
            status=TicketStatus.IN_PROGRESS,
            assigned_operator=operator,
        )

    sys_msg = ChatMessage.objects.create(
        session=locked_session,
        role=MessageRole.SYSTEM,
        source=MessageSource.SYSTEM,
        content='До діалогу підключився адміністратор ресторану.',
    )
    locked_session.status = ChatSessionStatus.ESCALATED
    locked_session.is_escalated = True
    locked_session.escalation_reason = cleaned_details
    locked_session.assigned_operator = operator
    locked_session.assigned_at = now
    locked_session.last_message_at = sys_msg.created_at
    locked_session.updated_at = sys_msg.created_at
    locked_session.save(
        update_fields=[
            'status',
            'is_escalated',
            'escalation_reason',
            'assigned_operator',
            'assigned_at',
            'last_message_at',
            'updated_at',
        ]
    )
    schedule_on_commit(
        publish_session_event,
        locked_session.session_uuid,
        'message',
        build_message_event_data(sys_msg, session=locked_session),
        event_id=sys_msg.id,
    )
    schedule_on_commit(
        publish_session_event,
        locked_session.session_uuid,
        'operator_joined',
        build_status_event_data(
            locked_session,
            operator=operator,
            ticket=ticket,
            operator_connected=True,
            reason='proactive_takeover',
        ),
    )
    schedule_on_commit(
        publish_desk_event,
        'queue_update',
        {
            'reason': 'proactive_takeover',
            'session_uuid': str(locked_session.session_uuid),
            'ticket_id': ticket.id,
            'status': ChatSessionStatus.ESCALATED,
            'assigned_operator_id': operator.id,
            'assigned_operator_name': get_operator_display_name(operator),
        },
        session_uuid=locked_session.session_uuid,
    )
    return DeskActionResult(
        session=locked_session,
        ticket=ticket,
        system_message=sys_msg,
    )


class SupportDeskService:
    @staticmethod
    def claim_session(
        session: ChatSession | uuid.UUID | str,
        operator: Any,
    ) -> DeskActionResult:
        _require_operator(operator)
        with _sqlite_support_guard(), transaction.atomic():
            locked_session = _lock_session(session)
            if locked_session.status != ChatSessionStatus.ESCALATED:
                raise InvalidSessionStateError(
                    'Взяти в роботу можна лише ескальовану сесію.'
                )

            ticket = (
                SupportTicket.objects.select_for_update()
                .filter(session=locked_session, status__in=_OPEN_TICKET_STATUSES)
                .order_by('-created_at', '-id')
                .first()
            )

            if locked_session.assigned_operator_id is not None:
                if locked_session.assigned_operator_id != operator.id:
                    owner = locked_session.assigned_operator
                    raise SessionOwnershipConflictError(
                        f'Діалог вже веде {get_operator_display_name(owner)}.',
                        owner=owner,
                    )
                if ticket is not None and (
                    ticket.status != TicketStatus.IN_PROGRESS
                    or ticket.assigned_operator_id != operator.id
                ):
                    ticket.status = TicketStatus.IN_PROGRESS
                    ticket.assigned_operator = operator
                    ticket.save(update_fields=['status', 'assigned_operator'])
                _sync_session_attrs(session, locked_session)
                return DeskActionResult(
                    session=locked_session,
                    ticket=ticket,
                    system_message=None,
                )

            now = timezone.now()
            sys_msg = ChatMessage.objects.create(
                session=locked_session,
                role=MessageRole.SYSTEM,
                source=MessageSource.SYSTEM,
                content='До діалогу підключився адміністратор ресторану.',
            )
            locked_session.assigned_operator = operator
            locked_session.assigned_at = now
            locked_session.last_message_at = sys_msg.created_at
            locked_session.updated_at = sys_msg.created_at
            locked_session.save(
                update_fields=[
                    'assigned_operator',
                    'assigned_at',
                    'last_message_at',
                    'updated_at',
                ]
            )
            if ticket is not None:
                ticket.status = TicketStatus.IN_PROGRESS
                ticket.assigned_operator = operator
                ticket.save(update_fields=['status', 'assigned_operator'])

            _sync_session_attrs(session, locked_session)
            schedule_on_commit(
                publish_session_event,
                locked_session.session_uuid,
                'message',
                build_message_event_data(sys_msg, session=locked_session),
                event_id=sys_msg.id,
            )
            schedule_on_commit(
                publish_session_event,
                locked_session.session_uuid,
                'operator_joined',
                build_status_event_data(
                    locked_session,
                    operator=operator,
                    ticket=ticket,
                    operator_connected=True,
                    reason='claimed',
                ),
            )
            schedule_on_commit(
                publish_desk_event,
                'queue_update',
                {
                    'reason': 'claimed',
                    'session_uuid': str(locked_session.session_uuid),
                    'ticket_id': ticket.id if ticket is not None else None,
                    'status': ChatSessionStatus.ESCALATED,
                    'assigned_operator_id': operator.id,
                    'assigned_operator_name': get_operator_display_name(operator),
                },
                session_uuid=locked_session.session_uuid,
            )
            return DeskActionResult(
                session=locked_session,
                ticket=ticket,
                system_message=sys_msg,
            )

    @staticmethod
    def proactive_takeover(
        session: ChatSession | uuid.UUID | str,
        operator: Any,
        details: str = '',
    ) -> DeskActionResult:
        _require_operator(operator)
        with _sqlite_support_guard(), transaction.atomic():
            locked_session = _lock_session(session)
            if locked_session.status != ChatSessionStatus.ACTIVE:
                raise InvalidSessionStateError(
                    'Проактивно перехопити можна лише активну AI-сесію.'
                )
            result = _proactive_takeover_locked(
                locked_session,
                operator,
                details=details,
            )
            _sync_session_attrs(session, locked_session)
            return result

    @staticmethod
    def takeover_session(
        session: ChatSession | uuid.UUID | str,
        operator: Any,
        *,
        allow_proactive: bool = False,
        details: str = '',
    ) -> DeskActionResult:
        _require_operator(operator)
        with _sqlite_support_guard(), transaction.atomic():
            locked_session = _lock_session(session)
            if locked_session.status == ChatSessionStatus.ACTIVE:
                if allow_proactive:
                    result = _proactive_takeover_locked(
                        locked_session,
                        operator,
                        details=details,
                    )
                    _sync_session_attrs(session, locked_session)
                    return result
                raise InvalidSessionStateError(
                    'Для перехоплення активної AI-сесії використайте проактивне перехоплення.'
                )
            if locked_session.status != ChatSessionStatus.ESCALATED:
                raise InvalidSessionStateError(
                    'Перехопити можна лише ескальовану сесію.'
                )

            ticket = (
                SupportTicket.objects.select_for_update()
                .filter(session=locked_session, status__in=_OPEN_TICKET_STATUSES)
                .order_by('-created_at', '-id')
                .first()
            )
            if locked_session.assigned_operator_id == operator.id:
                if ticket is not None and (
                    ticket.status != TicketStatus.IN_PROGRESS
                    or ticket.assigned_operator_id != operator.id
                ):
                    ticket.status = TicketStatus.IN_PROGRESS
                    ticket.assigned_operator = operator
                    ticket.save(update_fields=['status', 'assigned_operator'])
                _sync_session_attrs(session, locked_session)
                return DeskActionResult(
                    session=locked_session,
                    ticket=ticket,
                    system_message=None,
                )

            had_previous_operator = locked_session.assigned_operator_id is not None
            sys_text = (
                'Діалог продовжить інший адміністратор ресторану.'
                if had_previous_operator
                else 'До діалогу підключився адміністратор ресторану.'
            )
            now = timezone.now()
            sys_msg = ChatMessage.objects.create(
                session=locked_session,
                role=MessageRole.SYSTEM,
                source=MessageSource.SYSTEM,
                content=sys_text,
            )
            locked_session.assigned_operator = operator
            locked_session.assigned_at = now
            locked_session.last_message_at = sys_msg.created_at
            locked_session.updated_at = sys_msg.created_at
            locked_session.save(
                update_fields=[
                    'assigned_operator',
                    'assigned_at',
                    'last_message_at',
                    'updated_at',
                ]
            )
            if ticket is not None:
                ticket.status = TicketStatus.IN_PROGRESS
                ticket.assigned_operator = operator
                ticket.save(update_fields=['status', 'assigned_operator'])

            _sync_session_attrs(session, locked_session)
            takeover_reason = 'takeover' if had_previous_operator else 'claimed'
            schedule_on_commit(
                publish_session_event,
                locked_session.session_uuid,
                'message',
                build_message_event_data(sys_msg, session=locked_session),
                event_id=sys_msg.id,
            )
            schedule_on_commit(
                publish_session_event,
                locked_session.session_uuid,
                'operator_joined',
                build_status_event_data(
                    locked_session,
                    operator=operator,
                    ticket=ticket,
                    operator_connected=True,
                    reason=takeover_reason,
                ),
            )
            schedule_on_commit(
                publish_desk_event,
                'queue_update',
                {
                    'reason': takeover_reason,
                    'session_uuid': str(locked_session.session_uuid),
                    'ticket_id': ticket.id if ticket is not None else None,
                    'status': ChatSessionStatus.ESCALATED,
                    'assigned_operator_id': operator.id,
                    'assigned_operator_name': get_operator_display_name(operator),
                },
                session_uuid=locked_session.session_uuid,
            )
            return DeskActionResult(
                session=locked_session,
                ticket=ticket,
                system_message=sys_msg,
            )

    @staticmethod
    def send_customer_message(
        session: ChatSession | uuid.UUID | str,
        content: str,
        *,
        source: str = MessageSource.WEB,
    ) -> ChatMessage:
        cleaned = _validate_message_content(content)
        validated_source = _validate_message_source(
            source, allowed=_CUSTOMER_MESSAGE_SOURCES
        )
        with _sqlite_support_guard(), transaction.atomic():
            locked_session = _lock_session(session)
            if locked_session.status != ChatSessionStatus.ESCALATED:
                raise InvalidSessionStateError(
                    'Повідомлення оператору можна надсилати лише в ескальованій сесії.'
                )
            msg = ChatMessage.objects.create(
                session=locked_session,
                role=MessageRole.USER,
                source=validated_source,
                content=cleaned,
            )
            locked_session.last_message_at = msg.created_at
            locked_session.updated_at = msg.created_at
            locked_session.save(
                update_fields=['last_message_at', 'updated_at']
            )
            _sync_session_attrs(session, locked_session)
            schedule_on_commit(
                publish_session_event,
                locked_session.session_uuid,
                'message',
                build_message_event_data(msg, session=locked_session),
                event_id=msg.id,
            )
            schedule_on_commit(
                publish_desk_event,
                'queue_update',
                {
                    'reason': 'customer_message',
                    'session_uuid': str(locked_session.session_uuid),
                    'message_id': msg.id,
                    'status': locked_session.status,
                },
                session_uuid=locked_session.session_uuid,
            )
            return msg

    @staticmethod
    def send_operator_message(
        session: ChatSession | uuid.UUID | str,
        operator: Any,
        content: str,
        *,
        source: str = MessageSource.WEB,
    ) -> ChatMessage:
        validated_source = _validate_message_source(
            source, allowed=_OPERATOR_MESSAGE_SOURCES
        )
        _require_operator(
            operator,
            allow_none=(validated_source == MessageSource.TELEGRAM),
        )
        cleaned = _validate_message_content(content)

        with _sqlite_support_guard(), transaction.atomic():
            locked_session = _lock_session(session)
            if locked_session.status != ChatSessionStatus.ESCALATED:
                raise InvalidSessionStateError(
                    'Відповідати можна лише в ескальованій сесії.'
                )

            session_update_fields = [
                'last_message_at',
                'operator_last_read_id',
                'updated_at',
            ]

            auto_join_sys_msg: ChatMessage | None = None
            auto_join_ticket: SupportTicket | None = None
            if validated_source == MessageSource.TELEGRAM:
                ticket = (
                    SupportTicket.objects.select_for_update()
                    .filter(session=locked_session, status__in=_OPEN_TICKET_STATUSES)
                    .order_by('-created_at', '-id')
                    .first()
                )
                auto_join_ticket = ticket
                was_already_in_progress = (
                    ticket is not None and ticket.status == TicketStatus.IN_PROGRESS
                )
                became_in_progress = False
                if ticket is not None:
                    ticket_fields: list[str] = []
                    if ticket.status == TicketStatus.OPEN:
                        ticket.status = TicketStatus.IN_PROGRESS
                        ticket_fields.append('status')
                        became_in_progress = True
                    if operator is not None and ticket.assigned_operator_id is None:
                        ticket.assigned_operator = operator
                        ticket_fields.append('assigned_operator')
                    if ticket_fields:
                        ticket.save(update_fields=ticket_fields)
                if operator is not None and locked_session.assigned_operator_id is None:
                    locked_session.assigned_operator = operator
                    locked_session.assigned_at = timezone.now()
                    session_update_fields.extend(['assigned_operator', 'assigned_at'])
                    if not was_already_in_progress:
                        became_in_progress = True
                if became_in_progress:
                    auto_join_sys_msg = ChatMessage.objects.create(
                        session=locked_session,
                        role=MessageRole.SYSTEM,
                        source=MessageSource.SYSTEM,
                        content='До діалогу підключився адміністратор ресторану.',
                    )
            else:
                if locked_session.assigned_operator_id is None:
                    raise SessionOwnershipConflictError(
                        'Спочатку візьміть звернення в роботу.',
                        owner=None,
                    )
                if locked_session.assigned_operator_id != operator.id:
                    owner = locked_session.assigned_operator
                    raise SessionOwnershipConflictError(
                        f'Діалог веде {get_operator_display_name(owner)}.',
                        owner=owner,
                    )

            msg = ChatMessage.objects.create(
                session=locked_session,
                role=MessageRole.OPERATOR,
                sender_user=operator,
                source=validated_source,
                content=cleaned,
            )
            locked_session.last_message_at = msg.created_at
            locked_session.operator_last_read_id = max(
                locked_session.operator_last_read_id,
                msg.id,
            )
            locked_session.updated_at = msg.created_at
            locked_session.save(update_fields=session_update_fields)
            _sync_session_attrs(session, locked_session)

            if auto_join_sys_msg is not None:
                schedule_on_commit(
                    publish_session_event,
                    locked_session.session_uuid,
                    'message',
                    build_message_event_data(
                        auto_join_sys_msg, session=locked_session
                    ),
                    event_id=auto_join_sys_msg.id,
                )
                schedule_on_commit(
                    publish_session_event,
                    locked_session.session_uuid,
                    'operator_joined',
                    build_status_event_data(
                        locked_session,
                        operator=operator,
                        ticket=auto_join_ticket,
                        operator_connected=True,
                        reason='telegram_auto_claim',
                    ),
                )
            schedule_on_commit(
                publish_session_event,
                locked_session.session_uuid,
                'message',
                build_message_event_data(
                    msg,
                    session=locked_session,
                    operator_name=(
                        get_operator_display_name(operator)
                        if operator is not None
                        else 'Адміністратор ресторану'
                    ),
                ),
                event_id=msg.id,
            )
            schedule_on_commit(
                publish_desk_event,
                'queue_update',
                {
                    'reason': 'operator_message',
                    'session_uuid': str(locked_session.session_uuid),
                    'message_id': msg.id,
                    'status': locked_session.status,
                },
                session_uuid=locked_session.session_uuid,
            )
            return msg

    @staticmethod
    def return_to_ai(
        session: ChatSession | uuid.UUID | str,
        operator: Any,
        *,
        source: str = MessageSource.WEB,
    ) -> DeskActionResult:
        validated_source = _validate_message_source(
            source, allowed=_OPERATOR_MESSAGE_SOURCES
        )
        _require_operator(
            operator,
            allow_none=(validated_source == MessageSource.TELEGRAM),
        )
        with _sqlite_support_guard(), transaction.atomic():
            locked_session = _lock_session(session)
            if locked_session.status != ChatSessionStatus.ESCALATED:
                raise InvalidSessionStateError(
                    'Повернути AI можна лише для ескальованої сесії.'
                )

            now = timezone.now()
            open_tickets = list(
                SupportTicket.objects.select_for_update()
                .filter(session=locked_session, status__in=_OPEN_TICKET_STATUSES)
                .order_by('-created_at', '-id')
            )
            ticket = open_tickets[0] if open_tickets else None
            if open_tickets:
                SupportTicket.objects.filter(
                    pk__in=[t.pk for t in open_tickets]
                ).update(
                    status=TicketStatus.RESOLVED,
                    resolved_at=now,
                )
                ticket.status = TicketStatus.RESOLVED
                ticket.resolved_at = now

            sys_msg = ChatMessage.objects.create(
                session=locked_session,
                role=MessageRole.SYSTEM,
                source=MessageSource.SYSTEM,
                content=(
                    'Адміністратор завершив діалог. '
                    'Далі вам допомагає AI-консьєрж Double Bite.'
                ),
            )
            locked_session.status = ChatSessionStatus.ACTIVE
            locked_session.is_escalated = False
            locked_session.assigned_operator = None
            locked_session.assigned_at = None
            locked_session.last_message_at = sys_msg.created_at
            locked_session.updated_at = sys_msg.created_at
            locked_session.save(
                update_fields=[
                    'status',
                    'is_escalated',
                    'assigned_operator',
                    'assigned_at',
                    'last_message_at',
                    'updated_at',
                ]
            )
            _sync_session_attrs(session, locked_session)
            schedule_on_commit(
                publish_session_event,
                locked_session.session_uuid,
                'message',
                build_message_event_data(sys_msg, session=locked_session),
                event_id=sys_msg.id,
            )
            schedule_on_commit(
                publish_session_event,
                locked_session.session_uuid,
                'status_changed',
                build_status_event_data(
                    locked_session,
                    operator=None,
                    ticket=ticket,
                    operator_connected=False,
                    reason='returned_to_ai',
                ),
            )
            schedule_on_commit(
                publish_desk_event,
                'queue_update',
                {
                    'reason': 'returned_to_ai',
                    'session_uuid': str(locked_session.session_uuid),
                    'ticket_id': ticket.id if ticket is not None else None,
                    'status': ChatSessionStatus.ACTIVE,
                },
                session_uuid=locked_session.session_uuid,
            )
            return DeskActionResult(
                session=locked_session,
                ticket=ticket,
                system_message=sys_msg,
            )

    @staticmethod
    def resolve_session(
        session: ChatSession | uuid.UUID | str,
        operator: Any,
        *,
        source: str = MessageSource.WEB,
    ) -> DeskActionResult:
        validated_source = _validate_message_source(
            source, allowed=_OPERATOR_MESSAGE_SOURCES
        )
        _require_operator(
            operator,
            allow_none=(validated_source == MessageSource.TELEGRAM),
        )
        with _sqlite_support_guard(), transaction.atomic():
            locked_session = _lock_session(session)
            if locked_session.status not in _ACTIVE_SESSION_STATUSES:
                raise InvalidSessionStateError('Сесію чату вже завершено.')

            now = timezone.now()
            open_tickets = list(
                SupportTicket.objects.select_for_update()
                .filter(session=locked_session, status__in=_OPEN_TICKET_STATUSES)
                .order_by('-created_at', '-id')
            )
            ticket = open_tickets[0] if open_tickets else None
            if open_tickets:
                SupportTicket.objects.filter(
                    pk__in=[t.pk for t in open_tickets]
                ).update(
                    status=TicketStatus.RESOLVED,
                    resolved_at=now,
                )
                ticket.status = TicketStatus.RESOLVED
                ticket.resolved_at = now

            sys_msg = ChatMessage.objects.create(
                session=locked_session,
                role=MessageRole.SYSTEM,
                source=MessageSource.SYSTEM,
                content=(
                    'Звернення завершено. Якщо виникнуть нові запитання — '
                    'натисніть «Новий діалог».'
                ),
            )
            locked_session.status = ChatSessionStatus.RESOLVED
            locked_session.is_escalated = False
            locked_session.assigned_operator = None
            locked_session.assigned_at = None
            locked_session.last_message_at = sys_msg.created_at
            locked_session.updated_at = sys_msg.created_at
            locked_session.save(
                update_fields=[
                    'status',
                    'is_escalated',
                    'assigned_operator',
                    'assigned_at',
                    'last_message_at',
                    'updated_at',
                ]
            )
            _sync_session_attrs(session, locked_session)
            schedule_on_commit(
                publish_session_event,
                locked_session.session_uuid,
                'message',
                build_message_event_data(sys_msg, session=locked_session),
                event_id=sys_msg.id,
            )
            schedule_on_commit(
                publish_session_event,
                locked_session.session_uuid,
                'session_resolved',
                build_status_event_data(
                    locked_session,
                    operator=None,
                    ticket=ticket,
                    operator_connected=False,
                    reason='resolved',
                ),
            )
            schedule_on_commit(
                publish_desk_event,
                'queue_update',
                {
                    'reason': 'resolved',
                    'session_uuid': str(locked_session.session_uuid),
                    'ticket_id': ticket.id if ticket is not None else None,
                    'status': ChatSessionStatus.RESOLVED,
                },
                session_uuid=locked_session.session_uuid,
            )
            return DeskActionResult(
                session=locked_session,
                ticket=ticket,
                system_message=sys_msg,
            )

    @staticmethod
    def mark_session_read(
        session: ChatSession | uuid.UUID | str,
        operator: Any,
        last_message_id: int | None = None,
    ) -> int:
        _require_operator(operator)
        if last_message_id is not None and (
            isinstance(last_message_id, bool)
            or not isinstance(last_message_id, int)
            or last_message_id < 0
        ):
            raise ValueError('last_message_id must be a non-negative integer')

        with _sqlite_support_guard(), transaction.atomic():
            locked_session = _lock_session(session)
            latest_id = (
                locked_session.messages.order_by('-id')
                .values_list('id', flat=True)
                .first()
                or 0
            )
            target_id = (
                latest_id
                if last_message_id is None
                else min(last_message_id, latest_id)
            )
            new_read_id = max(locked_session.operator_last_read_id, target_id)
            if new_read_id > locked_session.operator_last_read_id:
                locked_session.operator_last_read_id = new_read_id
                locked_session.save(update_fields=['operator_last_read_id'])
                schedule_on_commit(
                    publish_desk_event,
                    'queue_update',
                    {
                        'reason': 'marked_read',
                        'session_uuid': str(locked_session.session_uuid),
                    },
                    session_uuid=locked_session.session_uuid,
                )
            _sync_session_attrs(session, locked_session)
            return locked_session.operator_last_read_id


def _sync_session_attrs(target: Any, source: ChatSession) -> None:
    if not isinstance(target, ChatSession) or target is source:
        return
    target.is_escalated = source.is_escalated
    target.status = source.status
    target.escalation_reason = source.escalation_reason
    target.assigned_operator = source.assigned_operator
    target.assigned_at = source.assigned_at
    target.last_message_at = source.last_message_at
    target.operator_last_read_id = source.operator_last_read_id
    target.updated_at = source.updated_at


def validate_order_access(order: Order, request: HttpRequest) -> bool:
    user = _get_authenticated_user(request)
    if user is not None and order.user_id == user.id:
        return True

    if order.user_id is None and order.guest_token:
        guest_token = get_guest_token(request, create=False)
        if guest_token and secrets.compare_digest(str(order.guest_token), str(guest_token)):
            return True
        pre_token = get_pre_login_guest_token(request)
        if pre_token and secrets.compare_digest(str(order.guest_token), str(pre_token)):
            return True

    return False


def check_order_status_for_request(
    request: HttpRequest,
    order_id: int | str,
) -> dict[str, Any]:
    if order_id is None or isinstance(order_id, bool):
        return {'error': 'Замовлення не знайдено або доступ заборонено'}

    cleaned_id = str(order_id).strip()
    if not cleaned_id:
        return {'error': 'Замовлення не знайдено або доступ заборонено'}

    normalized = cleaned_id.lstrip('#').strip()
    order = None
    if normalized:
        order = Order.objects.filter(order_number__iexact=normalized).first()
    if order is None and cleaned_id != normalized:
        order = Order.objects.filter(order_number__iexact=cleaned_id).first()

    if order is None and normalized.isascii() and normalized.isdigit():
        pk_val = int(normalized)
        if 1 <= pk_val <= _INT32_MAX:
            order = Order.objects.filter(pk=pk_val).first()

    if not order or not validate_order_access(order, request):
        return {'error': 'Замовлення не знайдено або доступ заборонено'}

    items_summary = [
        f'{item.dish_title} x {item.quantity}'
        for item in order.items.all()
    ]

    return {
        'order_id': order.id,
        'order_number': order.order_number,
        'status': order.status,
        'status_display': order.get_status_display(),
        'eta_minutes': order.eta_minutes,
        'address': order.delivery_address,
        'customer_name': order.customer_name,
        'total_amount': float(order.total_amount),
        'payment_status': order.get_payment_status_display(),
        'created_at': order.created_at.strftime('%d.%m.%Y %H:%M'),
        'items': items_summary,
    }


def get_faq_answers(query: str) -> list[dict[str, Any]]:
    cleaned_query = str(query or '').strip()
    if not cleaned_query:
        return []

    query_cf = _normalize_text(cleaned_query)
    keywords = _extract_query_keywords(query_cf)
    scored: list[tuple[int, FAQKnowledge]] = []

    for faq in FAQKnowledge.objects.filter(is_active=True):
        score = _score_faq(faq, query_cf, keywords)
        if score > 0:
            scored.append((score, faq))

    scored.sort(key=lambda item: (-item[0], item[1].id))

    return [
        {
            'id': faq.id,
            'question': faq.question,
            'answer': faq.answer,
            'category': faq.category,
        }
        for _, faq in scored[:5]
    ]
