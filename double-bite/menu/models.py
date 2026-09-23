from decimal import Decimal
from typing import Any

from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.fields.files import ImageFieldFile
from django.utils.text import slugify
from pgvector.django import HnswIndex, VectorField


class DishImageFieldFile(ImageFieldFile):
    @property
    def url(self) -> str:
        if not self.name:
            return ''
        if self.name.startswith(('http://', 'https://', '//', '/', 'data:')):
            return self.name
        if not self.name.endswith('.webp'):
            webp_name = self.name.rsplit('.', 1)[0] + '.webp'
            try:
                if self.storage.exists(webp_name):
                    return self.storage.url(webp_name)
            except Exception:
                pass
        return super().url

    @property
    def path(self) -> str:
        if not self.name or self.name.startswith(('http://', 'https://', '//', '/', 'data:')):
            return ''
        try:
            return super().path
        except (ValueError, OSError, SuspiciousFileOperation):
            return ''


class DishImageField(models.ImageField):
    attr_class = DishImageFieldFile

    def deconstruct(self) -> tuple[Any, ...]:
        name, _path, args, kwargs = super().deconstruct()
        return name, 'django.db.models.ImageField', args, kwargs


class Category(models.Model):
    name = models.CharField('Назва', max_length=100)
    slug = models.SlugField('Slug', max_length=120, unique=True, allow_unicode=True)
    icon = models.CharField('Іконка', max_length=50, blank=True, default='')
    display_order = models.PositiveIntegerField('Порядок відображення', default=0)
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Категорія'
        verbose_name_plural = 'Категорії'
        ordering = ('display_order', 'name')

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.slug and self.name:
            generated = slugify(self.name, allow_unicode=True)
            self.slug = generated or self.name.lower()
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class Dish(models.Model):
    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name='dishes',
        verbose_name='Категорія',
    )
    title = models.CharField('Назва страви', max_length=200)
    slug = models.SlugField('Slug', max_length=220, unique=True, allow_unicode=True)
    description = models.TextField('Опис', blank=True)
    price = models.DecimalField(
        'Ціна (грн)',
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
    )
    image = DishImageField(
        'Зображення',
        upload_to='dishes/',
        max_length=500,
        blank=True,
        null=True,
    )
    weight_grams = models.PositiveIntegerField(
        'Вага (г)',
        validators=[MinValueValidator(1)],
    )
    calories = models.PositiveIntegerField('Калорійність (ккал)', default=0)
    allergens = models.CharField('Алергени', max_length=255, blank=True)
    is_vegetarian = models.BooleanField('Вегетаріанська', default=False)
    is_spicy = models.BooleanField('Гостра', default=False)
    is_available = models.BooleanField('В наявності', default=True, db_index=True)
    embedding = VectorField(
        'Векторний ембедінг',
        dimensions=1024,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = 'Страва'
        verbose_name_plural = 'Страви'
        ordering = ('title',)
        indexes = [
            HnswIndex(
                name='dish_embedding_hnsw_idx',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.price is not None and self.price <= Decimal('0.00'):
            raise ValidationError({'price': 'Ціна повинна бути більшою за 0.'})
        if self.weight_grams is not None and self.weight_grams <= 0:
            raise ValidationError({'weight_grams': 'Вага повинна бути більшою за 0.'})

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.slug and self.title:
            generated = slugify(self.title, allow_unicode=True)
            self.slug = generated or self.title.lower()
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.title} ({self.price} грн)"


class DishOption(models.Model):
    dish = models.ForeignKey(
        Dish,
        on_delete=models.CASCADE,
        related_name='options',
        verbose_name='Страва',
    )
    name = models.CharField('Назва опції / модифікатора', max_length=100)
    price_delta = models.DecimalField(
        'Зміна ціни (грн)',
        max_digits=8,
        decimal_places=2,
        default=Decimal('0.00'),
    )

    class Meta:
        verbose_name = 'Опція страви'
        verbose_name_plural = 'Опції страв'
        ordering = ('dish', 'price_delta', 'name')

    def clean(self) -> None:
        super().clean()
        if self.dish_id and self.dish.price + self.price_delta < Decimal('0.00'):
            raise ValidationError({'price_delta': 'Підсумкова ціна страви не може бути меншою за 0.'})

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.dish.title} - {self.name} (+{self.price_delta} грн)"
