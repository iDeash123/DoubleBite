from config.transactions import schedule_on_commit
from django.contrib import admin
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from support.models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    FAQKnowledge,
    SupportTicket,
    TicketStatus,
)
from support.realtime import publish_desk_event, publish_session_event
from support.services import _sqlite_support_guard


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 0
    fields = ('role', 'content', 'created_at')
    readonly_fields = ('created_at',)
    can_delete = False


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = (
        'session_uuid',
        'user',
        'status',
        'is_escalated',
        'assigned_operator',
        'created_at',
        'desk_link',
    )
    list_filter = ('status', 'is_escalated', 'created_at')
    search_fields = ('session_uuid', 'user__email', 'escalation_reason')
    list_select_related = ('user', 'assigned_operator')
    readonly_fields = ('session_uuid', 'created_at', 'updated_at')
    exclude = ('guest_token',)
    inlines = (ChatMessageInline,)

    @admin.display(description='Пульт підтримки')
    def desk_link(self, obj: ChatSession) -> str:
        url = reverse('support:desk_session', args=[obj.session_uuid])
        return format_html('<a href="{}">Відкрити чат у пульті</a>', url)


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'session', 'role', 'content_snippet', 'created_at')
    list_filter = ('role', 'created_at')
    search_fields = ('content', 'session__session_uuid')
    readonly_fields = ('created_at',)

    def content_snippet(self, obj: ChatMessage) -> str:
        return obj.content[:50] + '...' if len(obj.content) > 50 else obj.content

    content_snippet.short_description = 'Вміст'


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'session',
        'order',
        'customer_phone',
        'reason',
        'status',
        'assigned_operator',
        'created_at',
        'resolved_at',
        'desk_link',
    )
    list_filter = ('status', 'reason', 'created_at')
    list_select_related = ('session', 'order', 'assigned_operator')
    search_fields = (
        'details',
        'customer_phone',
        'order__order_number',
        'session__session_uuid',
    )
    readonly_fields = ('created_at',)
    actions = ('mark_in_progress', 'mark_resolved')

    @admin.display(description='Пульт підтримки')
    def desk_link(self, obj: SupportTicket) -> str:
        url = reverse('support:desk_session', args=[obj.session_id])
        return format_html('<a href="{}">Відкрити чат у пульті</a>', url)

    @admin.action(description='Перевести обрані тікети в статус "В обробці"')
    def mark_in_progress(self, request, queryset):
        queryset.update(status=TicketStatus.IN_PROGRESS)

    @admin.action(description='Позначити обрані тікети як "Вирішено"')
    def mark_resolved(self, request, queryset):
        now = timezone.now()
        with _sqlite_support_guard(), transaction.atomic():
            ticket_rows = list(queryset.values_list('pk', 'session_id'))
            if not ticket_rows:
                return
            ticket_ids = sorted({pk for pk, _ in ticket_rows})
            session_ids = sorted({sid for _, sid in ticket_rows if sid is not None})

            if session_ids:
                list(
                    ChatSession.objects.select_for_update()
                    .filter(pk__in=session_ids)
                    .order_by('pk')
                )
                list(
                    SupportTicket.objects.select_for_update()
                    .filter(session_id__in=session_ids)
                    .order_by('pk')
                )
            else:
                list(
                    SupportTicket.objects.select_for_update()
                    .filter(pk__in=ticket_ids)
                    .order_by('pk')
                )

            open_ticket_ids = list(
                SupportTicket.objects.filter(
                    pk__in=ticket_ids,
                    status__in=[TicketStatus.OPEN, TicketStatus.IN_PROGRESS],
                ).values_list('pk', 'telegram_thread_id')
            )
            SupportTicket.objects.filter(
                pk__in=ticket_ids,
                status__in=[TicketStatus.OPEN, TicketStatus.IN_PROGRESS],
            ).update(
                status=TicketStatus.RESOLVED,
                resolved_at=now,
            )
            from support.telegram import sync_ticket_closed_to_telegram

            for closed_ticket_id, thread_id in open_ticket_ids:
                if thread_id is not None:
                    schedule_on_commit(
                        sync_ticket_closed_to_telegram,
                        closed_ticket_id,
                        outcome='admin_resolved',
                    )

            if session_ids:
                open_session_ids = set(
                    SupportTicket.objects.filter(
                        session_id__in=session_ids,
                        status__in=[TicketStatus.OPEN, TicketStatus.IN_PROGRESS],
                    ).values_list('session_id', flat=True)
                )
                sessions_to_resolve = [
                    sid for sid in session_ids if sid not in open_session_ids
                ]
                if sessions_to_resolve:
                    escalated_ids = list(
                        ChatSession.objects.filter(
                            pk__in=sessions_to_resolve,
                            status=ChatSessionStatus.ESCALATED,
                        ).values_list('pk', flat=True)
                    )
                    if escalated_ids:
                        ChatSession.objects.filter(pk__in=escalated_ids).update(
                            is_escalated=False,
                            status=ChatSessionStatus.RESOLVED,
                            assigned_operator=None,
                            assigned_at=None,
                            updated_at=now,
                        )
                        for sid in escalated_ids:
                            schedule_on_commit(
                                publish_session_event,
                                sid,
                                'session_resolved',
                                {
                                    'status': ChatSessionStatus.RESOLVED,
                                    'is_escalated': False,
                                    'operator_connected': False,
                                    'assigned_operator_id': None,
                                    'assigned_operator_name': None,
                                    'reason': 'admin_resolved',
                                },
                            )
                        schedule_on_commit(
                            publish_desk_event,
                            'queue_update',
                            {
                                'reason': 'admin_resolved',
                                'status': ChatSessionStatus.RESOLVED,
                            },
                        )


@admin.register(FAQKnowledge)
class FAQKnowledgeAdmin(admin.ModelAdmin):
    list_display = ('id', 'question', 'category', 'is_active', 'updated_at')
    list_filter = ('is_active', 'category')
    search_fields = ('question', 'answer', 'category')
