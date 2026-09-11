from django.contrib import admin

from .models import Cart, CartItem, Order, OrderItem


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
        'total_amount',
        'created_at',
    )
    list_filter = ('status', 'payment_method', 'payment_status', 'created_at')
    search_fields = (
        'order_number',
        'customer_name',
        'customer_phone',
        'delivery_address',
        'stripe_session_id',
        'stripe_payment_intent_id',
    )
    readonly_fields = (
        'order_number',
        'stripe_session_id',
        'stripe_payment_intent_id',
        'total_amount',
        'created_at',
        'updated_at',
    )
    inlines = (OrderItemInline,)


class CartItemInline(admin.TabularInline):
    model = CartItem
    extra = 0
    readonly_fields = ('unit_price', 'total_price')


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'session_key', 'total_quantity', 'total_amount', 'updated_at')
    search_fields = ('user__email', 'session_key')
    readonly_fields = ('created_at', 'updated_at')
    inlines = (CartItemInline,)
