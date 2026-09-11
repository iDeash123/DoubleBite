from decimal import Decimal
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpRequest
from django.shortcuts import get_object_or_404

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

User = get_user_model()


class CartService:
    @staticmethod
    def get_or_create_cart(request: HttpRequest) -> Cart:
        if not request.session.session_key:
            request.session.create()

        if request.user.is_authenticated:
            cart, _ = Cart.objects.get_or_create(user=request.user)
            return cart

        cart, _ = Cart.objects.get_or_create(session_key=request.session.session_key)
        return cart

    @staticmethod
    def get_cart(request: HttpRequest) -> Cart | None:
        if request.user.is_authenticated:
            return Cart.objects.filter(user=request.user).first()
        if request.session.session_key:
            return Cart.objects.filter(session_key=request.session.session_key).first()
        return None

    @classmethod
    def get_items_count(cls, request: HttpRequest) -> int:
        cart = cls.get_cart(request)
        if not cart:
            return 0
        return cart.total_quantity

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
        if quantity > 99:
            quantity = 99

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
            # Find item matching dish and identical options
            for item in cart.items.select_for_update().filter(dish=dish):
                if item.selected_options == options_list:
                    item.quantity = min(99, item.quantity + quantity)
                    item.save(update_fields=['quantity', 'updated_at'])
                    return item

            return CartItem.objects.create(
                cart=cart,
                dish=dish,
                quantity=quantity,
                selected_options=options_list,
            )

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

        if quantity <= 0:
            item.delete()
            return None

        if quantity > 99:
            quantity = 99

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
            item.delete()
            return True
        return False

    @classmethod
    def clear_cart(cls, request: HttpRequest) -> None:
        cart = cls.get_cart(request)
        if cart:
            cart.items.all().delete()

    @classmethod
    def merge_guest_cart(cls, request: HttpRequest, user: Any) -> None:
        session_key = getattr(request.session, 'session_key', None)
        if not session_key:
            return

        guest_cart = (
            Cart.objects.filter(session_key=session_key)
            .exclude(user__isnull=False)
            .first()
        )
        if not guest_cart:
            return

        with transaction.atomic():
            user_cart, _ = Cart.objects.get_or_create(user=user)

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
    ) -> Order:
        import os
        from django.core.exceptions import ValidationError
        from .exceptions import CartEmptyError, OrderMinimumAmountError
        from .models import Order, OrderItem, OrderStatus, PaymentStatus

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

            # Clear cart items upon successful order placement
            cart.items.all().delete()

        return order

    @classmethod
    def cancel_order(cls, order: Order) -> None:
        from .models import OrderStatus

        order.transition_to(OrderStatus.CANCELLED)
