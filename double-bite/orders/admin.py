from django.contrib import admin

from .models import (
    Cart,
    CartItem,
    InvalidStatusTransitionError,
    Order,
    OrderItem,
    OrderStatus,
    ProcessedStripeEvent,
)


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ('dish_title', 'price', 'quantity', 'total_price', 'selected_options')
    can_delete = False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        'order_number',
        'customer_name',
        'customer_phone',
        'status',
        'payment_method',
        'payment_status',
        'requires_manual_refund',
        'total_amount',
        'created_at',
    )
    list_filter = ('status', 'payment_method', 'payment_status', 'requires_manual_refund', 'created_at')
    search_fields = (
        'order_number',
        'customer_name',
        'customer_phone',
        'delivery_address',
        'stripe_session_id',
        'stripe_payment_intent_id',
    )
    exclude = ('session_key', 'access_token')
    readonly_fields = (
        'order_number',
        'status',
        'payment_status',
        'stripe_session_id',
        'stripe_payment_intent_id',
        'total_amount',
        'created_at',
        'updated_at',
    )
    actions = [
        'action_mark_paid',
        'action_mark_preparing',
        'action_mark_on_way',
        'action_mark_delivered',
        'action_mark_cancelled',
    ]
    inlines = (OrderItemInline,)

    def _apply_transition(self, request, queryset, target_status):
        success_count = 0
        error_count = 0
        for order in queryset:
            try:
                order.transition_to(target_status)
                success_count += 1
            except InvalidStatusTransitionError:
                error_count += 1
        if request:
            if success_count:
                self.message_user(request, f"Успішно змінено статус {success_count} замовлень на {target_status}.")
            if error_count:
                self.message_user(
                    request,
                    f"Не вдалося змінити статус для {error_count} замовлень через неприпустимий перехід.",
                    level='warning',
                )

    @admin.action(description='Перевести у статус: Оплачено')
    def action_mark_paid(self, request, queryset):
        self._apply_transition(request, queryset, OrderStatus.PAID)

    @admin.action(description='Перевести у статус: Готується')
    def action_mark_preparing(self, request, queryset):
        self._apply_transition(request, queryset, OrderStatus.PREPARING)

    @admin.action(description='Перевести у статус: В дорозі')
    def action_mark_on_way(self, request, queryset):
        self._apply_transition(request, queryset, OrderStatus.ON_WAY)

    @admin.action(description='Перевести у статус: Доставлено')
    def action_mark_delivered(self, request, queryset):
        self._apply_transition(request, queryset, OrderStatus.DELIVERED)

    @admin.action(description='Перевести у статус: Скасовано')
    def action_mark_cancelled(self, request, queryset):
        self._apply_transition(request, queryset, OrderStatus.CANCELLED)


@admin.register(ProcessedStripeEvent)
class ProcessedStripeEventAdmin(admin.ModelAdmin):
    list_display = ('event_id', 'event_type', 'created_at')
    search_fields = ('event_id', 'event_type')
    readonly_fields = ('event_id', 'event_type', 'created_at')


class CartItemInline(admin.TabularInline):
    model = CartItem
    extra = 0
    readonly_fields = ('unit_price', 'total_price')


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'total_quantity', 'total_amount', 'updated_at')
    search_fields = ('user__email',)
    exclude = ('session_key',)
    readonly_fields = ('created_at', 'updated_at')
    inlines = (CartItemInline,)
