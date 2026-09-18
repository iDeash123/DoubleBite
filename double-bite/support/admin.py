from django.contrib import admin
from django.utils import timezone

from support.models import (
    ChatMessage,
    ChatSession,
    FAQKnowledge,
    SupportTicket,
    TicketStatus,
)


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
        'session_key',
        'status',
        'is_escalated',
        'created_at',
    )
    list_filter = ('status', 'is_escalated', 'created_at')
    search_fields = ('session_uuid', 'user__email', 'session_key', 'escalation_reason')
    list_select_related = ('user',)
    readonly_fields = ('session_uuid', 'created_at', 'updated_at')
    inlines = (ChatMessageInline,)


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
        'created_at',
        'resolved_at',
    )
    list_filter = ('status', 'reason', 'created_at')
    list_select_related = ('session', 'order')
    search_fields = (
        'details',
        'customer_phone',
        'order__order_number',
        'session__session_uuid',
    )
    readonly_fields = ('created_at',)
    actions = ('mark_in_progress', 'mark_resolved')

    @admin.action(description='Перевести обрані тікети в статус "В обробці"')
    def mark_in_progress(self, request, queryset):
        queryset.update(status=TicketStatus.IN_PROGRESS)

    @admin.action(description='Позначити обрані тікети як "Вирішено"')
    def mark_resolved(self, request, queryset):
        queryset.update(status=TicketStatus.RESOLVED, resolved_at=timezone.now())


@admin.register(FAQKnowledge)
class FAQKnowledgeAdmin(admin.ModelAdmin):
    list_display = ('id', 'question', 'category', 'is_active', 'updated_at')
    list_filter = ('is_active', 'category')
    search_fields = ('question', 'answer', 'category')
