import datetime
import logging
import os
import re
import secrets
import threading
from contextlib import nullcontext
from decimal import Decimal
from typing import Any

import stripe
from accounts.models import Role
from config.partials import render_partial_or_full
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .exceptions import (
    CartEmptyError,
    CartForbiddenError,
    DishNotFoundError,
    DishUnavailableError,
    InvalidOptionError,
    InvalidStatusTransitionError,
    OrderMinimumAmountError,
)
from .forms import OrderCheckoutForm
from .middleware import get_guest_token, get_pre_login_guest_token
from .models import Order, OrderStatus, PaymentMethod, PaymentStatus, ProcessedStripeEvent
from .services import CartService, OrderService, StripeService

logger = logging.getLogger('orders')

_STRICT_INT_RE = re.compile(r'-?[0-9]+')
_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_SQLITE_WEBHOOK_LOCK = threading.Lock()


def _parse_strict_int(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not _STRICT_INT_RE.fullmatch(text):
            return None
        val = int(text)
    else:
        return None
    if _INT32_MIN <= val <= _INT32_MAX:
        return val
    return None


def _is_staff_blocked(user: Any) -> bool:
    return bool(
        user is not None
        and user.is_authenticated
        and not user.is_superuser
        and getattr(user, 'role', None) in (
            Role.RESTAURANT_ADMIN,
            Role.COURIER,
        )
    )


def _store_order_access_in_session(request: HttpRequest, order: Order) -> None:
    session = getattr(request, 'session', None)
    if session is None:
        return
    session[f'order_token_{order.pk}'] = order.access_token
    if hasattr(session, 'modified'):
        session.modified = True


def _safe_referer_redirect(request: HttpRequest, fallback: str = 'menu:catalog') -> HttpResponse:
    referer = request.META.get('HTTP_REFERER')
    if referer and url_has_allowed_host_and_scheme(
        url=referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(referer)
    return redirect(fallback)


def cart_view(request: HttpRequest) -> HttpResponse:
    cart = CartService.get_cart(request)
    summary = CartService.summary(cart)
    request._cached_cart_summary_obj = summary
    min_order_amount = getattr(settings, 'MIN_ORDER_AMOUNT', Decimal(200))
    min_diff = max(Decimal('0.00'), min_order_amount - summary.total_amount)

    context = {
        'cart': cart,
        'items': summary.items,
        'total_amount': summary.total_amount,
        'total_quantity': summary.total_quantity,
        'min_order_amount': min_order_amount,
        'meets_min_amount': summary.meets_min_amount,
        'min_diff': min_diff,
    }

    if request.headers.get('HX-Request') and not request.headers.get('HX-History-Restore-Request'):
        return render_partial_or_full(request, 'orders/cart.html#cart-content', context)

    return render_partial_or_full(request, 'orders/cart.html', context)


def cart_badge_view(request: HttpRequest) -> HttpResponse:
    count = CartService.get_items_count(request)
    return render(request, 'partials/cart_badge.html', {'cart_items_count': count})


@require_POST
def cart_add_view(request: HttpRequest, dish_id: int) -> HttpResponse:
    if _is_staff_blocked(getattr(request, 'user', None)):
        return HttpResponseForbidden(
            'Персонал ресторану не може замовляти страви через клієнтський кошик.'
        )

    raw_qty = request.POST.get('quantity')
    if raw_qty is None or str(raw_qty).strip() == '':
        quantity = 1
    else:
        parsed_qty = _parse_strict_int(raw_qty)
        if parsed_qty is None or not (1 <= parsed_qty <= 99):
            return HttpResponse('Некоректна кількість страви.', status=400)
        quantity = parsed_qty

    raw_option_id = request.POST.get('option_id', '')
    option_id: int | None = None
    if str(raw_option_id).strip() != '':
        parsed_option = _parse_strict_int(raw_option_id)
        if parsed_option is None or parsed_option <= 0:
            return HttpResponse('Некоректний ідентифікатор опції.', status=400)
        option_id = parsed_option

    option_ids: list[int] = []
    for raw_opt in request.POST.getlist('option_ids'):
        stripped = str(raw_opt).strip()
        if not stripped:
            continue
        parsed_opt = _parse_strict_int(stripped)
        if parsed_opt is None or parsed_opt <= 0:
            return HttpResponse('Некоректний ідентифікатор опції.', status=400)
        option_ids.append(parsed_opt)

    try:
        CartService.add_dish(
            request,
            dish_id=dish_id,
            quantity=quantity,
            option_id=option_id,
            option_ids=option_ids or None,
        )
    except CartForbiddenError as e:
        return HttpResponseForbidden(str(e))
    except DishNotFoundError as e:
        raise Http404(str(e)) from e
    except (DishUnavailableError, InvalidOptionError) as e:
        if request.headers.get('HX-Request'):
            return HttpResponse(str(e), status=400)
        messages.error(request, str(e))
        return _safe_referer_redirect(request, fallback='menu:catalog')

    if request.headers.get('HX-Request'):
        count = CartService.get_items_count(request)
        response = render(request, 'partials/cart_badge.html', {'cart_items_count': count})
        response['HX-Trigger'] = 'open-cart-drawer'
        return response

    return _safe_referer_redirect(request, fallback='menu:catalog')


@require_POST
def cart_update_view(request: HttpRequest, item_id: int) -> HttpResponse:
    quantity = _parse_strict_int(request.POST.get('quantity'))
    if quantity is None or not (0 <= quantity <= 99):
        return HttpResponse('Некоректна кількість страви.', status=400)

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
    if _is_staff_blocked(getattr(request, 'user', None)):
        return HttpResponseForbidden(
            'Персонал ресторану не може оформлювати клієнтські замовлення.'
        )

    cart = CartService.get_cart(request)
    if not cart or not cart.items.exists():
        recent_order = _find_recent_pending_order(request)
        if recent_order:
            _store_order_access_in_session(request, recent_order)
            messages.info(
                request,
                f'Ваше замовлення {recent_order.order_number} вже оформлено. '
                f'Ви можете відстежити його статус.',
            )
            return redirect('orders:tracking', order_number=recent_order.order_number)

        messages.warning(request, 'Кошик порожній. Додайте страви для оформлення замовлення.')
        return redirect('orders:cart')

    summary = CartService.summary(cart)
    request._cached_cart_summary_obj = summary
    min_order_amount = getattr(settings, 'MIN_ORDER_AMOUNT', Decimal(200))

    if not summary.meets_min_amount:
        messages.warning(
            request,
            f'Мінімальна сума замовлення — {min_order_amount} грн. Додайте ще страв до кошика.',
        )
        return redirect('orders:cart')

    saved_addresses = (
        list(request.user.addresses.all())
        if request.user.is_authenticated
        else []
    )

    if request.method == 'POST':
        form = OrderCheckoutForm(request.POST)
        if form.is_valid():
            try:
                payment_method = form.cleaned_data['payment_method']
                is_online_payment = PaymentMethod.is_online(payment_method)

                order = OrderService.create_order_from_cart(
                    request=request,
                    customer_name=form.cleaned_data['customer_name'],
                    customer_phone=form.cleaned_data['customer_phone'],
                    delivery_address=form.cleaned_data['delivery_address'],
                    payment_method=payment_method,
                    notes=form.cleaned_data.get('notes', ''),
                    customer_email=form.cleaned_data.get('customer_email', ''),
                    create_stripe_session=False,
                )

                _store_order_access_in_session(request, order)
                redirect_url = reverse(
                    'orders:tracking',
                    kwargs={'order_number': order.order_number},
                )

                if is_online_payment:
                    try:
                        session = StripeService.create_checkout_session(order, request=request)
                        if session and getattr(session, 'url', None):
                            return redirect(session.url)
                    except (stripe.StripeError, ValueError) as e:
                        logger.error('Error creating Stripe checkout session: %s', e)
                        messages.warning(
                            request,
                            f'Замовлення {order.order_number} створено, але не вдалося '
                            f'перенаправити на оплату. Ви можете оплатити на сторінці замовлення.',
                        )
                        return redirect(redirect_url)

                messages.success(request, f'Замовлення {order.order_number} успішно оформлено!')
                return redirect(redirect_url)
            except CartEmptyError as e:
                recent_order = _find_recent_pending_order(request)
                if recent_order:
                    _store_order_access_in_session(request, recent_order)
                    messages.info(
                        request,
                        f'Ваше замовлення {recent_order.order_number} вже оформлено. '
                        f'Ви можете відстежити його статус.',
                    )
                    return redirect('orders:tracking', order_number=recent_order.order_number)
                form.add_error(None, str(e))
            except (
                DishUnavailableError,
                InvalidOptionError,
                OrderMinimumAmountError,
            ) as e:
                form.add_error(None, str(e))
            except ValidationError as e:
                form.add_error(None, e)
    else:
        initial_data: dict[str, Any] = {'payment_method': PaymentMethod.ONLINE}
        if request.user.is_authenticated:
            full_name = f'{request.user.first_name} {request.user.last_name}'.strip()
            initial_data['customer_name'] = full_name or request.user.email.split('@')[0]
            if getattr(request.user, 'email', None):
                initial_data['customer_email'] = request.user.email
            if getattr(request.user, 'phone', None):
                initial_data['customer_phone'] = request.user.phone
            default_address = next((addr for addr in saved_addresses if addr.is_default), None)
            if default_address:
                initial_data['delivery_address'] = default_address.format_full_address()
        form = OrderCheckoutForm(initial=initial_data)

    context = {
        'form': form,
        'cart': cart,
        'items': summary.items,
        'total_amount': summary.total_amount,
        'total_quantity': summary.total_quantity,
        'saved_addresses': saved_addresses,
    }
    return render(request, 'orders/checkout.html', context)


def _find_recent_pending_order(request: HttpRequest) -> Order | None:
    """Find a recent order for the current user/guest to redirect to instead of empty cart."""
    cutoff = timezone.now() - datetime.timedelta(minutes=30)

    if request.user.is_authenticated:
        return (
            Order.objects.filter(
                user=request.user,
                created_at__gte=cutoff,
                status__in=(OrderStatus.PENDING, OrderStatus.PAID),
            )
            .order_by('-created_at')
            .first()
        )

    guest_token = get_guest_token(request, create=False)
    if guest_token:
        return (
            Order.objects.filter(
                guest_token=guest_token,
                user__isnull=True,
                created_at__gte=cutoff,
                status__in=(OrderStatus.PENDING, OrderStatus.PAID),
            )
            .order_by('-created_at')
            .first()
        )

    return None


def user_can_view_order(request: HttpRequest, order: Order) -> bool:
    u = request.user
    if u.is_authenticated and (u.is_superuser or getattr(u, 'role', None) == Role.RESTAURANT_ADMIN):
        return True
    if u.is_authenticated and order.user_id == u.id:
        return True

    session = getattr(request, 'session', None)
    session_token = session.get(f'order_token_{order.pk}') if session is not None else None
    token = request.GET.get('t') or session_token
    if (
        token
        and getattr(order, 'access_token', None)
        and secrets.compare_digest(str(token), str(order.access_token))
    ):
        return True

    if order.user_id is None and order.guest_token:
        guest_token = get_guest_token(request, create=False)
        if guest_token and secrets.compare_digest(str(order.guest_token), str(guest_token)):
            return True
        pre_token = get_pre_login_guest_token(request)
        if pre_token and secrets.compare_digest(str(order.guest_token), str(pre_token)):
            return True

    return False


def _user_can_cancel_order(request: HttpRequest, order: Order) -> bool:
    if order.status not in (OrderStatus.PENDING, OrderStatus.PAID):
        return False
    return user_can_view_order(request, order)


def _can_pay_order_online(order: Order) -> bool:
    return bool(
        order.is_online_payment
        and order.status == OrderStatus.PENDING
        and order.payment_status != PaymentStatus.PAID
    )


def tracking_view(request: HttpRequest, order_number: str) -> HttpResponse:
    order = get_object_or_404(Order.objects.prefetch_related('items'), order_number=order_number)
    if not user_can_view_order(request, order):
        raise Http404('Замовлення не знайдено.')

    query_token = request.GET.get('t')
    if (
        query_token
        and getattr(order, 'access_token', None)
        and secrets.compare_digest(str(query_token), str(order.access_token))
    ):
        _store_order_access_in_session(request, order)
        return redirect('orders:tracking', order_number=order.order_number)

    return render(
        request,
        'orders/tracking.html',
        {
            'order': order,
            'can_cancel': _user_can_cancel_order(request, order),
            'can_pay_online': _can_pay_order_online(order),
        },
    )


def tracking_status_view(request: HttpRequest, order_number: str) -> HttpResponse:
    order = get_object_or_404(Order.objects.prefetch_related('items'), order_number=order_number)
    if not user_can_view_order(request, order):
        raise Http404('Замовлення не знайдено.')

    return render_partial_or_full(
        request,
        'orders/tracking.html#order-status',
        {
            'order': order,
            'can_cancel': _user_can_cancel_order(request, order),
            'can_pay_online': _can_pay_order_online(order),
        },
    )


@require_POST
def order_cancel_view(request: HttpRequest, order_number: str) -> HttpResponse:
    order = get_object_or_404(Order, order_number=order_number)
    if not user_can_view_order(request, order):
        raise Http404('Замовлення не знайдено.')

    if not _user_can_cancel_order(request, order):
        return HttpResponseForbidden('Ви не маєте прав для скасування цього замовлення.')

    try:
        OrderService.cancel_order(order)
        messages.info(request, f'Замовлення {order.order_number} успішно скасовано.')
    except InvalidStatusTransitionError as e:
        messages.error(request, str(e))

    return redirect('orders:tracking', order_number=order.order_number)


def cart_drawer_view(request: HttpRequest) -> HttpResponse:
    cart = CartService.get_cart(request)
    summary = CartService.summary(cart)
    request._cached_cart_summary_obj = summary
    min_order_amount = getattr(settings, 'MIN_ORDER_AMOUNT', Decimal(200))

    context = {
        'cart': cart,
        'items': summary.items,
        'total_amount': summary.total_amount,
        'total_quantity': summary.total_quantity,
        'min_order_amount': min_order_amount,
        'meets_min_amount': summary.meets_min_amount,
    }
    if request.headers.get('HX-Request') and not request.headers.get('HX-History-Restore-Request'):
        return render_partial_or_full(
            request,
            'partials/cart_drawer.html#cart-drawer-content',
            context,
        )

    return render(request, 'partials/cart_drawer.html', context)


@login_required(login_url=reverse_lazy('accounts:login'))
def order_list_view(request: HttpRequest) -> HttpResponse:
    if getattr(request.user, 'role', None) != Role.CUSTOMER and not request.user.is_superuser:
        return HttpResponseForbidden('Перегляд списку замовлень доступний лише клієнтам.')
    orders = (
        Order.objects.filter(user=request.user)
        .prefetch_related('items')
        .order_by('-created_at')
    )
    paginator = Paginator(orders, 15)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(
        request,
        'orders/order_list.html',
        {'orders': page_obj, 'page_obj': page_obj},
    )


@csrf_exempt
@require_POST
def stripe_webhook_view(request: HttpRequest) -> HttpResponse:
    payload = request.body
    sig_header = request.headers.get('Stripe-Signature') or request.META.get('HTTP_STRIPE_SIGNATURE', '')
    webhook_secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '') or os.getenv('STRIPE_WEBHOOK_SECRET', '')

    if not sig_header:
        logger.warning('Stripe webhook called without Stripe-Signature header.')
        return HttpResponse('Missing signature header', status=400)

    if not webhook_secret:
        logger.error('STRIPE_WEBHOOK_SECRET is not configured.')
        return HttpResponse('Webhook secret not configured', status=500)

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=webhook_secret,
        )
    except ValueError as e:
        logger.warning('Invalid Stripe webhook payload: %s', e)
        return HttpResponse('Invalid payload', status=400)
    except stripe.SignatureVerificationError as e:
        logger.warning('Invalid Stripe webhook signature: %s', e)
        return HttpResponse('Invalid signature', status=400)
    except stripe.StripeError as e:
        logger.error('Unexpected error in Stripe webhook verification: %s', e)
        return HttpResponse('Webhook verification failed', status=400)

    event_id = getattr(event, 'id', None) or (event.get('id') if isinstance(event, dict) else None)
    event_type = getattr(event, 'type', None) or (event.get('type') if isinstance(event, dict) else None)
    data_object = None
    if hasattr(event, 'data') and hasattr(event.data, 'object'):
        data_object = event.data.object
    elif isinstance(event, dict):
        data_object = event.get('data', {}).get('object')

    db_conn = transaction.get_connection()
    use_sqlite_lock = db_conn.vendor == 'sqlite' and not db_conn.in_atomic_block
    guard = _SQLITE_WEBHOOK_LOCK if use_sqlite_lock else nullcontext()

    with guard, transaction.atomic():
        if event_id:
            try:
                with transaction.atomic():
                    ProcessedStripeEvent.objects.create(
                        event_id=event_id,
                        event_type=event_type or '',
                    )
            except IntegrityError:
                logger.info('Stripe event already processed: %s', event_id)
                return HttpResponse(status=200)

        if event_type in (
            'checkout.session.completed',
            'checkout.session.async_payment_succeeded',
        ) and data_object:
            StripeService.handle_checkout_session_completed(data_object)
        elif event_type in (
            'payment_intent.payment_failed',
            'checkout.session.async_payment_failed',
            'checkout.session.expired',
        ) and data_object:
            StripeService.handle_payment_failed(data_object, event_type=event_type)
        elif event_type == 'charge.refunded' and data_object:
            StripeService.handle_charge_refunded(data_object)

    return HttpResponse(status=200)


@require_POST
def stripe_checkout_view(request: HttpRequest, order_number: str) -> HttpResponse:
    order = get_object_or_404(Order, order_number=order_number)
    if not user_can_view_order(request, order):
        raise Http404('Замовлення не знайдено.')

    if not order.is_online_payment:
        messages.error(request, 'Онлайн-оплата недоступна для обраного способу оплати.')
        return redirect('orders:tracking', order_number=order.order_number)

    if order.payment_status == PaymentStatus.PAID:
        messages.info(request, 'Це замовлення вже оплачено.')
        return redirect('orders:tracking', order_number=order.order_number)

    if order.status != OrderStatus.PENDING:
        msg = (
            'Це замовлення було скасовано.'
            if order.status == OrderStatus.CANCELLED
            else 'Оплата онлайн недоступна у поточному статусі замовлення.'
        )
        messages.error(request, msg)
        return redirect('orders:tracking', order_number=order.order_number)

    try:
        session = StripeService.create_checkout_session(order, request=request)
        if session and getattr(session, 'url', None):
            return redirect(session.url)
    except (stripe.StripeError, ValueError) as e:
        logger.error('Error creating Stripe checkout session: %s', e)
        messages.error(request, 'Помилка ініціалізації онлайн-оплати.')

    return redirect('orders:tracking', order_number=order.order_number)
