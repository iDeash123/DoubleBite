import os
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import Role
from config.partials import render_partial_or_full
from .exceptions import (
    CartEmptyError,
    DishUnavailableError,
    InvalidStatusTransitionError,
    OrderMinimumAmountError,
)
from .forms import OrderCheckoutForm
from .models import Order, OrderStatus, PaymentMethod
from .services import CartService, OrderService


def cart_view(request: HttpRequest) -> HttpResponse:
    cart = CartService.get_cart(request)
    items = list(cart.items.select_related('dish', 'dish__category').all()) if cart else []
    total_amount = cart.total_amount if cart else Decimal('0.00')
    total_quantity = cart.total_quantity if cart else 0

    min_order_amount = Decimal(os.getenv('MIN_ORDER_AMOUNT', '200'))
    meets_min_amount = (total_amount >= min_order_amount)
    min_diff = max(Decimal('0.00'), min_order_amount - total_amount)

    context = {
        'cart': cart,
        'items': items,
        'total_amount': total_amount,
        'total_quantity': total_quantity,
        'min_order_amount': min_order_amount,
        'meets_min_amount': meets_min_amount,
        'min_diff': min_diff,
    }

    if request.headers.get('HX-Request'):
        return render_partial_or_full(request, 'orders/cart.html#cart-content', context)

    return render_partial_or_full(request, 'orders/cart.html', context)


@require_POST
def cart_add_view(request: HttpRequest, dish_id: int) -> HttpResponse:
    if request.user.is_authenticated and getattr(request.user, 'role', None) in (
        Role.RESTAURANT_ADMIN,
        Role.COURIER,
    ):
        return HttpResponseForbidden('Персонал ресторану не може замовляти страви через клієнтський кошик.')

    quantity_raw = request.POST.get('quantity', '1')
    try:
        quantity = int(quantity_raw)
    except (ValueError, TypeError):
        quantity = 1

    option_id_raw = request.POST.get('option_id', '')
    option_id = int(option_id_raw) if option_id_raw.isdigit() else None

    try:
        CartService.add_dish(
            request,
            dish_id=dish_id,
            quantity=quantity,
            option_id=option_id,
        )
    except DishUnavailableError as e:
        if request.headers.get('HX-Request'):
            return HttpResponse(str(e), status=400)
        messages.error(request, str(e))
        referer = request.META.get('HTTP_REFERER')
        return redirect(referer or 'menu:catalog')

    if request.headers.get('HX-Request'):
        count = CartService.get_items_count(request)
        return render(request, 'partials/cart_badge.html', {'cart_items_count': count})

    referer = request.META.get('HTTP_REFERER')
    return redirect(referer or 'menu:catalog')


@require_POST
def cart_update_view(request: HttpRequest, item_id: int) -> HttpResponse:
    quantity_raw = request.POST.get('quantity', '1')
    try:
        quantity = int(quantity_raw)
    except (ValueError, TypeError):
        quantity = 1

    CartService.update_item_quantity(request, item_id=item_id, quantity=quantity)

    if request.headers.get('HX-Request'):
        if 'cart-drawer' in request.headers.get('HX-Target', ''):
            return cart_drawer_view(request)
        return cart_view(request)

    return redirect('orders:cart')


@require_POST
def cart_remove_view(request: HttpRequest, item_id: int) -> HttpResponse:
    CartService.remove_item(request, item_id=item_id)

    if request.headers.get('HX-Request'):
        if 'cart-drawer' in request.headers.get('HX-Target', ''):
            return cart_drawer_view(request)
        return cart_view(request)

    return redirect('orders:cart')


@require_POST
def cart_clear_view(request: HttpRequest) -> HttpResponse:
    CartService.clear_cart(request)

    if request.headers.get('HX-Request'):
        if 'cart-drawer' in request.headers.get('HX-Target', ''):
            return cart_drawer_view(request)
        return cart_view(request)

    return redirect('orders:cart')



def checkout_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated and getattr(request.user, 'role', None) in (
        Role.RESTAURANT_ADMIN,
        Role.COURIER,
    ):
        return HttpResponseForbidden('Персонал ресторану не може оформлювати клієнтські замовлення.')

    cart = CartService.get_cart(request)
    if not cart or not cart.items.exists():
        messages.warning(request, 'Кошик порожній. Додайте страви для оформлення замовлення.')
        return redirect('orders:cart')

    total_amount = cart.total_amount
    total_quantity = cart.total_quantity
    min_order_amount = Decimal(os.getenv('MIN_ORDER_AMOUNT', '200'))

    if total_amount < min_order_amount:
        messages.warning(
            request,
            f'Мінімальна сума замовлення — {min_order_amount} грн. Додайте ще страв до кошика.',
        )
        return redirect('orders:cart')

    items = list(cart.items.select_related('dish', 'dish__category').all())

    if request.method == 'POST':
        form = OrderCheckoutForm(request.POST)
        if form.is_valid():
            try:
                order = OrderService.create_order_from_cart(
                    request=request,
                    customer_name=form.cleaned_data['customer_name'],
                    customer_phone=form.cleaned_data['customer_phone'],
                    delivery_address=form.cleaned_data['delivery_address'],
                    payment_method=form.cleaned_data['payment_method'],
                    notes=form.cleaned_data.get('notes', ''),
                )
                messages.success(request, f'Замовлення {order.order_number} успішно оформлено!')
                return redirect('orders:tracking', order_number=order.order_number)
            except (CartEmptyError, OrderMinimumAmountError, ValidationError) as e:
                form.add_error(None, str(e))
    else:
        initial_data = {}
        if request.user.is_authenticated:
            full_name = f"{request.user.first_name} {request.user.last_name}".strip()
            initial_data['customer_name'] = full_name or request.user.email.split('@')[0]
            if hasattr(request.user, 'phone') and request.user.phone:
                initial_data['customer_phone'] = request.user.phone
            default_address = request.user.addresses.filter(is_default=True).first()
            if default_address:
                initial_data['delivery_address'] = (
                    f"{default_address.city}, {default_address.street} {default_address.building}, "
                    f"кв. {default_address.apartment}"
                )
        initial_data['payment_method'] = PaymentMethod.CARD
        form = OrderCheckoutForm(initial=initial_data)

    context = {
        'form': form,
        'cart': cart,
        'items': items,
        'total_amount': total_amount,
        'total_quantity': total_quantity,
    }
    return render(request, 'orders/checkout.html', context)


def _user_can_cancel_order(request: HttpRequest, order: Order) -> bool:
    if order.status not in (OrderStatus.PENDING, OrderStatus.PAID):
        return False
    if request.user.is_authenticated:
        return bool(
            order.user_id == request.user.id
            or getattr(request.user, 'role', None) == Role.RESTAURANT_ADMIN
        )
    if order.session_key and request.session.session_key:
        return bool(order.user_id is None and order.session_key == request.session.session_key)
    return False


def tracking_view(request: HttpRequest, order_number: str) -> HttpResponse:
    order = get_object_or_404(Order.objects.prefetch_related('items'), order_number=order_number)
    can_cancel = _user_can_cancel_order(request, order)
    return render(request, 'orders/tracking.html', {'order': order, 'can_cancel': can_cancel})


def tracking_status_view(request: HttpRequest, order_number: str) -> HttpResponse:
    order = get_object_or_404(Order.objects.prefetch_related('items'), order_number=order_number)
    can_cancel = _user_can_cancel_order(request, order)
    return render_partial_or_full(
        request,
        'orders/tracking.html#order-status',
        {'order': order, 'can_cancel': can_cancel},
    )


@require_POST
def order_cancel_view(request: HttpRequest, order_number: str) -> HttpResponse:
    order = get_object_or_404(Order, order_number=order_number)

    is_admin = bool(
        request.user.is_authenticated
        and getattr(request.user, 'role', None) == Role.RESTAURANT_ADMIN
    )
    is_owner = False
    if request.user.is_authenticated:
        is_owner = bool(order.user_id == request.user.id)
    elif order.session_key and request.session.session_key:
        is_owner = bool(order.user_id is None and order.session_key == request.session.session_key)

    if not (is_admin or is_owner):
        return HttpResponseForbidden('Ви не маєте прав для скасування цього замовлення.')

    if order.status in (OrderStatus.PENDING, OrderStatus.PAID):
        try:
            OrderService.cancel_order(order)
            messages.info(request, f'Замовлення {order.order_number} успішно скасовано.')
        except InvalidStatusTransitionError as e:
            messages.error(request, str(e))
    else:
        messages.error(request, 'Неможливо скасувати замовлення у поточному статусі.')
    return redirect('orders:tracking', order_number=order.order_number)


def cart_drawer_view(request: HttpRequest) -> HttpResponse:
    cart = CartService.get_cart(request)
    items = list(cart.items.select_related('dish', 'dish__category').all()) if cart else []
    total_amount = cart.total_amount if cart else Decimal('0.00')
    total_quantity = cart.total_quantity if cart else 0
    min_order_amount = Decimal(os.getenv('MIN_ORDER_AMOUNT', '200'))
    meets_min_amount = (total_amount >= min_order_amount)

    context = {
        'cart': cart,
        'items': items,
        'total_amount': total_amount,
        'total_quantity': total_quantity,
        'min_order_amount': min_order_amount,
        'meets_min_amount': meets_min_amount,
    }
    if request.headers.get('HX-Request'):
        return render_partial_or_full(
            request,
            'partials/cart_drawer.html#cart-drawer-content',
            context,
        )

    return render(request, 'partials/cart_drawer.html', context)



@login_required(login_url='/accounts/login/')
def order_list_view(request: HttpRequest) -> HttpResponse:
    if getattr(request.user, 'role', None) != Role.CUSTOMER:
        return HttpResponseForbidden('Перегляд списку замовлень доступний лише клієнтам.')
    orders = (
        Order.objects.filter(user=request.user)
        .prefetch_related('items')
        .order_by('-created_at')
    )
    return render(request, 'orders/order_list.html', {'orders': orders})
