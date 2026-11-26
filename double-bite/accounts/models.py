import threading
from contextlib import nullcontext
from typing import Any

from config.validators import normalize_email, normalize_phone, phone_validator
from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models, transaction
from django.db.models import Q
from django.db.models.functions import Lower

_SQLITE_ADDRESS_LOCK = threading.Lock()


class Role(models.TextChoices):
    CUSTOMER = 'CUSTOMER', 'Клієнт'
    RESTAURANT_ADMIN = 'RESTAURANT_ADMIN', 'Менеджер'
    COURIER = 'COURIER', "Кур'єр"


class CustomUserManager(BaseUserManager):
    def create_user(
        self, email: str, password: str | None = None, **extra_fields: Any
    ) -> 'User':
        normalized_email = normalize_email(email)
        if not normalized_email:
            raise ValueError("Email є обов'язковим полем")
        skip_email_address = bool(extra_fields.pop('_skip_email_address', False))
        if 'phone' in extra_fields and extra_fields['phone'] is not None:
            extra_fields['phone'] = normalize_phone(extra_fields['phone'])
        extra_fields.setdefault('role', Role.CUSTOMER)
        user = self.model(email=normalized_email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        if not skip_email_address:
            from allauth.account.models import EmailAddress

            EmailAddress.objects.using(self._db).update_or_create(
                user=user,
                email=user.email,
                defaults={'verified': True, 'primary': True},
            )
        return user

    def create_superuser(
        self, email: str, password: str | None = None, **extra_fields: Any
    ) -> 'User':
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', Role.RESTAURANT_ADMIN)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Суперкористувач повинен мати is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Суперкористувач повинен мати is_superuser=True.')

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    username = None
    email = models.EmailField('Електронна пошта', unique=True)
    phone = models.CharField(
        'Номер телефону',
        max_length=20,
        blank=True,
        validators=[phone_validator],
    )
    role = models.CharField(
        'Роль',
        max_length=20,
        choices=Role.choices,
        default=Role.CUSTOMER,
    )
    telegram_id = models.BigIntegerField(
        'Telegram ID',
        null=True,
        blank=True,
        unique=True,
    )
    created_at = models.DateTimeField('Дата створення', auto_now_add=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS: list[str] = []

    objects = CustomUserManager()

    class Meta:
        verbose_name = 'Користувач'
        verbose_name_plural = 'Користувачі'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                Lower('email'),
                name='unique_user_email_ci',
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        update_fields = kwargs.get('update_fields')
        if not update_fields or 'email' in update_fields:
            self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.email


class DeliveryAddress(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='addresses',
        verbose_name='Користувач',
    )
    title = models.CharField('Назва адреси', max_length=50, default='Дім')
    city = models.CharField('Місто', max_length=100, default='Київ')
    street = models.CharField('Вулиця', max_length=255)
    building = models.CharField('Будинок', max_length=20)
    apartment = models.CharField('Квартира / Офіс', max_length=20, blank=True)
    floor = models.CharField('Поверх', max_length=10, blank=True)
    intercom = models.CharField('Домофон', max_length=20, blank=True)
    is_default = models.BooleanField('Основна адреса', default=False)
    created_at = models.DateTimeField('Дата створення', auto_now_add=True)

    class Meta:
        verbose_name = 'Адреса доставки'
        verbose_name_plural = 'Адреси доставки'
        ordering = ['-is_default', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user'],
                condition=Q(is_default=True),
                name='unique_default_delivery_address_per_user',
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.is_default and self.user_id:
            db_conn = transaction.get_connection(using=kwargs.get('using'))
            use_sqlite_lock = (
                db_conn.vendor == 'sqlite' and not db_conn.in_atomic_block
            )
            guard = _SQLITE_ADDRESS_LOCK if use_sqlite_lock else nullcontext()
            with guard, transaction.atomic():
                User.objects.select_for_update().filter(pk=self.user_id).exists()
                list(
                    DeliveryAddress.objects.select_for_update().filter(
                        user_id=self.user_id
                    )
                )
                other_defaults = DeliveryAddress.objects.filter(
                    user_id=self.user_id,
                    is_default=True,
                )
                if self.pk is not None:
                    other_defaults = other_defaults.exclude(pk=self.pk)
                other_defaults.update(is_default=False)
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def format_full_address(self) -> str:
        """Build a clean comma-separated delivery address string omitting blank parts."""
        parts: list[str] = []
        city = (self.city or '').strip()
        if city:
            parts.append(city)
        street_building = f"{(self.street or '').strip()} {(self.building or '').strip()}".strip()
        if street_building:
            parts.append(street_building)
        apartment = (self.apartment or '').strip()
        if apartment:
            parts.append(f'кв. {apartment}')
        entrance = (getattr(self, 'entrance', '') or '').strip()
        if entrance:
            parts.append(f"під'їзд {entrance}")
        floor = (self.floor or '').strip()
        if floor:
            parts.append(f'поверх {floor}')
        intercom = (self.intercom or '').strip()
        if intercom:
            parts.append(f'домофон {intercom}')
        return ', '.join(parts)

    def __str__(self) -> str:
        return f'{self.title}: {self.city}, {self.street} {self.building}'
