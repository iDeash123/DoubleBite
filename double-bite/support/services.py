import re
import uuid
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest
from orders.models import Order

from support.models import (
    ChatSession,
    ChatSessionStatus,
    FAQKnowledge,
    SupportTicket,
    TicketReason,
    TicketStatus,
)


class ChatSessionService:
    @staticmethod
    def get_or_create_active_session(request: HttpRequest) -> ChatSession:
        if not request.session.session_key:
            request.session.create()

        session_key = request.session.session_key or ''

        if request.user.is_authenticated:
            active_session = ChatSession.objects.filter(
                user=request.user,
                status__in=[ChatSessionStatus.ACTIVE, ChatSessionStatus.ESCALATED],
            ).first()

            if not active_session and session_key:
                guest_session = ChatSession.objects.filter(
                    session_key=session_key,
                    user__isnull=True,
                    status__in=[ChatSessionStatus.ACTIVE, ChatSessionStatus.ESCALATED],
                ).first()
                if guest_session:
                    guest_session.user = request.user
                    guest_session.session_key = ''
                    guest_session.save(update_fields=['user', 'session_key', 'updated_at'])
                    return guest_session

            if not active_session:
                active_session = ChatSession.objects.create(
                    user=request.user,
                    status=ChatSessionStatus.ACTIVE,
                )
            return active_session

        active_session = ChatSession.objects.filter(
            session_key=session_key,
            user__isnull=True,
            status__in=[ChatSessionStatus.ACTIVE, ChatSessionStatus.ESCALATED],
        ).first()

        if not active_session:
            active_session = ChatSession.objects.create(
                session_key=session_key,
                status=ChatSessionStatus.ACTIVE,
            )
        return active_session

    @staticmethod
    def get_session_for_request(
        request: HttpRequest,
        session_uuid: str | uuid.UUID,
    ) -> ChatSession | None:
        try:
            if isinstance(session_uuid, str):
                session_uuid = uuid.UUID(session_uuid)
        except (ValueError, TypeError, AttributeError):
            return None

        if request.user.is_authenticated:
            return ChatSession.objects.filter(
                session_uuid=session_uuid,
                user=request.user,
            ).first()

        session_key = request.session.session_key or ''
        if not session_key:
            return None

        return ChatSession.objects.filter(
            session_uuid=session_uuid,
            user__isnull=True,
            session_key=session_key,
        ).first()

    @staticmethod
    def reset_active_session(request: HttpRequest) -> ChatSession:
        if not request.session.session_key:
            request.session.create()

        session_key = request.session.session_key or ''

        with transaction.atomic():
            if request.user.is_authenticated:
                ChatSession.objects.filter(
                    user=request.user,
                    status__in=[ChatSessionStatus.ACTIVE, ChatSessionStatus.ESCALATED],
                ).update(status=ChatSessionStatus.RESOLVED)

                new_session = ChatSession.objects.create(
                    user=request.user,
                    status=ChatSessionStatus.ACTIVE,
                )
                return new_session

            ChatSession.objects.filter(
                session_key=session_key,
                user__isnull=True,
                status__in=[ChatSessionStatus.ACTIVE, ChatSessionStatus.ESCALATED],
            ).update(status=ChatSessionStatus.RESOLVED)

            new_session = ChatSession.objects.create(
                session_key=session_key,
                status=ChatSessionStatus.ACTIVE,
            )
            return new_session

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

        with transaction.atomic():
            session.is_escalated = True
            session.status = ChatSessionStatus.ESCALATED
            session.escalation_reason = details
            session.save(update_fields=['is_escalated', 'status', 'escalation_reason', 'updated_at'])

            phone = customer_phone.strip()
            if not phone and session.user and hasattr(session.user, 'phone'):
                phone = session.user.phone or ''
            if not phone and order:
                phone = order.customer_phone or ''

            ticket = SupportTicket.objects.create(
                session=session,
                order=order,
                customer_phone=phone,
                reason=normalized_reason,
                details=details,
                status=TicketStatus.OPEN,
            )
            return ticket


def validate_order_access(order: Order, request: HttpRequest) -> bool:
    if request.user.is_authenticated:
        return order.user_id == request.user.id

    session_key = request.session.session_key or ''
    if order.session_key and session_key and order.session_key == session_key:
        return True

    session_phone = request.session.get('guest_phone')
    return bool(session_phone and order.customer_phone and session_phone == order.customer_phone)


def check_order_status_for_request(
    request: HttpRequest,
    order_id: int | str,
) -> dict[str, Any]:
    cleaned_id = str(order_id).strip()
    digits = re.findall(r'\d+', cleaned_id)
    pk = int(digits[0]) if digits else None

    order = None
    if pk is not None:
        order = Order.objects.filter(pk=pk).first()

    if not order:
        order = Order.objects.filter(order_number__iexact=cleaned_id).first()

    if not order or not validate_order_access(order, request):
        return {'error': 'Замовлення не знайдено або доступ заборонено'}

    items_summary = [
        f"{item.dish_title} x {item.quantity}"
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
    cleaned_query = query.strip()
    if not cleaned_query:
        return []

    words = [w for w in cleaned_query.split() if len(w) > 2]
    q_filter = Q(question__icontains=cleaned_query) | Q(answer__icontains=cleaned_query)
    for word in words:
        q_filter |= Q(question__icontains=word) | Q(answer__icontains=word) | Q(category__icontains=word)

    faqs = FAQKnowledge.objects.filter(is_active=True).filter(q_filter).distinct()[:5]

    return [
        {
            'id': faq.id,
            'question': faq.question,
            'answer': faq.answer,
            'category': faq.category,
        }
        for faq in faqs
    ]


