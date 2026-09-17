import logging
import os
from decimal import Decimal
from typing import Any

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Sum
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from menu.models import Dish, DishOption

from .exceptions import DishUnavailableError
from .models import (
    Cart,
    CartItem,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)

logger = logging.getLogger('orders')
User = get_user_model()


def _get_val(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)



class CartService:
    @staticmethod
    def _invalidate_cart_cache(request: HttpRequest) -> None:
        if hasattr(request, '_cached_cart'):
            delattr(request, '_cached_cart')
        if hasattr(request, '_cart_items_count_cached'):
            delattr(request, '_cart_items_count_cached')

    @staticmethod
    def get_or_create_cart(request: HttpRequest) -> Cart:
        if not request.session.session_key:
            request.session.create()

        if request.user.is_authenticated:
            cart, _ = Cart.objects.get_or_create(user=request.user)
            request._cached_cart = cart
            return cart

        cart, _ = Cart.objects.get_or_create(session_key=request.session.session_key, user__isnull=True)
        request.session['guest_cart_id'] = cart.id
        request.session.modified = True
        request._cached_cart = cart
        return cart

    @staticmethod
    def get_cart(request: HttpRequest) -> Cart | None:
        if hasattr(request, '_cached_cart'):
            return request._cached_cart

        cart = None
        if request.user.is_authenticated:
            cart = Cart.objects.filter(user=request.user).first()
        else:
            cart_id = request.session.get('guest_cart_id')
            if cart_id:
                cart = Cart.objects.filter(id=cart_id, user__isnull=True).first()
            if not cart and request.session.session_key:
                cart = Cart.objects.filter(session_key=request.session.session_key, user__isnull=True).first()

        request._cached_cart = cart
        return cart

    @classmethod
    def get_items_count(cls, request: HttpRequest) -> int:
        if hasattr(request, '_cart_items_count_cached'):
            return request._cart_items_count_cached

        cart = cls.get_cart(request)
        if not cart:
            request._cart_items_count_cached = 0
            return 0

        if hasattr(cart, '_prefetched_objects_cache') and 'items' in cart._prefetched_objects_cache:
            count = cart.total_quantity
        else:
            total = cart.items.filter(
                dish__is_available=True,
                dish__category__is_active=True,
            ).aggregate(total=Sum('quantity'))['total']
            count = total or 0

        request._cart_items_count_cached = count
        return count

    @classmethod
    def add_dish(
        cls,
        request: HttpRequest,
        dish_id: int,
        quantity: int = 1,
        option_id: int | None = None,
        selected_options: list[dict[str, Any]] | None = None,
    ) -> CartItem:
        if quantity <= 0:
            quantity = 1
        quantity = min(quantity, 99)

        dish = get_object_or_404(Dish, pk=dish_id)
        if not dish.is_available or not dish.category.is_active:
            raise DishUnavailableError('Ця страва тимчасово недоступна для замовлення.')

        options_list: list[dict[str, Any]] = []
        if selected_options:
            options_list = list(selected_options)
        elif option_id:
            option = DishOption.objects.filter(dish=dish, id=option_id).first()
            if option:
                options_list.append({
                    'id': option.id,
                    'name': option.name,
                    'price_delta': str(option.price_delta),
                })

        cart = cls.get_or_create_cart(request)

        with transaction.atomic():
            cart = Cart.objects.select_for_update().get(id=cart.id)
            for item in cart.items.filter(dish=dish):
                if item.selected_options == options_list:
                    item.quantity = min(99, item.quantity + quantity)
                    item.save(update_fields=['quantity', 'updated_at'])
                    cls._invalidate_cart_cache(request)
                    return item

            new_item = CartItem.objects.create(
                cart=cart,
                dish=dish,
                quantity=quantity,
                selected_options=options_list,
            )
            cls._invalidate_cart_cache(request)
            return new_item

    @classmethod
    def update_item_quantity(
        cls,
        request: HttpRequest,
        item_id: int,
        quantity: int,
    ) -> CartItem | None:
        cart = cls.get_cart(request)
        if not cart:
            return None

        item = cart.items.filter(id=item_id).first()
        if not item:
            return None

        cls._invalidate_cart_cache(request)
        if quantity <= 0:
            item.delete()
            return None

        quantity = min(quantity, 99)

        item.quantity = quantity
        item.save(update_fields=['quantity', 'updated_at'])
        return item

    @classmethod
    def remove_item(cls, request: HttpRequest, item_id: int) -> bool:
        cart = cls.get_cart(request)
        if not cart:
            return False

        item = cart.items.filter(id=item_id).first()
        if item:
            cls._invalidate_cart_cache(request)
            item.delete()
            return True
        return False

    @classmethod
    def clear_cart(cls, request: HttpRequest) -> None:
        cart = cls.get_cart(request)
        if cart:
            cls._invalidate_cart_cache(request)
            cart.items.all().delete()

    @classmethod
    def merge_guest_cart(cls, request: HttpRequest, user: Any) -> None:
        guest_cart = None
        cart_id = request.session.get('guest_cart_id')
        if cart_id:
            guest_cart = Cart.objects.filter(id=cart_id, user__isnull=True).first()

        if not guest_cart:
            pre_key = request.session.get('_pre_login_session_key')
            if pre_key:
                guest_cart = Cart.objects.filter(session_key=pre_key, user__isnull=True).first()

        if not guest_cart:
            session_key = getattr(request.session, 'session_key', None)
            if session_key:
                guest_cart = Cart.objects.filter(session_key=session_key, user__isnull=True).first()

        if not guest_cart:
            return

        with transaction.atomic():
            user_cart, _ = Cart.objects.select_for_update().get_or_create(user=user)

            for guest_item in list(guest_cart.items.select_related('dish').all()):
                matched = False
                for user_item in user_cart.items.filter(dish=guest_item.dish):
                    if user_item.selected_options == guest_item.selected_options:
                        user_item.quantity = min(99, user_item.quantity + guest_item.quantity)
                        user_item.save(update_fields=['quantity', 'updated_at'])
                        matched = True
                        break

                if not matched:
                    guest_item.cart = user_cart
                    guest_item.save(update_fields=['cart', 'updated_at'])

            guest_cart.delete()
            request.session.pop('guest_cart_id', None)
            request.session.pop('_pre_login_session_key', None)
            request.session.modified = True


class StripeService:
    @classmethod
    def get_stripe_secret_key(cls) -> str:
        return getattr(settings, 'STRIPE_SECRET_KEY', '') or os.getenv('STRIPE_SECRET_KEY', '')

    @classmethod
    def get_stripe_public_key(cls) -> str:
        return getattr(settings, 'STRIPE_PUBLIC_KEY', '') or os.getenv('STRIPE_PUBLIC_KEY', '')

    @classmethod
    def get_webhook_secret(cls) -> str:
        return getattr(settings, 'STRIPE_WEBHOOK_SECRET', '') or os.getenv('STRIPE_WEBHOOK_SECRET', '')

    @classmethod
    def create_checkout_session(
        cls,
        order: Order,
        request: HttpRequest | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
    ) -> Any:
        secret_key = cls.get_stripe_secret_key()
        if not secret_key:
            raise ValueError('STRIPE_SECRET_KEY is not configured.')
        stripe.api_key = secret_key

        line_items = []
        for item in order.items.select_related('dish').all():
            price = item.price if item.price is not None else Decimal('0.00')
            unit_amount = int(price * 100)
            if unit_amount > 0:
                line_items.append({
                    'price_data': {
                        'currency': 'uah',
                        'unit_amount': unit_amount,
                        'product_data': {
                            'name': item.dish_title or 'Страва',
                        },
                    },
                    'quantity': item.quantity or 1,
                })

        if not line_items:
            fallback_amount = int((order.total_amount or Decimal('0.00')) * 100)
            if fallback_amount > 0:
                line_items.append({
                    'price_data': {
                        'currency': 'uah',
                        'unit_amount': fallback_amount,
                        'product_data': {
                            'name': f'Замовлення {order.order_number}',
                        },
                    },
                    'quantity': 1,
                })

        if not line_items:
            raise ValueError('Сума замовлення повинна бути більшою за 0 для створення сесії оплати.')

        if request:
            default_success_url = request.build_absolute_uri(
                reverse('orders:tracking', kwargs={'order_number': order.order_number})
            ) + '?session_id={CHECKOUT_SESSION_ID}'
            default_cancel_url = request.build_absolute_uri(
                reverse('orders:tracking', kwargs={'order_number': order.order_number})
            )
        else:
            default_success_url = f"/orders/tracking/{order.order_number}/?session_id={{CHECKOUT_SESSION_ID}}"
            default_cancel_url = f"/orders/tracking/{order.order_number}/"

        session_params: dict[str, Any] = {
            'payment_method_types': ['card'],
            'line_items': line_items,
            'mode': 'payment',
            'client_reference_id': order.order_number,
            'metadata': {
                'order_number': order.order_number,
                'order_id': str(order.id),
            },
            'payment_intent_data': {
                'metadata': {
                    'order_number': order.order_number,
                    'order_id': str(order.id),
                },
                'description': f'Замовлення {order.order_number}',
            },
            'success_url': success_url or default_success_url,
            'cancel_url': cancel_url or default_cancel_url,
        }

        if order.user and getattr(order.user, 'email', None):
            session_params['customer_email'] = order.user.email

        session = stripe.checkout.Session.create(**session_params)
        session_id = _get_val(session, 'id')
        if session_id:
            order.stripe_session_id = session_id
            order.save(update_fields=['stripe_session_id', 'updated_at'])
        return session

    @classmethod
    def handle_checkout_session_completed(
        cls,
        session: Any,
        target_status: str | None = None,
    ) -> Order | None:
        client_ref = _get_val(session, 'client_reference_id')
        session_id = _get_val(session, 'id')
        metadata = _get_val(session, 'metadata') or {}
        payment_intent = _get_val(session, 'payment_intent')

        order_number = client_ref or _get_val(metadata, 'order_number')
        order_id = _get_val(metadata, 'order_id')

        order = None
        if order_number:
            order = Order.objects.filter(order_number=order_number).first()
        if not order and session_id:
            order = Order.objects.filter(stripe_session_id=session_id).first()
        if not order and order_id:
            try:
                order = Order.objects.filter(id=int(order_id)).first()
            except (ValueError, TypeError):
                pass

        if not order:
            logger.warning(
                "Order not found for Stripe session: order_number=%s, session_id=%s, order_id=%s",
                order_number,
                session_id,
                order_id,
            )
            return None

        update_fields = ['payment_status', 'updated_at']
        if session_id and not order.stripe_session_id:
            order.stripe_session_id = session_id
            update_fields.append('stripe_session_id')

        pi_id = None
        if isinstance(payment_intent, str):
            pi_id = payment_intent
        elif payment_intent:
            pi_id = _get_val(payment_intent, 'id')

        if pi_id and not order.stripe_payment_intent_id:
            order.stripe_payment_intent_id = str(pi_id)
            update_fields.append('stripe_payment_intent_id')

        order.payment_status = PaymentStatus.PAID
        if order.can_transition_to(OrderStatus.PAID):
            order.transition_to(OrderStatus.PAID)
            order.payment_status = PaymentStatus.PAID

        success_status = target_status or getattr(settings, 'STRIPE_ORDER_SUCCESS_STATUS', 'PAID')
        if success_status == OrderStatus.PREPARING and order.can_transition_to(OrderStatus.PREPARING):
            order.transition_to(OrderStatus.PREPARING)
            order.payment_status = PaymentStatus.PAID

        order.save(update_fields=list(set(update_fields)))
        logger.info(
            "Order %s updated via Stripe webhook: status=%s, payment_status=%s",
            order.order_number,
            order.status,
            order.payment_status,
        )
        return order

    @classmethod
    def handle_payment_failed(cls, payment_intent: Any) -> Order | None:
        payment_intent_id = _get_val(payment_intent, 'id')
        metadata = _get_val(payment_intent, 'metadata') or {}
        order_number = _get_val(metadata, 'order_number')
        order_id = _get_val(metadata, 'order_id')

        order = None
        if payment_intent_id:
            order = Order.objects.filter(stripe_payment_intent_id=payment_intent_id).first()
        if not order and order_number:
            order = Order.objects.filter(order_number=order_number).first()
        if not order and order_id:
            try:
                order = Order.objects.filter(id=int(order_id)).first()
            except (ValueError, TypeError):
                pass

        if order:
            order.payment_status = PaymentStatus.FAILED
            update_fields = ['payment_status', 'updated_at']
            if payment_intent_id and not order.stripe_payment_intent_id:
                order.stripe_payment_intent_id = str(payment_intent_id)
                update_fields.append('stripe_payment_intent_id')
            order.save(update_fields=list(set(update_fields)))
            logger.info("Order %s marked as payment failed via Stripe webhook", order.order_number)
            return order

        logger.warning("Order not found for failed payment_intent: %s", payment_intent_id)
        return None

    @classmethod
    def handle_charge_refunded(cls, charge: Any) -> Order | None:
        payment_intent = _get_val(charge, 'payment_intent')
        pi_id = payment_intent if isinstance(payment_intent, str) else _get_val(payment_intent, 'id')
        metadata = _get_val(charge, 'metadata') or {}
        order_number = _get_val(metadata, 'order_number')

        order = None
        if pi_id:
            order = Order.objects.filter(stripe_payment_intent_id=str(pi_id)).first()
        if not order and order_number:
            order = Order.objects.filter(order_number=order_number).first()

        if order:
            order.payment_status = PaymentStatus.REFUNDED
            order.save(update_fields=['payment_status', 'updated_at'])
            logger.info("Order %s marked as refunded via Stripe webhook", order.order_number)
            return order

        logger.warning("Order not found for refunded charge: payment_intent=%s", pi_id)
        return None


class OrderService:
    @classmethod
    def create_order_from_cart(
        cls,
        request: HttpRequest,
        customer_name: str,
        customer_phone: str,
        delivery_address: str,
        payment_method: str = PaymentMethod.CARD,
        notes: str = '',
        create_stripe_session: bool = False,
    ) -> Order:
        from django.core.exceptions import ValidationError

        from .exceptions import CartEmptyError, OrderMinimumAmountError
        from .models import Order, OrderStatus, PaymentStatus

        cart = CartService.get_cart(request)
        if not cart or not cart.items.exists():
            raise CartEmptyError('Кошик порожній. Додайте страви для оформлення замовлення.')

        available_items = [
            item
            for item in cart.items.select_related('dish', 'dish__category').all()
            if item.is_available
        ]

        if not available_items:
            raise CartEmptyError('У вашому кошику немає доступних для замовлення страв.')

        total_amount = sum((item.total_price for item in available_items), Decimal('0.00'))

        min_order_amount = Decimal(os.getenv('MIN_ORDER_AMOUNT', '200'))
        if total_amount < min_order_amount:
            raise OrderMinimumAmountError(
                f'Мінімальна сума замовлення становить {min_order_amount} грн. Поточна сума: {total_amount} грн.'
            )

        name = customer_name.strip()
        phone = customer_phone.strip()
        address = delivery_address.strip()

        if not name:
            raise ValidationError({'customer_name': "Вкажіть ваше ім'я."})
        if not phone:
            raise ValidationError({'customer_phone': 'Вкажіть контактний номер телефону.'})
        if not address:
            raise ValidationError({'delivery_address': 'Вкажіть повну адресу доставки.'})

        session_key = getattr(request.session, 'session_key', '') or ''
        user = request.user if request.user.is_authenticated else None

        with transaction.atomic():
            order = Order.objects.create(
                user=user,
                session_key=session_key,
                customer_name=name,
                customer_phone=phone,
                delivery_address=address,
                payment_method=payment_method,
                status=OrderStatus.PENDING,
                payment_status=PaymentStatus.PENDING,
                total_amount=total_amount,
                notes=notes.strip(),
                eta_minutes=30,
            )

            for item in available_items:
                OrderItem.objects.create(
                    order=order,
                    dish=item.dish,
                    dish_title=item.dish.title,
                    price=item.unit_price,
                    quantity=item.quantity,
                    selected_options=item.selected_options,
                )

            cart.items.filter(id__in=[item.id for item in available_items]).delete()

        try:
            from orders.emails import send_order_confirmation_email

            host = request.get_host() if request else None
            proto = 'https' if request and request.is_secure() else 'http'
            send_order_confirmation_email(order, domain=host, protocol=proto)
        except Exception as e:
            logger.warning("Failed to send order confirmation email for %s: %s", order.order_number, e)

        if (
            payment_method in (PaymentMethod.ONLINE, PaymentMethod.STRIPE, 'ONLINE', 'STRIPE')
            and create_stripe_session
        ):
            try:
                StripeService.create_checkout_session(order, request=request)
            except (stripe.StripeError, ValueError) as e:
                logger.warning("Failed to create Stripe session for order %s: %s", order.order_number, e)

        return order

    @classmethod
    def create_stripe_checkout_session(
        cls,
        order: Order,
        request: HttpRequest | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
    ) -> Any:
        return StripeService.create_checkout_session(
            order=order,
            request=request,
            success_url=success_url,
            cancel_url=cancel_url,
        )

    @classmethod
    def handle_stripe_checkout_completed(
        cls,
        session: Any,
        target_status: str | None = None,
    ) -> Order | None:
        return StripeService.handle_checkout_session_completed(
            session=session,
            target_status=target_status,
        )

    @classmethod
    def cancel_order(cls, order: Order) -> None:
        from .models import OrderStatus

        order.transition_to(OrderStatus.CANCELLED)

