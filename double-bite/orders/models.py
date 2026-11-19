import logging
import secrets
from decimal import Decimal, InvalidOperation
from typing import Any

from config.transactions import schedule_on_commit
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Q
from menu.models import Dish

from .exceptions import InvalidStatusTransitionError

logger = logging.getLogger('orders')


class OrderStatus(models.TextChoices):
    PENDING = 'PENDING', 'Очікує оплати'
    PAID = 'PAID', 'Оплачено'
    PREPARING = 'PREPARING', 'Готується на кухні'
    ON_WAY = 'ON_WAY', "Кур'єр у дорозі"
    DELIVERED = 'DELIVERED', 'Доставлено'
    CANCELLED = 'CANCELLED', 'Скасовано'


class PaymentMethod(models.TextChoices):
    ONLINE = 'ONLINE', 'Онлайн-оплата карткою (Stripe)'
    CASH = 'CASH', "Готівкою кур'єру"
    CARD = 'CARD', "Терміналом кур'єру"

    @classmethod
    def is_online(cls, method: str | None) -> bool:
        return method in (cls.ONLINE, 'ONLINE')


class PaymentStatus(models.TextChoices):
    PENDING = 'PENDING', 'Очікує оплати'
    PAID = 'PAID', 'Оплачено'
    FAILED = 'FAILED', 'Помилка оплати'
    REFUNDED = 'REFUNDED', 'Повернено'
    PARTIALLY_REFUNDED = 'PARTIALLY_REFUNDED', 'Частково повернено'


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    OrderStatus.PENDING: {OrderStatus.PAID, OrderStatus.CANCELLED},
    OrderStatus.PAID: {OrderStatus.PREPARING, OrderStatus.CANCELLED},
    OrderStatus.PREPARING: {OrderStatus.ON_WAY},
    OrderStatus.ON_WAY: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}


class Cart(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='carts',
        verbose_name='Користувач',
    )
    guest_token = models.CharField(
        'Токен гостя',
        max_length=64,
        blank=True,
        default='',
        db_index=True,
    )
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = 'Кошик'
        verbose_name_plural = 'Кошики'
        ordering = ('-updated_at',)
        constraints = [
            models.UniqueConstraint(
                fields=['user'],
                condition=Q(user__isnull=False),
                name='unique_cart_per_user',
            ),
            models.UniqueConstraint(
                fields=['guest_token'],
                condition=Q(user__isnull=True) & ~Q(guest_token=''),
                name='unique_guest_cart_per_token',
            ),
        ]

    @property
    def total_quantity(self) -> int:
        if not self.pk:
            return 0
        return sum(
            item.quantity or 0
            for item in self.items.all()
            if item.is_available
        )

    @property
    def total_amount(self) -> Decimal:
        if not self.pk:
            return Decimal('0.00')
        return sum(
            (
                item.total_price
                for item in self.items.all()
                if item.is_available
            ),
            Decimal('0.00'),
        )

    def __str__(self) -> str:
        owner = self.user.email if self.user else f"Гість ({self.guest_token[:8]})"
        return f"Кошик #{self.id} — {owner}"


class CartItem(models.Model):
    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name='Кошик',
    )
    dish = models.ForeignKey(
        Dish,
        on_delete=models.CASCADE,
        related_name='cart_items',
        verbose_name='Страва',
    )
    quantity = models.PositiveIntegerField(
        'Кількість',
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(99)],
    )
    selected_options = models.JSONField(
        'Обрані опції',
        default=list,
        blank=True,
    )
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = 'Позиція кошика'
        verbose_name_plural = 'Позиції кошика'
        ordering = ('created_at',)

    @property
    def unit_price(self) -> Decimal:
        try:
            if not self.dish_id or not self.dish:
                return Decimal('0.00')
            base = getattr(self.dish, 'price', None)
            if base is None or isinstance(base, bool):
                return Decimal('0.00')
            base_dec = Decimal(str(base))
            if not base_dec.is_finite():
                return Decimal('0.00')
        except (ObjectDoesNotExist, InvalidOperation, TypeError, ValueError):
            return Decimal('0.00')

        delta = Decimal('0.00')
        if isinstance(self.selected_options, list):
            for opt in self.selected_options:
                if not isinstance(opt, dict):
                    continue
                val = opt.get('price_delta', 0)
                if val is None or isinstance(val, bool):
                    continue
                try:
                    opt_delta = Decimal(str(val))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if not opt_delta.is_finite():
                    continue
                delta += opt_delta
        return max(Decimal('0.00'), base_dec + delta)

    @property
    def total_price(self) -> Decimal:
        if self.quantity is None:
            return Decimal('0.00')
        unit = self.unit_price
        if unit is None:
            return Decimal('0.00')
        return unit * self.quantity

    @property
    def is_available(self) -> bool:
        try:
            if not self.dish_id or not self.dish:
                return False
            return bool(
                self.dish.is_available
                and self.dish.category
                and self.dish.category.is_active
            )
        except ObjectDoesNotExist:
            return False

    def __str__(self) -> str:
        try:
            title = self.dish.title if self.dish_id and self.dish else 'Позиція кошика'
        except ObjectDoesNotExist:
            title = 'Позиція кошика'
        return f"{title} x {self.quantity or 0}"


class Order(models.Model):
    order_number = models.CharField(
        'Номер замовлення',
        max_length=32,
        unique=True,
        blank=True,
        db_index=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='orders',
        verbose_name='Користувач',
    )
    guest_token = models.CharField(
        'Токен гостя',
        max_length=64,
        blank=True,
        default='',
        db_index=True,
    )
    access_token = models.CharField(
        'Токен доступу',
        max_length=64,
        unique=True,
        editable=False,
        default=secrets.token_urlsafe,
        db_index=True,
    )
    customer_name = models.CharField("Ім'я клієнта", max_length=100)
    customer_phone = models.CharField('Телефон клієнта', max_length=20)
    customer_email = models.EmailField('Email клієнта', blank=True, default='')
    delivery_address = models.TextField('Адреса доставки')
    status = models.CharField(
        'Статус замовлення',
        max_length=20,
        choices=OrderStatus.choices,
        default=OrderStatus.PENDING,
        db_index=True,
    )
    payment_method = models.CharField(
        'Спосіб оплати',
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.ONLINE,
    )
    payment_status = models.CharField(
        'Статус оплати',
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
    )
    stripe_session_id = models.CharField(
        'ID сесії Stripe',
        max_length=255,
        blank=True,
        db_index=True,
    )
    stripe_payment_intent_id = models.CharField(
        'ID платежу Stripe (PaymentIntent)',
        max_length=255,
        blank=True,
    )
    total_amount = models.DecimalField(
        'Загальна сума (грн)',
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
    )
    eta_minutes = models.PositiveIntegerField(
        'Орієнтовний час доставки (хв)',
        default=30,
    )
    notes = models.TextField('Коментар до замовлення', blank=True)
    requires_manual_refund = models.BooleanField(
        'Потребує ручного повернення коштів',
        default=False,
    )
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = 'Замовлення'
        verbose_name_plural = 'Замовлення'
        ordering = ('-created_at',)

    @property
    def is_online_payment(self) -> bool:
        return PaymentMethod.is_online(self.payment_method)

    @property
    def masked_customer_phone(self) -> str:
        phone = self.customer_phone or ''
        digits = [c for c in phone if c.isdigit()]
        if len(digits) >= 10:
            last2 = ''.join(digits[-2:])
            return f"+380 •• ••• •• {last2}"
        if len(phone) > 4:
            return phone[:2] + '•' * (len(phone) - 4) + phone[-2:]
        return phone

    def can_transition_to(self, target_status: str) -> bool:
        if self.status == OrderStatus.PENDING:
            if self.is_online_payment:
                return target_status in {OrderStatus.PAID, OrderStatus.CANCELLED}
            return target_status in {
                OrderStatus.PAID,
                OrderStatus.PREPARING,
                OrderStatus.CANCELLED,
            }
        return target_status in ALLOWED_TRANSITIONS.get(self.status, set())

    def transition_to(self, target_status: str) -> None:
        with transaction.atomic():
            locked_order = Order.objects.select_for_update().get(pk=self.pk)
            if not locked_order.can_transition_to(target_status):
                raise InvalidStatusTransitionError(
                    f"Неприпустимий перехід статусу з {locked_order.status} до {target_status}."
                )
            locked_order.status = target_status
            if target_status == OrderStatus.PAID:
                locked_order.payment_status = PaymentStatus.PAID
            elif (
                target_status == OrderStatus.DELIVERED
                and not locked_order.is_online_payment
                and locked_order.payment_status == PaymentStatus.PENDING
            ):
                locked_order.payment_status = PaymentStatus.PAID
            locked_order.save(update_fields=['status', 'payment_status', 'updated_at'])
            self.status = locked_order.status
            self.payment_status = locked_order.payment_status
            self.updated_at = locked_order.updated_at

            if target_status in (
                OrderStatus.PREPARING,
                OrderStatus.ON_WAY,
                OrderStatus.DELIVERED,
                OrderStatus.CANCELLED,
            ):
                from orders.emails import send_order_status_update_email

                schedule_on_commit(send_order_status_update_email, locked_order)

    def save(self, *args: Any, **kwargs: Any) -> None:
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.order_number:
            self.order_number = f"DB-{self.pk:04d}"
            super().save(update_fields=['order_number'])

    def __str__(self) -> str:
        return f"{self.order_number or f'Order #{self.pk}'} ({self.get_status_display()}) — {self.total_amount} грн"


class OrderItem(models.Model):
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name='Замовлення',
    )
    dish = models.ForeignKey(
        Dish,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='order_items',
        verbose_name='Страва',
    )
    dish_title = models.CharField('Назва страви (снапшот)', max_length=200)
    price = models.DecimalField(
        'Ціна (снапшот, грн)',
        max_digits=8,
        decimal_places=2,
    )
    quantity = models.PositiveIntegerField('Кількість', default=1)
    selected_options = models.JSONField(
        'Обрані опції (снапшот)',
        default=list,
        blank=True,
    )

    class Meta:
        verbose_name = 'Позиція замовлення'
        verbose_name_plural = 'Позиції замовлення'

    @property
    def unit_price(self) -> Decimal:
        return self.price if self.price is not None else Decimal('0.00')

    @property
    def total_price(self) -> Decimal:
        if self.price is None or self.quantity is None:
            return Decimal('0.00')
        return self.price * self.quantity

    def __str__(self) -> str:
        dish_title = self.dish_title or 'Позиція'
        qty = self.quantity or 0
        price_str = f"{self.price} грн" if self.price is not None else '0.00 грн'
        return f"{dish_title} x {qty} ({price_str})"


class ProcessedStripeEvent(models.Model):
    event_id = models.CharField('ID події Stripe', max_length=255, unique=True, db_index=True)
    event_type = models.CharField('Тип події', max_length=100)
    created_at = models.DateTimeField('Створено', auto_now_add=True)

    class Meta:
        verbose_name = 'Оброблена подія Stripe'
        verbose_name_plural = 'Оброблені події Stripe'
        ordering = ('-created_at',)

    def __str__(self) -> str:
        return f"{self.event_type}: {self.event_id}"
