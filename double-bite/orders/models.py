from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from menu.models import Dish
from .exceptions import InvalidStatusTransitionError


class OrderStatus(models.TextChoices):
    PENDING = 'PENDING', 'Очікує оплати'
    PAID = 'PAID', 'Оплачено'
    PREPARING = 'PREPARING', 'Готується на кухні'
    ON_WAY = 'ON_WAY', "Кур'єр у дорозі"
    DELIVERED = 'DELIVERED', 'Доставлено'
    CANCELLED = 'CANCELLED', 'Скасовано'


class PaymentMethod(models.TextChoices):
    CARD = 'CARD', 'Картка'
    CASH = 'CASH', 'Готівка'


class PaymentStatus(models.TextChoices):
    PENDING = 'PENDING', 'Очікує оплати'
    COMPLETED = 'COMPLETED', 'Оплачено'


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
    session_key = models.CharField(
        'Ключ сесії',
        max_length=40,
        blank=True,
        db_index=True,
    )
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = 'Кошик'
        verbose_name_plural = 'Кошики'
        ordering = ('-updated_at',)

    @property
    def total_quantity(self) -> int:
        return sum(
            item.quantity
            for item in self.items.all()
            if item.dish.is_available and item.dish.category.is_active
        )

    @property
    def total_amount(self) -> Decimal:
        return sum(
            (
                item.total_price
                for item in self.items.all()
                if item.dish.is_available and item.dish.category.is_active
            ),
            Decimal('0.00'),
        )

    def __str__(self) -> str:
        owner = self.user.email if self.user else f"Гість ({self.session_key[:8]})"
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
        base = self.dish.price
        delta = Decimal('0.00')
        if isinstance(self.selected_options, list):
            for opt in self.selected_options:
                if isinstance(opt, dict):
                    val = opt.get('price_delta', 0)
                    try:
                        delta += Decimal(str(val))
                    except (InvalidOperation, TypeError):
                        pass
        return base + delta

    @property
    def total_price(self) -> Decimal:
        return self.unit_price * self.quantity

    @property
    def is_available(self) -> bool:
        return bool(self.dish.is_available and self.dish.category.is_active)

    def __str__(self) -> str:
        return f"{self.dish.title} x {self.quantity}"


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
    session_key = models.CharField(
        'Ключ сесії гостя',
        max_length=40,
        blank=True,
        db_index=True,
    )
    customer_name = models.CharField("Ім'я клієнта", max_length=100)
    customer_phone = models.CharField('Телефон клієнта', max_length=20)
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
        max_length=10,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CARD,
    )
    payment_status = models.CharField(
        'Статус оплати',
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
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
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = 'Замовлення'
        verbose_name_plural = 'Замовлення'
        ordering = ('-created_at',)

    def can_transition_to(self, target_status: str) -> bool:
        return target_status in ALLOWED_TRANSITIONS.get(self.status, set())

    def transition_to(self, target_status: str) -> None:
        if not self.can_transition_to(target_status):
            raise InvalidStatusTransitionError(
                f"Неприпустимий перехід статусу з {self.status} до {target_status}."
            )
        self.status = target_status
        if target_status == OrderStatus.PAID:
            self.payment_status = PaymentStatus.COMPLETED
        self.save(update_fields=['status', 'payment_status', 'updated_at'])

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
    def total_price(self) -> Decimal:
        return self.price * self.quantity

    def __str__(self) -> str:
        return f"{self.dish_title} x {self.quantity} ({self.price} грн)"
