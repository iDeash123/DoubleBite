import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import Count, F, Max, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce, Concat
from django.utils import timezone
from orders.models import Cart, Order
from orders.services import CartService, CartSummary

from support.models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    MessageRole,
    MessageSource,
    SupportTicket,
    TicketStatus,
)
from support.services import get_operator_display_name

DESK_TABS = ('waiting', 'in_progress', 'ai', 'resolved')
_OPEN_TICKET_STATUSES = (TicketStatus.OPEN, TicketStatus.IN_PROGRESS)
_INT32_MAX = 2**31 - 1


def normalize_desk_tab(raw_tab: str | None) -> str:
    cleaned = (raw_tab or '').strip()
    return cleaned if cleaned in DESK_TABS else 'waiting'


def resolve_default_tab_for_session(session: ChatSession) -> str:
    if session.status == ChatSessionStatus.ACTIVE:
        return 'ai'
    if session.status == ChatSessionStatus.RESOLVED:
        return 'resolved'
    if session.assigned_operator_id is not None:
        return 'in_progress'
    return 'waiting'


def format_session_customer_name(session: ChatSession) -> str:
    if session.user is not None:
        full_name = str(session.user.get_full_name() or '').strip()
        return full_name or session.user.email
    token_preview = session.guest_token[:8] if session.guest_token else 'anon'
    return f'Гість ({token_preview})'


def format_waiting_duration(
    since: datetime | None,
    now: datetime | None = None,
) -> str:
    if since is None:
        return '—'
    current = timezone.now() if now is None else now
    delta_seconds = max(0, int((current - since).total_seconds()))
    minutes = delta_seconds // 60
    if minutes < 1:
        return 'щойно'
    if minutes < 60:
        return f'{minutes} хв'
    hours, rem_minutes = divmod(minutes, 60)
    if hours < 24:
        return f'{hours} год {rem_minutes} хв' if rem_minutes else f'{hours} год'
    days, rem_hours = divmod(hours, 24)
    return f'{days} д {rem_hours} год' if rem_hours else f'{days} д'


def _to_localtime(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return timezone.localtime(dt) if timezone.is_aware(dt) else dt


def _truncate_preview(text: str, limit: int = 90) -> str:
    normalized = ' '.join((text or '').split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + '…'


@dataclass(frozen=True)
class DeskQueueItem:
    session: ChatSession
    session_uuid: str
    customer_name: str
    is_guest: bool
    ticket: SupportTicket | None
    ticket_id: int | None
    reason: str | None
    reason_display: str | None
    last_message_preview: str
    last_activity_at: datetime | None
    waiting_since: datetime | None
    waiting_label: str
    unread_count: int
    owner_label: str
    is_owned_by_me: bool
    is_active_selected: bool


@dataclass(frozen=True)
class DeskMessageView:
    id: int
    role: str
    sender: str
    sender_label: str
    operator_name: str | None
    source: str
    source_badge: str
    content: str
    created_at_str: str
    show_divider_before: bool


@dataclass(frozen=True)
class PastSessionSummary:
    session: ChatSession
    session_uuid: str
    date_label: str
    ticket: SupportTicket | None
    reason_display: str
    status_display: str
    message_count: int
    preview: str


def get_desk_tab_counts() -> dict[str, int]:
    counts = ChatSession.objects.aggregate(
        waiting=Count(
            'pk',
            filter=Q(
                status=ChatSessionStatus.ESCALATED,
                assigned_operator__isnull=True,
            ),
        ),
        in_progress=Count(
            'pk',
            filter=Q(
                status=ChatSessionStatus.ESCALATED,
                assigned_operator__isnull=False,
            ),
        ),
        ai=Count(
            'pk',
            filter=Q(status=ChatSessionStatus.ACTIVE),
        ),
        resolved=Count(
            'pk',
            filter=Q(status=ChatSessionStatus.RESOLVED),
        ),
    )
    return {
        'waiting': int(counts.get('waiting') or 0),
        'in_progress': int(counts.get('in_progress') or 0),
        'ai': int(counts.get('ai') or 0),
        'resolved': int(counts.get('resolved') or 0),
    }


def _apply_desk_search(qs: Any, query: str) -> Any:
    cleaned = (query or '').strip()
    if not cleaned:
        return qs

    qs = qs.annotate(
        _search_full_name=Concat(
            Coalesce('user__first_name', Value('')),
            Value(' '),
            Coalesce('user__last_name', Value('')),
        )
    )

    matching_guest_orders = (
        Order.objects.filter(user__isnull=True)
        .exclude(guest_token='')
        .filter(
            Q(order_number__icontains=cleaned)
            | Q(customer_phone__icontains=cleaned)
            | Q(customer_name__icontains=cleaned)
            | Q(customer_email__icontains=cleaned)
        )
        .values('guest_token')
    )

    search_q = (
        Q(user__email__icontains=cleaned)
        | Q(user__first_name__icontains=cleaned)
        | Q(user__last_name__icontains=cleaned)
        | Q(user__isnull=False, _search_full_name__icontains=cleaned)
        | Q(user__phone__icontains=cleaned)
        | (Q(user__isnull=True) & ~Q(guest_token='') & Q(guest_token__icontains=cleaned))
        | (Q(user__isnull=True) & ~Q(guest_token='') & Q(guest_token__in=matching_guest_orders))
        | Q(escalation_reason__icontains=cleaned)
        | Q(tickets__customer_phone__icontains=cleaned)
        | Q(tickets__details__icontains=cleaned)
        | Q(tickets__order__order_number__icontains=cleaned)
        | Q(user__orders__order_number__icontains=cleaned)
        | Q(user__orders__customer_phone__icontains=cleaned)
        | Q(user__orders__customer_name__icontains=cleaned)
        | Q(user__orders__customer_email__icontains=cleaned)
        | Q(messages__content__icontains=cleaned)
    )

    numeric_part = cleaned.lstrip('#').strip()
    if numeric_part.isascii() and numeric_part.isdigit():
        ticket_id = int(numeric_part)
        if 1 <= ticket_id <= _INT32_MAX:
            search_q |= Q(tickets__id=ticket_id)

    return qs.filter(search_q).distinct()


def get_desk_queue(
    *,
    tab: str = 'waiting',
    query: str = '',
    current_operator: Any = None,
    active_session_uuid: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    normalized_tab = normalize_desk_tab(tab)
    cleaned_query = (query or '').strip()
    counts = get_desk_tab_counts()

    base_qs = ChatSession.objects.select_related('user', 'assigned_operator')
    if normalized_tab == 'waiting':
        open_ticket_created = Subquery(
            SupportTicket.objects.filter(
                session=OuterRef('pk'),
                status__in=_OPEN_TICKET_STATUSES,
            )
            .order_by('-created_at', '-id')
            .values('created_at')[:1]
        )
        qs = (
            base_qs.filter(
                status=ChatSessionStatus.ESCALATED,
                assigned_operator__isnull=True,
            )
            .annotate(
                waiting_anchor=Coalesce(
                    open_ticket_created,
                    'last_message_at',
                    'created_at',
                )
            )
            .order_by('waiting_anchor', 'created_at', 'pk')
        )
    elif normalized_tab == 'in_progress':
        qs = (
            base_qs.filter(
                status=ChatSessionStatus.ESCALATED,
                assigned_operator__isnull=False,
            )
            .annotate(
                activity_anchor=Coalesce(
                    'last_message_at',
                    'assigned_at',
                    'updated_at',
                    'created_at',
                )
            )
            .order_by('-activity_anchor', '-created_at', 'pk')
        )
    elif normalized_tab == 'ai':
        qs = (
            base_qs.filter(status=ChatSessionStatus.ACTIVE)
            .annotate(
                activity_anchor=Coalesce(
                    'last_message_at',
                    'updated_at',
                    'created_at',
                )
            )
            .order_by('-activity_anchor', '-created_at', 'pk')
        )
    else:
        qs = (
            base_qs.filter(status=ChatSessionStatus.RESOLVED)
            .annotate(
                activity_anchor=Coalesce(
                    'last_message_at',
                    'updated_at',
                    'created_at',
                )
            )
            .order_by('-activity_anchor', '-created_at', 'pk')
        )

    qs = _apply_desk_search(qs, cleaned_query)
    limit = 50 if normalized_tab == 'resolved' else 100
    sessions = list(qs[:limit])

    session_pks = [s.session_uuid for s in sessions]
    active_ticket_map: dict[uuid.UUID, SupportTicket] = {}
    latest_ticket_map: dict[uuid.UUID, SupportTicket] = {}
    last_msg_map: dict[uuid.UUID, ChatMessage] = {}
    unread_map: dict[uuid.UUID, int] = {}

    if session_pks:
        tickets = (
            SupportTicket.objects.filter(session_id__in=session_pks)
            .select_related('order', 'assigned_operator')
            .order_by('-created_at', '-id')
        )
        for t in tickets:
            if t.session_id not in latest_ticket_map:
                latest_ticket_map[t.session_id] = t
            if t.status in _OPEN_TICKET_STATUSES and t.session_id not in active_ticket_map:
                active_ticket_map[t.session_id] = t

        last_msg_ids = [
            row['last_id']
            for row in ChatMessage.objects.filter(session_id__in=session_pks)
            .exclude(role=MessageRole.TOOL)
            .values('session_id')
            .annotate(last_id=Max('id'))
            if row['last_id'] is not None
        ]
        if last_msg_ids:
            for msg in ChatMessage.objects.filter(id__in=last_msg_ids):
                last_msg_map[msg.session_id] = msg

        unread_rows = (
            ChatMessage.objects.filter(
                session_id__in=session_pks,
                role=MessageRole.USER,
                id__gt=F('session__operator_last_read_id'),
            )
            .values('session_id')
            .annotate(cnt=Count('id'))
        )
        for row in unread_rows:
            unread_map[row['session_id']] = int(row['cnt'] or 0)

    now = timezone.now()
    active_uuid_str = str(active_session_uuid) if active_session_uuid else ''
    current_op_id = getattr(current_operator, 'id', None)

    items: list[DeskQueueItem] = []
    for s in sessions:
        active_ticket = active_ticket_map.get(s.session_uuid)
        latest_ticket = latest_ticket_map.get(s.session_uuid)
        ticket = active_ticket or latest_ticket
        last_msg = last_msg_map.get(s.session_uuid)
        last_activity = _to_localtime(
            s.last_message_at or s.updated_at or s.created_at
        )
        waiting_since = (
            active_ticket.created_at
            if active_ticket is not None
            else (s.last_message_at or s.created_at)
        )
        is_owned_by_me = bool(
            current_op_id is not None and s.assigned_operator_id == current_op_id
        )
        if s.assigned_operator_id is None:
            owner_label = ''
        elif is_owned_by_me:
            owner_label = 'Ви'
        else:
            owner_label = get_operator_display_name(s.assigned_operator)

        unread_count = (
            unread_map.get(s.session_uuid, 0)
            if s.status == ChatSessionStatus.ESCALATED
            else 0
        )

        items.append(
            DeskQueueItem(
                session=s,
                session_uuid=str(s.session_uuid),
                customer_name=format_session_customer_name(s),
                is_guest=s.user_id is None,
                ticket=ticket,
                ticket_id=ticket.id if ticket else None,
                reason=ticket.reason if ticket else None,
                reason_display=ticket.get_reason_display() if ticket else None,
                last_message_preview=(
                    _truncate_preview(last_msg.content)
                    if last_msg is not None
                    else 'Немає повідомлень'
                ),
                last_activity_at=last_activity,
                waiting_since=waiting_since,
                waiting_label=format_waiting_duration(waiting_since, now),
                unread_count=unread_count,
                owner_label=owner_label,
                is_owned_by_me=is_owned_by_me,
                is_active_selected=(
                    bool(active_uuid_str) and str(s.session_uuid) == active_uuid_str
                ),
            )
        )

    return {
        'tab': normalized_tab,
        'query': cleaned_query,
        'counts': counts,
        'items': items,
    }


def _resolve_sender_kind(role: str) -> str:
    if role == MessageRole.USER:
        return 'user'
    if role == MessageRole.OPERATOR:
        return 'operator'
    if role == MessageRole.SYSTEM:
        return 'system'
    return 'ai'


def serialize_desk_message(
    msg: ChatMessage,
    *,
    customer_name: str,
    show_divider_before: bool = False,
) -> DeskMessageView:
    created_local = _to_localtime(msg.created_at)
    created_str = created_local.strftime('%H:%M') if created_local else ''
    sender = _resolve_sender_kind(msg.role)
    operator_name: str | None = None
    source_badge = ''
    if msg.role == MessageRole.OPERATOR:
        operator_name = (
            get_operator_display_name(msg.sender_user)
            if msg.sender_user is not None
            else 'Адміністратор ресторану'
        )
        sender_label = operator_name
        source_badge = 'TG' if msg.source == MessageSource.TELEGRAM else 'Web'
    elif msg.role == MessageRole.USER:
        sender_label = customer_name
    elif msg.role == MessageRole.SYSTEM:
        sender_label = 'Система'
    else:
        sender_label = 'AI-консьєрж'

    return DeskMessageView(
        id=msg.id,
        role=msg.role,
        sender=sender,
        sender_label=sender_label,
        operator_name=operator_name,
        source=msg.source,
        source_badge=source_badge,
        content=msg.content,
        created_at_str=created_str,
        show_divider_before=show_divider_before,
    )


def _get_recent_orders_for_session(session: ChatSession) -> list[Order]:
    if session.user_id is not None:
        qs = Order.objects.filter(user_id=session.user_id)
    elif session.guest_token:
        qs = Order.objects.filter(
            user__isnull=True,
            guest_token=session.guest_token,
        )
    else:
        return []
    return list(qs.prefetch_related('items').order_by('-created_at')[:5])


def _get_cart_summary_for_session(session: ChatSession) -> CartSummary:
    cart: Cart | None = None
    if session.user_id is not None:
        cart = Cart.objects.filter(user_id=session.user_id).first()
    elif session.guest_token:
        cart = Cart.objects.filter(
            user__isnull=True,
            guest_token=session.guest_token,
        ).first()
    return CartService.summary(cart)


def _get_past_sessions_for_session(session: ChatSession) -> list[PastSessionSummary]:
    if session.user_id is not None:
        past_qs = ChatSession.objects.filter(
            user_id=session.user_id,
            status=ChatSessionStatus.RESOLVED,
        ).exclude(pk=session.pk)
    elif session.guest_token:
        past_qs = ChatSession.objects.filter(
            user__isnull=True,
            guest_token=session.guest_token,
            status=ChatSessionStatus.RESOLVED,
        ).exclude(pk=session.pk)
    else:
        return []

    past_sessions = list(past_qs.order_by('-updated_at', '-created_at')[:10])
    if not past_sessions:
        return []

    past_pks = [s.session_uuid for s in past_sessions]
    ticket_map: dict[uuid.UUID, SupportTicket] = {}
    for t in SupportTicket.objects.filter(session_id__in=past_pks).order_by(
        '-created_at', '-id'
    ):
        if t.session_id not in ticket_map:
            ticket_map[t.session_id] = t

    count_map: dict[uuid.UUID, int] = {}
    last_id_map: dict[uuid.UUID, int] = {}
    for row in (
        ChatMessage.objects.filter(session_id__in=past_pks)
        .exclude(role=MessageRole.TOOL)
        .values('session_id')
        .annotate(cnt=Count('id'), last_id=Max('id'))
    ):
        count_map[row['session_id']] = int(row['cnt'] or 0)
        if row['last_id'] is not None:
            last_id_map[row['session_id']] = row['last_id']

    last_msg_map: dict[uuid.UUID, ChatMessage] = {}
    if last_id_map:
        for msg in ChatMessage.objects.filter(id__in=list(last_id_map.values())):
            last_msg_map[msg.session_id] = msg

    summaries: list[PastSessionSummary] = []
    for past in past_sessions:
        ticket = ticket_map.get(past.session_uuid)
        last_msg = last_msg_map.get(past.session_uuid)
        dt = _to_localtime(past.last_message_at or past.updated_at or past.created_at)
        date_label = dt.strftime('%d.%m.%Y %H:%M') if dt else ''
        summaries.append(
            PastSessionSummary(
                session=past,
                session_uuid=str(past.session_uuid),
                date_label=date_label,
                ticket=ticket,
                reason_display=(
                    ticket.get_reason_display() if ticket is not None else 'AI-чат'
                ),
                status_display=(
                    ticket.get_status_display()
                    if ticket is not None
                    else past.get_status_display()
                ),
                message_count=count_map.get(past.session_uuid, 0),
                preview=(
                    _truncate_preview(last_msg.content, limit=80)
                    if last_msg is not None
                    else 'Немає повідомлень'
                ),
            )
        )
    return summaries


def get_desk_session_context(
    session: ChatSession,
    *,
    current_operator: Any = None,
) -> dict[str, Any]:
    customer_name = format_session_customer_name(session)
    tickets = list(
        session.tickets.select_related('order', 'assigned_operator').order_by(
            '-created_at', '-id'
        )
    )
    active_ticket = next(
        (t for t in tickets if t.status in _OPEN_TICKET_STATUSES),
        None,
    )
    latest_ticket = active_ticket or (tickets[0] if tickets else None)

    raw_messages = list(
        session.messages.exclude(role=MessageRole.TOOL)
        .select_related('sender_user')
        .order_by('created_at', 'id')
    )

    divider_msg_id: int | None = None
    escalation_time_label = ''
    if latest_ticket is not None:
        local_ticket_dt = _to_localtime(latest_ticket.created_at)
        escalation_time_label = (
            local_ticket_dt.strftime('%H:%M') if local_ticket_dt else ''
        )
        ticket_marker = f'#{latest_ticket.id}'
        for m in raw_messages:
            if m.role == MessageRole.SYSTEM and ticket_marker in m.content:
                divider_msg_id = m.id
                break
        if divider_msg_id is None:
            for m in raw_messages:
                if m.created_at >= latest_ticket.created_at:
                    divider_msg_id = m.id
                    break

    messages = [
        serialize_desk_message(
            m,
            customer_name=customer_name,
            show_divider_before=(divider_msg_id is not None and m.id == divider_msg_id),
        )
        for m in raw_messages
    ]
    show_escalation_divider_at_end = bool(
        latest_ticket is not None and divider_msg_id is None
    )
    last_message_id = max((m.id for m in raw_messages), default=0)

    recent_orders = _get_recent_orders_for_session(session)
    cart_summary = _get_cart_summary_for_session(session)
    past_sessions = _get_past_sessions_for_session(session)

    user = session.user
    email = (
        user.email
        if user is not None
        else next((o.customer_email for o in recent_orders if o.customer_email), '')
    )
    phone = (
        (user.phone if user is not None and user.phone else '')
        or (
            latest_ticket.customer_phone
            if latest_ticket is not None and latest_ticket.customer_phone
            else ''
        )
        or next((o.customer_phone for o in recent_orders if o.customer_phone), '')
    )
    registered_at = ''
    if user is not None:
        reg_dt = getattr(user, 'created_at', None) or getattr(user, 'date_joined', None)
        local_reg = _to_localtime(reg_dt)
        if local_reg is not None:
            registered_at = local_reg.strftime('%d.%m.%Y')

    guest_order_name = (
        next((o.customer_name for o in recent_orders if o.customer_name), '')
        if user is None
        else ''
    )

    current_op_id = getattr(current_operator, 'id', None)
    is_owned_by_me = bool(
        current_op_id is not None and session.assigned_operator_id == current_op_id
    )
    assigned_operator_name = (
        get_operator_display_name(session.assigned_operator)
        if session.assigned_operator is not None
        else ''
    )
    can_reply = bool(
        session.status == ChatSessionStatus.ESCALATED and is_owned_by_me
    )

    return {
        'session': session,
        'session_uuid': str(session.session_uuid),
        'customer': {
            'name': customer_name,
            'order_name': guest_order_name,
            'email': email or 'Не вказано',
            'phone': phone or 'Не вказано',
            'is_registered': user is not None,
            'registered_at': registered_at,
        },
        'active_ticket': active_ticket,
        'ticket': latest_ticket,
        'ticket_waiting_label': (
            format_waiting_duration(active_ticket.created_at)
            if active_ticket is not None
            else ''
        ),
        'messages': messages,
        'last_message_id': last_message_id,
        'escalation_time_label': escalation_time_label,
        'show_escalation_divider_at_end': show_escalation_divider_at_end,
        'recent_orders': recent_orders,
        'cart_summary': cart_summary,
        'past_sessions': past_sessions,
        'is_owned_by_me': is_owned_by_me,
        'assigned_operator_name': assigned_operator_name,
        'can_reply': can_reply,
    }


def get_past_session_for_desk(
    past_session_uuid: uuid.UUID,
    *,
    current_session_uuid: uuid.UUID,
) -> dict[str, Any] | None:
    if current_session_uuid is None:
        return None

    past_session = (
        ChatSession.objects.select_related('user', 'assigned_operator')
        .filter(
            session_uuid=past_session_uuid,
            status=ChatSessionStatus.RESOLVED,
        )
        .first()
    )
    if past_session is None:
        return None

    current_session = ChatSession.objects.filter(
        session_uuid=current_session_uuid
    ).first()
    if current_session is None or current_session.pk == past_session.pk:
        return None
    if current_session.user_id is not None:
        if past_session.user_id != current_session.user_id:
            return None
    elif current_session.guest_token:
        if (
            past_session.user_id is not None
            or past_session.guest_token != current_session.guest_token
        ):
            return None
    else:
        return None

    customer_name = format_session_customer_name(past_session)
    ticket = (
        past_session.tickets.select_related('order', 'assigned_operator')
        .order_by('-created_at', '-id')
        .first()
    )
    raw_messages = list(
        past_session.messages.exclude(role=MessageRole.TOOL)
        .select_related('sender_user')
        .order_by('created_at', 'id')
    )
    messages = [
        serialize_desk_message(m, customer_name=customer_name)
        for m in raw_messages
    ]
    dt = _to_localtime(
        past_session.last_message_at
        or past_session.updated_at
        or past_session.created_at
    )
    return {
        'past_session': past_session,
        'ticket': ticket,
        'date_label': dt.strftime('%d.%m.%Y %H:%M') if dt else '',
        'messages': messages,
    }
