from typing import Any

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    CUSTOMER = 'CUSTOMER', 'Клієнт'
    RESTAURANT_ADMIN = 'RESTAURANT_ADMIN', 'Менеджер'
    COURIER = 'COURIER', 'Кур\'єр'


class CustomUserManager(BaseUserManager):
    def create_user(
        self, email: str, password: str | None = None, **extra_fields: Any
    ) -> 'User':
        if not email:
            raise ValueError('Email є обов\'язковим полем')
        email = self.normalize_email(email)
        extra_fields.setdefault('role', Role.CUSTOMER)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
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
    phone = models.CharField('Номер телефону', max_length=20, blank=True)
    role = models.CharField(
        'Роль',
        max_length=20,
        choices=Role.choices,
        default=Role.CUSTOMER,
    )
    created_at = models.DateTimeField('Дата створення', auto_now_add=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS: list[str] = []

    objects = CustomUserManager()

    class Meta:
        verbose_name = 'Користувач'
        verbose_name_plural = 'Користувачі'
        ordering = ['-created_at']

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

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.is_default and self.user_id:
            DeliveryAddress.objects.filter(
                user_id=self.user_id, is_default=True
            ).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.title}: {self.city}, {self.street} {self.building}"
