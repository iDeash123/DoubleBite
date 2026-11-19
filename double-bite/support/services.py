import logging
import re
import secrets
import threading
import uuid
from contextlib import nullcontext
from typing import Any

from config.transactions import schedule_on_commit
from config.validators import extract_phone_digits
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils import timezone
from orders.middleware import get_guest_token, get_pre_login_guest_token
from orders.models import Order

from support.models import (
    ChatSession,
    ChatSessionStatus,
    FAQKnowledge,
    SupportTicket,
    TicketReason,
    TicketStatus,
)

logger = logging.getLogger('support')

_INT32_MAX = 2**31 - 1
_ACTIVE_SESSION_STATUSES = (ChatSessionStatus.ACTIVE, ChatSessionStatus.ESCALATED)
_SQLITE_SUPPORT_LOCK = threading.RLock()

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
                        return ChatSession.objects.create(
                            user=user,
                            status=ChatSessionStatus.ACTIVE,
                        )
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
                return ChatSession.objects.create(
                    guest_token='',
                    status=ChatSessionStatus.ACTIVE,
                )

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
                    return ChatSession.objects.create(
                        guest_token=guest_token,
                        status=ChatSessionStatus.ACTIVE,
                    )
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
        now = timezone.now()

        if user is not None:
            with _sqlite_support_guard(), transaction.atomic():
                locked_sessions = list(
                    ChatSession.objects.select_for_update().filter(
                        user=user,
                        status__in=_ACTIVE_SESSION_STATUSES,
                    )
                )
                if locked_sessions:
                    ChatSession.objects.filter(
                        pk__in=[s.pk for s in locked_sessions]
                    ).update(
                        status=ChatSessionStatus.RESOLVED,
                        is_escalated=False,
                        updated_at=now,
                    )
                try:
                    with transaction.atomic():
                        return ChatSession.objects.create(
                            user=user,
                            status=ChatSessionStatus.ACTIVE,
                        )
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
                return ChatSession.objects.create(
                    guest_token='',
                    status=ChatSessionStatus.ACTIVE,
                )

            locked_sessions = list(
                ChatSession.objects.select_for_update().filter(
                    guest_token=guest_token,
                    user__isnull=True,
                    status__in=_ACTIVE_SESSION_STATUSES,
                )
            )
            if locked_sessions:
                ChatSession.objects.filter(
                    pk__in=[s.pk for s in locked_sessions]
                ).update(
                    status=ChatSessionStatus.RESOLVED,
                    is_escalated=False,
                    updated_at=now,
                )
            try:
                with transaction.atomic():
                    return ChatSession.objects.create(
                        guest_token=guest_token,
                        status=ChatSessionStatus.ACTIVE,
                    )
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
        session: ChatSession,
        reason: str,
        details: str,
        order: Order | None = None,
        customer_phone: str = '',
    ) -> SupportTicket:
        valid_reasons = {choice[0] for choice in TicketReason.choices}
        normalized_reason = reason if reason in valid_reasons else TicketReason.HUMAN_REQUESTED
        cleaned_details = str(details or '')[:1000]
        cooldown_key = f'escalation_cooldown:{session.pk}'

        with _sqlite_support_guard(), transaction.atomic():
            locked_session = (
                ChatSession.objects.select_for_update()
                .select_related('user')
                .get(pk=session.pk)
            )

            existing_ticket = (
                SupportTicket.objects.select_for_update()
                .filter(
                    session=locked_session,
                    status__in=[TicketStatus.OPEN, TicketStatus.IN_PROGRESS],
                )
                .order_by('-created_at', '-id')
                .first()
            )
            if existing_ticket is not None:
                _sync_session_attrs(session, locked_session)
                return existing_ticket

            if cache.get(cooldown_key):
                recent_ticket = (
                    SupportTicket.objects.filter(session=locked_session)
                    .order_by('-created_at', '-id')
                    .first()
                )
                if recent_ticket is not None:
                    _sync_session_attrs(session, locked_session)
                    return recent_ticket

            raw_phone = str(customer_phone or '').strip()
            if not raw_phone and locked_session.user and hasattr(locked_session.user, 'phone'):
                raw_phone = str(locked_session.user.phone or '').strip()
            if not raw_phone and order:
                raw_phone = str(order.customer_phone or '').strip()

            digits = extract_phone_digits(raw_phone, max_len=20)
            if 7 <= len(digits) <= 15:
                phone = f'+{digits}' if raw_phone.startswith('+') else digits
            else:
                phone = ''

            locked_session.is_escalated = True
            locked_session.status = ChatSessionStatus.ESCALATED
            locked_session.escalation_reason = cleaned_details
            locked_session.save(
                update_fields=['is_escalated', 'status', 'escalation_reason', 'updated_at']
            )
            _sync_session_attrs(session, locked_session)

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
                        status__in=[TicketStatus.OPEN, TicketStatus.IN_PROGRESS],
                    )
                    .order_by('-created_at', '-id')
                    .first()
                )
                if existing_ticket is not None:
                    return existing_ticket
                raise

            cache.set(cooldown_key, True, timeout=60)
            from support.emails import send_ticket_escalated_email

            schedule_on_commit(send_ticket_escalated_email, ticket)
            return ticket


def _sync_session_attrs(target: ChatSession, source: ChatSession) -> None:
    if target is source:
        return
    target.is_escalated = source.is_escalated
    target.status = source.status
    target.escalation_reason = source.escalation_reason
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
