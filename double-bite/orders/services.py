import logging
import os
import re
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlsplit

import stripe
from accounts.models import Role
from config.transactions import schedule_on_commit
from config.validators import normalize_email, normalize_phone, phone_validator
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import HttpRequest
from django.urls import reverse
from menu.models import Dish, DishOption

from .exceptions import (
    CartEmptyError,
    CartForbiddenError,
    DishNotFoundError,
    DishUnavailableError,
    InvalidOptionError,
    InvalidStatusTransitionError,
    OrderMinimumAmountError,
)
from .middleware import (
    PRE_LOGIN_GUEST_TOKEN_SESSION_KEY,
    get_guest_token,
    get_pre_login_guest_token,
)
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

_STRICT_INT_RE = re.compile(r'-?[0-9]+')
_INT32_MAX = 2**31 - 1
_MANUAL_REFUND_NOTE = '[Потребує ручного повернення коштів через Stripe]'
_SQLITE_CART_LOCK = threading.RLock()


def _sqlite_cart_guard() -> Any:
    db_conn = transaction.get_connection()
    if db_conn.vendor == 'sqlite' and not db_conn.in_atomic_block:
        return _SQLITE_CART_LOCK
    return nullcontext()


def _get_val(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _parse_strict_positive_int(raw: Any) -> int | None:
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
    if 1 <= val <= _INT32_MAX:
        return val
    return None


def _parse_finite_decimal(raw: Any) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        dec = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not dec.is_finite():
        return None
    return dec


def _append_audit_note(existing: str, note: str) -> str:
    if not note:
        return existing
    if note in (existing or ''):
        return existing
    return f'{existing}\n{note}'.strip() if existing else note


def _resolve_pi_id(payment_intent: Any) -> str:
    raw = None
    if isinstance(payment_intent, str):
        raw = payment_intent
    elif payment_intent is not None:
        raw = _get_val(payment_intent, 'id')
    pi_id = str(raw).strip() if raw else ''
    if pi_id.startswith('cs_'):
        return ''
    return pi_id


def _append_access_token_param(url: str, access_token: str) -> str:
    if not access_token:
        return url
    query_params = parse_qs(urlsplit(url).query)
    if 't' in query_params:
        return url
    sep = '&' if '?' in url else '?'
    return f'{url}{sep}t={access_token}'


def _mark_session_modified(session: Any) -> None:
    if session is not None and hasattr(session, 'modified'):
        session.modified = True


def _session_get(session: Any, key: str, default: Any = None) -> Any:
    if session is not None and hasattr(session, 'get'):
        return session.get(key, default)
    return default


@dataclass(frozen=True)
class CartSummary:
    items: list[CartItem]
    total_amount: Decimal
    total_quantity: int
    meets_min_amount: bool


class CartService:
    @classmethod
    def summary(cls, cart: Cart | None) -> CartSummary:
        if not cart or not getattr(cart, 'pk', None):
            return CartSummary(
                items=[],
                total_amount=Decimal('0.00'),
                total_quantity=0,
                meets_min_amount=False,
            )
        prefetched = getattr(cart, '_prefetched_objects_cache', None)
        if isinstance(prefetched, dict) and 'items' in prefetched:
            items = list(cart.items.all())
        else:
            items = list(cart.items.select_related('dish', 'dish__category').all())

        available_items = [item for item in items if item.is_available]
        total_amount = sum((item.total_price for item in available_items), Decimal('0.00'))
        total_quantity = sum((item.quantity or 0 for item in available_items), 0)
        min_amount = getattr(settings, 'MIN_ORDER_AMOUNT', Decimal(200))
        return CartSummary(
            items=items,
            total_amount=total_amount,
            total_quantity=total_quantity,
            meets_min_amount=total_amount >= min_amount,
        )

    @staticmethod
    def _invalidate_cart_cache(request: HttpRequest) -> None:
        for attr in (
            '_cached_cart_obj',
            '_cached_cart_summary_obj',
            '_cart_items_count_cached',
            '_cached_cart',
        ):
            if hasattr(request, attr):
                delattr(request, attr)

    @staticmethod
    def _get_or_create_user_cart(user: Any) -> Cart:
        with _sqlite_cart_guard():
            cart = Cart.objects.filter(user=user).first()
            if cart is None:
                try:
                    with transaction.atomic():
                        cart = Cart.objects.create(user=user)
                except IntegrityError:
                    cart = Cart.objects.get(user=user)
            return cart

    @staticmethod
    def get_or_create_cart(request: HttpRequest) -> Cart:
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            cart = CartService._get_or_create_user_cart(user)
            request._cached_cart_obj = cart
            return cart

        guest_token = get_guest_token(request, create=True)
        session = getattr(request, 'session', None)
        cart_id = _session_get(session, 'guest_cart_id')
        with _sqlite_cart_guard():
            cart = None
            if cart_id:
                cart = Cart.objects.filter(
                    id=cart_id,
                    guest_token=guest_token,
                    user__isnull=True,
                ).first()
            if cart is None:
                cart = Cart.objects.filter(
                    guest_token=guest_token,
                    user__isnull=True,
                ).first()
            if cart is None:
                try:
                    with transaction.atomic():
                        cart = Cart.objects.create(guest_token=guest_token, user=None)
                except IntegrityError:
                    cart = Cart.objects.get(guest_token=guest_token, user__isnull=True)

        if session is not None:
            session['guest_cart_id'] = cart.id
            _mark_session_modified(session)
        request._cached_cart_obj = cart
        return cart

    @staticmethod
    def get_cart(request: HttpRequest) -> Cart | None:
        if hasattr(request, '_cached_cart_obj'):
            return request._cached_cart_obj
        if hasattr(request, '_cached_cart'):
            return request._cached_cart

        cart = None
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            cart = Cart.objects.filter(user=user).first()
        else:
            guest_token = get_guest_token(request, create=False)
            if guest_token:
                session = getattr(request, 'session', None)
                cart_id = _session_get(session, 'guest_cart_id')
                if cart_id:
                    cart = Cart.objects.filter(
                        id=cart_id,
                        guest_token=guest_token,
                        user__isnull=True,
                    ).first()
                if cart is None:
                    cart = Cart.objects.filter(
                        guest_token=guest_token,
                        user__isnull=True,
                    ).first()

        request._cached_cart_obj = cart
        return cart

    @classmethod
    def get_items_count(cls, request: HttpRequest) -> int:
        if hasattr(request, '_cart_items_count_cached'):
            return request._cart_items_count_cached

        if hasattr(request, '_cached_cart_summary_obj'):
            count = request._cached_cart_summary_obj.total_quantity
            request._cart_items_count_cached = count
            return count

        cart = cls.get_cart(request)
        if not cart:
            request._cart_items_count_cached = 0
            return 0

        prefetched = getattr(cart, '_prefetched_objects_cache', None)
        if isinstance(prefetched, dict) and 'items' in prefetched:
            count = cart.total_quantity
        else:
            total = cart.items.filter(
                dish__is_available=True,
                dish__category__is_active=True,
            ).aggregate(total=Sum('quantity'))['total']
            count = total or 0

        request._cart_items_count_cached = count
        return count

    @staticmethod
    def _canonicalize_options_for_dish(
        dish: Dish,
        option_id: int | None = None,
        option_ids: list[int] | None = None,
        selected_options: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        collected_ids: list[int] = []

        if option_ids is not None:
            if isinstance(option_ids, (str, bytes)) or not hasattr(option_ids, '__iter__'):
                raise InvalidOptionError('Некоректний список опцій.')
            for raw_id in option_ids:
                parsed = _parse_strict_positive_int(raw_id)
                if parsed is None:
                    raise InvalidOptionError('Некоректний ідентифікатор опції.')
                collected_ids.append(parsed)

        if option_id is not None and option_id != '':
            parsed = _parse_strict_positive_int(option_id)
            if parsed is None:
                raise InvalidOptionError('Некоректний ідентифікатор опції.')
            collected_ids.append(parsed)

        if selected_options is not None:
            if not isinstance(selected_options, list):
                raise InvalidOptionError('Некоректний формат обраних опцій.')
            for entry in selected_options:
                if not isinstance(entry, dict):
                    raise InvalidOptionError('Некоректний формат обраної опції.')
                raw_entry_id = (
                    entry['option_id']
                    if 'option_id' in entry and entry['option_id'] is not None
                    else entry.get('id')
                )
                parsed = _parse_strict_positive_int(raw_entry_id)
                if parsed is None:
                    raise InvalidOptionError('Опція повинна містити коректний ідентифікатор.')
                if 'price_delta' in entry and entry['price_delta'] is not None:
                    if _parse_finite_decimal(entry['price_delta']) is None:
                        raise InvalidOptionError('Некоректна вартість опції.')
                collected_ids.append(parsed)

        unique_ids = sorted(set(collected_ids))
        if not unique_ids:
            return []

        matched = list(
            DishOption.objects.filter(dish=dish, id__in=unique_ids).order_by('id')
        )
        if len(matched) != len(unique_ids):
            raise InvalidOptionError('Обрана опція недоступна для цієї страви.')

        result: list[dict[str, Any]] = []
        for opt in matched:
            if _parse_finite_decimal(opt.price_delta) is None:
                raise InvalidOptionError('Некоректна вартість опції в меню.')
            result.append({
                'option_id': opt.id,
                'name': opt.name,
                'price_delta': str(opt.price_delta),
            })
        return result

    @staticmethod
    def _extract_option_ids(options: Any) -> tuple[int, ...] | None:
        if not options:
            return ()
        if not isinstance(options, list):
            return None
        ids: list[int] = []
        for entry in options:
            if not isinstance(entry, dict):
                return None
            raw_id = (
                entry['option_id']
                if 'option_id' in entry and entry['option_id'] is not None
                else entry.get('id')
            )
            parsed = _parse_strict_positive_int(raw_id)
            if parsed is None:
                return None
            ids.append(parsed)
        return tuple(sorted(ids))

    @classmethod
    def _items_options_match(
        cls,
        existing_options: Any,
        target_options: list[dict[str, Any]],
        target_sig: tuple[int, ...] | None,
    ) -> bool:
        existing_sig = cls._extract_option_ids(existing_options)
        if target_sig is not None and existing_sig is not None:
            return existing_sig == target_sig
        return existing_options == target_options

    @classmethod
    def add_dish(
        cls,
        request: HttpRequest,
        dish_id: int,
        quantity: int = 1,
        option_id: int | None = None,
        option_ids: list[int] | None = None,
        selected_options: list[dict[str, Any]] | None = None,
    ) -> CartItem:
        user = getattr(request, 'user', None)
        if (
            user is not None
            and user.is_authenticated
            and not user.is_superuser
            and getattr(user, 'role', None) in (
                Role.RESTAURANT_ADMIN,
                Role.COURIER,
            )
        ):
            raise CartForbiddenError(
                'Персонал ресторану не може замовляти страви через клієнтський кошик.'
            )

        if quantity <= 0:
            quantity = 1
        quantity = min(quantity, 99)

        dish = Dish.objects.select_related('category').filter(pk=dish_id).first()
        if dish is None:
            raise DishNotFoundError('Страву не знайдено.')
        if not dish.is_available or not dish.category.is_active:
            raise DishUnavailableError('Ця страва тимчасово недоступна для замовлення.')

        canonical_options = cls._canonicalize_options_for_dish(
            dish,
            option_id=option_id,
            option_ids=option_ids,
            selected_options=selected_options,
        )
        target_sig = cls._extract_option_ids(canonical_options)
        session = getattr(request, 'session', None)

        with _sqlite_cart_guard():
            for attempt in range(2):
                cart = cls.get_or_create_cart(request)
                with transaction.atomic():
                    locked_cart = Cart.objects.select_for_update().filter(id=cart.id).first()
                    if locked_cart is None:
                        cls._invalidate_cart_cache(request)
                        if session is not None and hasattr(session, 'pop'):
                            session.pop('guest_cart_id', None)
                        if attempt == 0:
                            continue
                        raise CartEmptyError('Кошик було видалено. Спробуйте ще раз.')

                    for item in locked_cart.items.filter(dish=dish):
                        if cls._items_options_match(
                            item.selected_options,
                            canonical_options,
                            target_sig,
                        ):
                            item.quantity = min(99, item.quantity + quantity)
                            item.save(update_fields=['quantity', 'updated_at'])
                            cls._invalidate_cart_cache(request)
                            return item

                    new_item = CartItem.objects.create(
                        cart=locked_cart,
                        dish=dish,
                        quantity=quantity,
                        selected_options=canonical_options,
                    )
                    cls._invalidate_cart_cache(request)
                    return new_item

        raise CartEmptyError('Не вдалося додати страву до кошика.')

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
        if request is None:
            return

        session = getattr(request, 'session', None)
        pre_token = get_pre_login_guest_token(request) or get_guest_token(request, create=False)
        if pre_token and session is not None:
            session[PRE_LOGIN_GUEST_TOKEN_SESSION_KEY] = pre_token
            _mark_session_modified(session)

        cart_id = (
            _session_get(session, 'guest_cart_id')
            or getattr(request, '_pre_login_guest_cart_id', None)
        )

        with _sqlite_cart_guard(), transaction.atomic():
            guest_cart = None
            if cart_id:
                candidate = Cart.objects.select_for_update().filter(
                    id=cart_id,
                    user__isnull=True,
                ).first()
                if candidate and (
                    not pre_token
                    or not candidate.guest_token
                    or candidate.guest_token == pre_token
                ):
                    guest_cart = candidate

            if guest_cart is None and pre_token:
                guest_cart = Cart.objects.select_for_update().filter(
                    guest_token=pre_token,
                    user__isnull=True,
                ).first()

            if guest_cart is None:
                return

            user_cart = Cart.objects.select_for_update().filter(user=user).first()
            if user_cart is None:
                try:
                    with transaction.atomic():
                        user_cart = Cart.objects.create(user=user)
                except IntegrityError:
                    user_cart = Cart.objects.select_for_update().get(user=user)

            for guest_item in list(guest_cart.items.select_related('dish').all()):
                guest_options = guest_item.selected_options or []
                if guest_options:
                    try:
                        guest_options = cls._canonicalize_options_for_dish(
                            guest_item.dish,
                            selected_options=guest_options,
                        )
                    except InvalidOptionError:
                        guest_options = list(guest_options) if isinstance(guest_options, list) else []

                guest_sig = cls._extract_option_ids(guest_options)
                matched = False
                for user_item in user_cart.items.filter(dish=guest_item.dish):
                    if cls._items_options_match(user_item.selected_options, guest_options, guest_sig):
                        user_item.quantity = min(99, user_item.quantity + guest_item.quantity)
                        user_item.save(update_fields=['quantity', 'updated_at'])
                        matched = True
                        break

                if not matched:
                    guest_item.cart = user_cart
                    guest_item.quantity = min(99, guest_item.quantity)
                    guest_item.selected_options = guest_options
                    guest_item.save(
                        update_fields=['cart', 'quantity', 'selected_options', 'updated_at']
                    )

            guest_cart.delete()
            if session is not None and hasattr(session, 'pop'):
                session.pop('guest_cart_id', None)
                _mark_session_modified(session)
            cls._invalidate_cart_cache(request)

    merge_guest_cart_to_user = merge_guest_cart


class StripeService:
    @classmethod
    def get_stripe_secret_key(cls) -> str:
        return getattr(settings, 'STRIPE_SECRET_KEY', '') or os.getenv('STRIPE_SECRET_KEY', '')

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

        with transaction.atomic():
            locked_order = Order.objects.select_for_update().select_related('user').get(pk=order.pk)
            if not locked_order.is_online_payment:
                raise ValueError('Онлайн-оплата доступна лише для методу оплати ONLINE.')
            if locked_order.status != OrderStatus.PENDING:
                raise ValueError('Створення платіжної сесії можливе лише для замовлень у статусі PENDING.')
            if locked_order.payment_status == PaymentStatus.PAID:
                raise ValueError('Це замовлення вже оплачено.')

            db_items = list(locked_order.items.select_related('dish').all())
            source_items = db_items if db_items else list(order.items.all())

            line_items = []
            for item in source_items:
                price = item.price if item.price is not None else Decimal('0.00')
                unit_amount = int((price * 100).quantize(Decimal(1)))
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
                fallback_amount = int(
                    ((locked_order.total_amount or Decimal('0.00')) * 100).quantize(Decimal(1))
                )
                if fallback_amount > 0:
                    line_items.append({
                        'price_data': {
                            'currency': 'uah',
                            'unit_amount': fallback_amount,
                            'product_data': {
                                'name': f'Замовлення {locked_order.order_number}',
                            },
                        },
                        'quantity': 1,
                    })

            if not line_items:
                raise ValueError('Сума замовлення повинна бути більшою за 0 для створення сесії оплати.')

            order_pk = locked_order.pk
            order_number = locked_order.order_number
            existing_session_id = locked_order.stripe_session_id or ''
            customer_email = locked_order.customer_email or (
                locked_order.user.email if locked_order.user else ''
            )
            is_guest_order = locked_order.user_id is None and bool(locked_order.access_token)
            access_token = locked_order.access_token if is_guest_order else ''

        if existing_session_id:
            try:
                existing_session = stripe.checkout.Session.retrieve(
                    existing_session_id,
                    api_key=secret_key,
                )
                if _get_val(existing_session, 'status') == 'open' and _get_val(existing_session, 'url'):
                    return existing_session
            except stripe.StripeError:
                pass

        if request:
            tracking_path = reverse('orders:tracking', kwargs={'order_number': order_number})
            default_success_url = (
                request.build_absolute_uri(tracking_path) + '?session_id={CHECKOUT_SESSION_ID}'
            )
            default_cancel_url = request.build_absolute_uri(tracking_path)
        else:
            default_success_url = (
                f'/orders/tracking/{order_number}/?session_id={{CHECKOUT_SESSION_ID}}'
            )
            default_cancel_url = f'/orders/tracking/{order_number}/'

        resolved_success_url = success_url or default_success_url
        resolved_cancel_url = cancel_url or default_cancel_url
        if is_guest_order:
            resolved_success_url = _append_access_token_param(resolved_success_url, access_token)
            resolved_cancel_url = _append_access_token_param(resolved_cancel_url, access_token)

        session_params: dict[str, Any] = {
            'payment_method_types': ['card'],
            'line_items': line_items,
            'mode': 'payment',
            'client_reference_id': order_number,
            'metadata': {
                'order_number': order_number,
                'order_id': str(order_pk),
            },
            'payment_intent_data': {
                'metadata': {
                    'order_number': order_number,
                    'order_id': str(order_pk),
                },
                'description': f'Замовлення {order_number}',
            },
            'success_url': resolved_success_url,
            'cancel_url': resolved_cancel_url,
        }
        if customer_email:
            session_params['customer_email'] = customer_email

        idempotency_key = (
            f'checkout-order-{order_pk}-retry-{existing_session_id}'
            if existing_session_id
            else f'checkout-order-{order_pk}'
        )
        session = stripe.checkout.Session.create(
            api_key=secret_key,
            idempotency_key=idempotency_key,
            **session_params,
        )
        new_session_id = _get_val(session, 'id')
        if new_session_id:
            with transaction.atomic():
                locked_order = Order.objects.select_for_update().get(pk=order_pk)
                locked_order.stripe_session_id = new_session_id
                locked_order.save(update_fields=['stripe_session_id', 'updated_at'])
                order.stripe_session_id = locked_order.stripe_session_id
                order.updated_at = locked_order.updated_at
        return session

    @classmethod
    def _locate_order_for_update(
        cls,
        *,
        session_id: str = '',
        payment_intent_id: str = '',
        order_number: str = '',
        order_id: Any = None,
    ) -> Order | None:
        order = None
        if session_id:
            order = Order.objects.select_for_update().filter(stripe_session_id=session_id).first()
        if not order and payment_intent_id:
            order = Order.objects.select_for_update().filter(
                stripe_payment_intent_id=payment_intent_id
            ).first()
        if not order and order_number:
            order = Order.objects.select_for_update().filter(order_number=order_number).first()
        if not order and order_id is not None:
            parsed_id = _parse_strict_positive_int(order_id)
            if parsed_id is not None:
                order = Order.objects.select_for_update().filter(id=parsed_id).first()
        return order

    @classmethod
    @transaction.atomic
    def handle_checkout_session_completed(
        cls,
        session: Any,
        target_status: str | None = None,
    ) -> Order | None:
        payment_status = _get_val(session, 'payment_status')
        if payment_status != 'paid':
            logger.warning('Checkout session payment status is not paid: %s', payment_status)
            return None

        client_ref = _get_val(session, 'client_reference_id')
        raw_session_id = _get_val(session, 'id')
        session_id = str(raw_session_id).strip() if raw_session_id else ''
        metadata = _get_val(session, 'metadata') or {}
        payment_intent = _get_val(session, 'payment_intent')

        order_number = client_ref or _get_val(metadata, 'order_number') or ''
        order_id = _get_val(metadata, 'order_id')

        order = cls._locate_order_for_update(
            session_id=session_id,
            order_number=str(order_number) if order_number else '',
            order_id=order_id,
        )
        if not order:
            logger.warning(
                'Order not found for Stripe session: order_number=%s, session_id=%s, order_id=%s',
                order_number,
                session_id,
                order_id,
            )
            return None

        if order.payment_status == PaymentStatus.PAID:
            return order

        pi_id = _resolve_pi_id(payment_intent)
        audit_fields = ['requires_manual_refund', 'notes', 'updated_at']
        if session_id and not order.stripe_session_id:
            order.stripe_session_id = session_id
            audit_fields.append('stripe_session_id')
        if pi_id and not order.stripe_payment_intent_id:
            order.stripe_payment_intent_id = pi_id
            audit_fields.append('stripe_payment_intent_id')

        if order.status == OrderStatus.CANCELLED:
            order.requires_manual_refund = True
            order.notes = _append_audit_note(
                order.notes,
                '[Потребує ручного повернення коштів через Stripe: '
                'оплата надійшла після скасування замовлення]',
            )
            order.save(update_fields=audit_fields)
            logger.error(
                'Stripe payment completed for already CANCELLED order %s (session=%s)',
                order.order_number,
                session_id,
            )
            return order

        amount_total = _get_val(session, 'amount_total')
        currency = _get_val(session, 'currency')
        expected_cents = int(
            ((order.total_amount or Decimal('0.00')) * 100).quantize(Decimal(1))
        )

        amount_valid = False
        if amount_total is not None and not isinstance(amount_total, bool):
            if not (isinstance(amount_total, float) and not amount_total.is_integer()):
                try:
                    amount_valid = int(amount_total) == expected_cents
                except (ValueError, TypeError):
                    amount_valid = False

        currency_valid = bool(currency) and str(currency).strip().lower() == 'uah'
        if not amount_valid or not currency_valid:
            order.requires_manual_refund = True
            order.notes = _append_audit_note(
                order.notes,
                f'[Потребує ручного повернення коштів через Stripe: '
                f'невідповідність суми або валюти ({amount_total}/{currency}, '
                f'очікувалося {expected_cents}/uah)]',
            )
            order.save(update_fields=audit_fields)
            logger.error(
                'Amount or currency mismatch for %s: session=%s/%s, expected=%s/uah',
                order.order_number,
                amount_total,
                currency,
                expected_cents,
            )
            return None

        update_fields = ['status', 'payment_status', 'updated_at']
        if 'stripe_session_id' in audit_fields:
            update_fields.append('stripe_session_id')
        if 'stripe_payment_intent_id' in audit_fields:
            update_fields.append('stripe_payment_intent_id')

        order.payment_status = PaymentStatus.PAID
        if order.can_transition_to(OrderStatus.PAID):
            order.status = OrderStatus.PAID

        success_status = target_status or getattr(
            settings,
            'STRIPE_ORDER_SUCCESS_STATUS',
            OrderStatus.PAID,
        )
        if success_status == OrderStatus.PREPARING and order.can_transition_to(OrderStatus.PREPARING):
            order.status = OrderStatus.PREPARING

        order.save(update_fields=update_fields)
        logger.info(
            'Order %s updated via Stripe webhook: status=%s, payment_status=%s',
            order.order_number,
            order.status,
            order.payment_status,
        )

        from orders.emails import send_order_status_update_email, send_stripe_receipt_email

        if order.status == OrderStatus.PREPARING:
            schedule_on_commit(send_order_status_update_email, order)
        schedule_on_commit(send_stripe_receipt_email, order, session=session)
        return order

    @classmethod
    @transaction.atomic
    def handle_payment_failed(cls, obj: Any, event_type: str | None = None) -> Order | None:
        raw_obj_id = _get_val(obj, 'id')
        obj_id = str(raw_obj_id).strip() if raw_obj_id else ''
        obj_type = _get_val(obj, 'object')
        is_checkout_session = (
            obj_type == 'checkout.session'
            or obj_id.startswith('cs_')
            or event_type in ('checkout.session.async_payment_failed', 'checkout.session.expired')
        )

        metadata = _get_val(obj, 'metadata') or {}
        client_ref = _get_val(obj, 'client_reference_id')
        order_number = client_ref or _get_val(metadata, 'order_number') or ''
        order_id = _get_val(metadata, 'order_id')
        pi_from_obj = _resolve_pi_id(_get_val(obj, 'payment_intent'))

        order = cls._locate_order_for_update(
            session_id=obj_id if is_checkout_session else '',
            payment_intent_id=(
                pi_from_obj
                or (obj_id if not is_checkout_session and not obj_id.startswith('cs_') else '')
            ),
            order_number=str(order_number) if order_number else '',
            order_id=order_id,
        )
        if not order:
            logger.warning('Order not found for failed Stripe payment object: %s', obj_id)
            return None

        if order.payment_status in (
            PaymentStatus.PAID,
            PaymentStatus.REFUNDED,
            PaymentStatus.PARTIALLY_REFUNDED,
        ):
            logger.warning(
                'Ignoring payment failed webhook for order %s with payment_status=%s',
                order.order_number,
                order.payment_status,
            )
            return order

        order.payment_status = PaymentStatus.FAILED
        update_fields = ['payment_status', 'updated_at']
        if is_checkout_session and obj_id and not order.stripe_session_id:
            order.stripe_session_id = obj_id
            update_fields.append('stripe_session_id')
        effective_pi = pi_from_obj or (
            obj_id if not is_checkout_session and obj_id and not obj_id.startswith('cs_') else ''
        )
        if effective_pi and not order.stripe_payment_intent_id:
            order.stripe_payment_intent_id = effective_pi
            update_fields.append('stripe_payment_intent_id')

        order.save(update_fields=update_fields)
        logger.info('Order %s marked as payment failed via Stripe webhook', order.order_number)
        return order

    @classmethod
    @transaction.atomic
    def handle_charge_refunded(cls, charge: Any) -> Order | None:
        pi_id = _resolve_pi_id(_get_val(charge, 'payment_intent'))
        metadata = _get_val(charge, 'metadata') or {}
        order_number = _get_val(metadata, 'order_number') or ''

        order = cls._locate_order_for_update(
            payment_intent_id=pi_id,
            order_number=str(order_number) if order_number else '',
        )
        if not order:
            logger.warning('Order not found for refunded charge: payment_intent=%s', pi_id)
            return None

        refunded = _get_val(charge, 'refunded')
        raw_amount = _get_val(charge, 'amount')
        raw_refunded = _get_val(charge, 'amount_refunded')
        try:
            amount = int(raw_amount) if raw_amount is not None else 0
        except (ValueError, TypeError):
            amount = 0
        try:
            amount_refunded = int(raw_refunded) if raw_refunded is not None else 0
        except (ValueError, TypeError):
            amount_refunded = 0

        if refunded is not True and amount == 0 and amount_refunded == 0:
            logger.info(
                'Ignoring zero-amount refund webhook for order %s',
                order.order_number,
            )
            return order

        order_cents = int(((order.total_amount or Decimal('0.00')) * 100).quantize(Decimal(1)))
        is_full_refund = (
            refunded is True
            or (amount > 0 and amount_refunded >= amount)
            or (amount == 0 and amount_refunded > 0 and order_cents > 0 and amount_refunded >= order_cents)
        )

        update_fields = ['payment_status', 'updated_at']
        if is_full_refund:
            order.payment_status = PaymentStatus.REFUNDED
            if order.status not in (OrderStatus.CANCELLED, OrderStatus.DELIVERED):
                order.status = OrderStatus.CANCELLED
                update_fields.append('status')
        else:
            order.payment_status = PaymentStatus.PARTIALLY_REFUNDED

        order.save(update_fields=update_fields)
        logger.info(
            'Order %s marked as %s via Stripe webhook',
            order.order_number,
            order.payment_status,
        )

        from orders.emails import send_refund_notice_email

        schedule_on_commit(send_refund_notice_email, order, charge=charge)
        return order


class OrderService:
    @staticmethod
    def _recalculate_cart_item(
        item: CartItem,
        options_by_dish: dict[int, dict[int, DishOption]],
    ) -> tuple[Decimal, list[dict[str, Any]]]:
        base_price = _parse_finite_decimal(getattr(item.dish, 'price', None))
        if base_price is None or base_price < Decimal('0.00'):
            raise DishUnavailableError('Некоректна ціна страви.')

        raw_opts = item.selected_options
        if raw_opts is None:
            raw_opts_list: list[Any] = []
        elif isinstance(raw_opts, list):
            raw_opts_list = raw_opts
        else:
            raise InvalidOptionError('Некоректний формат опцій страви.')

        dish_opt_map = options_by_dish.get(item.dish_id, {})
        db_delta = Decimal('0.00')
        verified_options: list[dict[str, Any]] = []

        for entry in raw_opts_list:
            if not isinstance(entry, dict):
                raise InvalidOptionError('Некоректний формат опції страви.')
            raw_id = (
                entry['option_id']
                if 'option_id' in entry and entry['option_id'] is not None
                else entry.get('id')
            )
            opt_id = _parse_strict_positive_int(raw_id)
            if opt_id is None:
                raise InvalidOptionError('Опція не містить коректного ідентифікатора.')
            db_opt = dish_opt_map.get(opt_id)
            if db_opt is None:
                raise InvalidOptionError('Обрана опція більше не доступна для цієї страви.')
            opt_delta = _parse_finite_decimal(db_opt.price_delta)
            if opt_delta is None:
                raise InvalidOptionError('Некоректна вартість опції в меню.')
            db_delta += opt_delta
            verified_options.append({
                'option_id': db_opt.id,
                'name': db_opt.name,
                'price_delta': str(opt_delta),
            })

        unit_price = max(Decimal('0.00'), base_price + db_delta)
        return unit_price, verified_options

    @classmethod
    def create_order_from_cart(
        cls,
        request: HttpRequest,
        customer_name: str,
        customer_phone: str,
        delivery_address: str,
        payment_method: str = PaymentMethod.ONLINE,
        notes: str = '',
        customer_email: str = '',
        create_stripe_session: bool = False,
    ) -> Order:
        name = (customer_name or '').strip()
        phone = normalize_phone(customer_phone)
        address = (delivery_address or '').strip()
        cleaned_notes = (notes or '').strip()

        if not name:
            raise ValidationError({'customer_name': "Вкажіть ваше ім'я."})
        if len(name) > 100:
            raise ValidationError({'customer_name': "Ім'я занадто довге (максимум 100 символів)."})
        if not phone:
            raise ValidationError({'customer_phone': 'Вкажіть контактний номер телефону.'})
        phone_validator(phone)

        if not address:
            raise ValidationError({'delivery_address': 'Вкажіть повну адресу доставки.'})
        if len(address) > 500:
            raise ValidationError({'delivery_address': 'Адреса занадто довга (максимум 500 символів).'})
        if len(cleaned_notes) > 500:
            raise ValidationError({'notes': 'Коментар занадто довгий (максимум 500 символів).'})

        if payment_method not in PaymentMethod.values:
            raise ValidationError({'payment_method': 'Обрано некоректний спосіб оплати.'})

        req_user = getattr(request, 'user', None)
        user = req_user if req_user is not None and req_user.is_authenticated else None

        email = normalize_email(customer_email)
        if not email and user is not None:
            email = normalize_email(getattr(user, 'email', ''))
        if email:
            validate_email(email)

        cart = CartService.get_cart(request)
        if not cart:
            raise CartEmptyError('Кошик порожній. Додайте страви для оформлення замовлення.')

        stale_only_deleted = False
        with transaction.atomic():
            locked_cart = Cart.objects.select_for_update().filter(id=cart.id).first()
            if not locked_cart:
                raise CartEmptyError('Кошик порожній. Додайте страви для оформлення замовлення.')

            all_items = list(
                locked_cart.items.select_related('dish', 'dish__category')
                .select_for_update(of=('self',))
                .all()
            )
            if not all_items:
                raise CartEmptyError('Кошик порожній. Додайте страви для оформлення замовлення.')

            available_items = [item for item in all_items if item.is_available]
            unavailable_items = [item for item in all_items if not item.is_available]

            if not available_items:
                locked_cart.items.filter(id__in=[item.id for item in unavailable_items]).delete()
                CartService._invalidate_cart_cache(request)
                stale_only_deleted = True
            else:
                dish_ids = {item.dish_id for item in available_items}
                options_by_dish: dict[int, dict[int, DishOption]] = {}
                for opt in DishOption.objects.filter(dish_id__in=dish_ids):
                    options_by_dish.setdefault(opt.dish_id, {})[opt.id] = opt

                prepared_items: list[tuple[CartItem, Decimal, list[dict[str, Any]]]] = []
                total_amount = Decimal('0.00')
                for item in available_items:
                    unit_price, verified_options = cls._recalculate_cart_item(item, options_by_dish)
                    total_amount += unit_price * item.quantity
                    prepared_items.append((item, unit_price, verified_options))

                min_order_amount = getattr(settings, 'MIN_ORDER_AMOUNT', Decimal(200))
                if total_amount < min_order_amount:
                    raise OrderMinimumAmountError(
                        f'Мінімальна сума замовлення становить {min_order_amount} грн. '
                        f'Поточна сума: {total_amount} грн.'
                    )

                guest_token = get_guest_token(request, create=True) if user is None else ''
                order = Order.objects.create(
                    user=user,
                    guest_token=guest_token,
                    customer_name=name,
                    customer_phone=phone,
                    customer_email=email,
                    delivery_address=address,
                    payment_method=payment_method,
                    status=OrderStatus.PENDING,
                    payment_status=PaymentStatus.PENDING,
                    total_amount=total_amount,
                    notes=cleaned_notes,
                    eta_minutes=30,
                )

                for item, unit_price, verified_options in prepared_items:
                    OrderItem.objects.create(
                        order=order,
                        dish=item.dish,
                        dish_title=item.dish.title,
                        price=unit_price,
                        quantity=item.quantity,
                        selected_options=verified_options,
                    )

                locked_cart.items.all().delete()
                CartService._invalidate_cart_cache(request)

                from orders.emails import send_order_confirmation_email

                host = request.get_host() if request else None
                proto = 'https' if request and request.is_secure() else 'http'
                schedule_on_commit(
                    send_order_confirmation_email,
                    order,
                    domain=host,
                    protocol=proto,
                )

        if stale_only_deleted:
            raise DishUnavailableError('У вашому кошику немає доступних для замовлення страв.')

        if PaymentMethod.is_online(payment_method) and create_stripe_session:
            try:
                StripeService.create_checkout_session(order, request=request)
            except (stripe.StripeError, ValueError) as exc:
                logger.warning(
                    'Failed to create Stripe session for order %s: %s',
                    order.order_number,
                    exc,
                )

        return order

    @classmethod
    def cancel_order(cls, order: Order) -> None:
        from orders.emails import send_order_status_update_email

        with transaction.atomic():
            locked_order = Order.objects.select_for_update().get(pk=order.pk)
            if locked_order.status == OrderStatus.CANCELLED:
                order.refresh_from_db()
                return
            if not locked_order.can_transition_to(OrderStatus.CANCELLED):
                raise InvalidStatusTransitionError(
                    f'Неприпустимий перехід статусу з {locked_order.status} '
                    f'до {OrderStatus.CANCELLED}.'
                )

            was_paid_or_partial = locked_order.payment_status in (
                PaymentStatus.PAID,
                PaymentStatus.PARTIALLY_REFUNDED,
            )
            needs_stripe_refund = locked_order.is_online_payment and was_paid_or_partial
            pi_id = locked_order.stripe_payment_intent_id
            order_pk = locked_order.pk
            order_number = locked_order.order_number

            locked_order.status = OrderStatus.CANCELLED
            locked_order.save(update_fields=['status', 'updated_at'])
            schedule_on_commit(send_order_status_update_email, locked_order)

        if needs_stripe_refund:
            refund_succeeded = False
            secret_key = StripeService.get_stripe_secret_key()
            if pi_id and secret_key:
                try:
                    stripe.Refund.create(
                        payment_intent=pi_id,
                        idempotency_key=f'refund-order-{order_pk}',
                        api_key=secret_key,
                    )
                    refund_succeeded = True
                except Exception as exc:
                    logger.error(
                        'Failed to refund Stripe payment for cancelled order %s: %s',
                        order_number,
                        exc,
                    )

            with transaction.atomic():
                locked_order = Order.objects.select_for_update().get(pk=order_pk)
                if refund_succeeded:
                    locked_order.payment_status = PaymentStatus.REFUNDED
                    locked_order.save(update_fields=['payment_status', 'updated_at'])
                else:
                    locked_order.requires_manual_refund = True
                    locked_order.notes = _append_audit_note(
                        locked_order.notes,
                        _MANUAL_REFUND_NOTE,
                    )
                    locked_order.save(
                        update_fields=['requires_manual_refund', 'notes', 'updated_at']
                    )

        order.refresh_from_db()
