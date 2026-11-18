import logging
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.validators import MinValueValidator, validate_slug
from django.db import models
from django.db.models.fields.files import ImageFieldFile
from django.urls import reverse
from django.utils.text import slugify
from pgvector.django import HnswIndex, VectorField

logger = logging.getLogger('menu')

_CYRILLIC_APOSTROPHE_RE = re.compile(
    r"(?<=[а-яА-ЯіїєґІЇЄҐёъыэЁЪЫЭ])['’ʼ`](?=[а-яА-ЯіїєґІЇЄҐёъыэЁЪЫЭ])"
)

_INITIAL_CYRILLIC_MAP: dict[str, str] = {
    'є': 'ye',
    'Є': 'Ye',
    'ї': 'yi',
    'Ї': 'Yi',
    'й': 'y',
    'Й': 'Y',
    'ю': 'yu',
    'Ю': 'Yu',
    'я': 'ya',
    'Я': 'Ya',
}

_CYRILLIC_MAP: dict[str, str] = {
    'а': 'a',
    'А': 'A',
    'б': 'b',
    'Б': 'B',
    'в': 'v',
    'В': 'V',
    'г': 'h',
    'Г': 'H',
    'ґ': 'g',
    'Ґ': 'G',
    'д': 'd',
    'Д': 'D',
    'е': 'e',
    'Е': 'E',
    'є': 'ie',
    'Є': 'Ie',
    'ж': 'zh',
    'Ж': 'Zh',
    'з': 'z',
    'З': 'Z',
    'и': 'y',
    'И': 'Y',
    'і': 'i',
    'І': 'I',
    'ї': 'i',
    'Ї': 'I',
    'й': 'i',
    'Й': 'I',
    'к': 'k',
    'К': 'K',
    'л': 'l',
    'Л': 'L',
    'м': 'm',
    'М': 'M',
    'н': 'n',
    'Н': 'N',
    'о': 'o',
    'О': 'O',
    'п': 'p',
    'П': 'P',
    'р': 'r',
    'Р': 'R',
    'с': 's',
    'С': 'S',
    'т': 't',
    'Т': 'T',
    'у': 'u',
    'У': 'U',
    'ф': 'f',
    'Ф': 'F',
    'х': 'kh',
    'Х': 'Kh',
    'ц': 'ts',
    'Ц': 'Ts',
    'ч': 'ch',
    'Ч': 'Ch',
    'ш': 'sh',
    'Ш': 'Sh',
    'щ': 'shch',
    'Щ': 'Shch',
    'ь': '',
    'Ь': '',
    'ю': 'iu',
    'Ю': 'Iu',
    'я': 'ia',
    'Я': 'Ia',
    'ё': 'yo',
    'Ё': 'Yo',
    'ъ': '',
    'Ъ': '',
    'ы': 'y',
    'Ы': 'Y',
    'э': 'e',
    'Э': 'E',
}


def transliterate_to_ascii(text: str) -> str:
    if not text:
        return ''
    cleaned = _CYRILLIC_APOSTROPHE_RE.sub('', str(text))
    parts: list[str] = []
    idx = 0
    length = len(cleaned)
    while idx < length:
        pair = cleaned[idx : idx + 2]
        if pair == 'зг':
            parts.append('zgh')
            idx += 2
            continue
        if pair == 'Зг':
            parts.append('Zgh')
            idx += 2
            continue
        if pair == 'ЗГ':
            parts.append('ZGH')
            idx += 2
            continue
        ch = cleaned[idx]
        is_word_start = idx == 0 or not cleaned[idx - 1].isalpha()
        if is_word_start and ch in _INITIAL_CYRILLIC_MAP:
            parts.append(_INITIAL_CYRILLIC_MAP[ch])
        elif ch in _CYRILLIC_MAP:
            parts.append(_CYRILLIC_MAP[ch])
        elif ch.isascii():
            parts.append(ch)
        else:
            parts.append(unicodedata.normalize('NFKD', ch).encode('ascii', 'ignore').decode('ascii'))
        idx += 1
    return ''.join(parts)


def _generate_unique_slug(
    model_cls: type[models.Model],
    source_text: str,
    *,
    fallback: str,
    max_length: int,
    instance_pk: Any = None,
) -> str:
    base = slugify(transliterate_to_ascii(source_text), allow_unicode=False) or fallback
    base = base[:max_length].rstrip('-') or fallback[:max_length]
    candidate = base
    counter = 2
    queryset = model_cls._default_manager.all()
    if instance_pk is not None:
        queryset = queryset.exclude(pk=instance_pk)
    while queryset.filter(slug=candidate).exists():
        suffix = f'-{counter}'
        trimmed = base[: max_length - len(suffix)].rstrip('-') or fallback[: max_length - len(suffix)]
        candidate = f'{trimmed}{suffix}'
        counter += 1
    return candidate


class DishImageFieldFile(ImageFieldFile):
    @property
    def url(self) -> str:
        if not self.name or self.name.lstrip().lower().startswith('data:'):
            return ''
        cached = getattr(self, '_resolved_url_cache', None)
        if cached is not None and cached[0] == self.name:
            return cached[1]
        if self.name.startswith(('http://', 'https://', '//', '/')):
            resolved = self.name
            self._resolved_url_cache = (self.name, resolved)
            return resolved
        resolved = ''
        if not self.name.lower().endswith('.webp'):
            webp_name = self.name.rsplit('.', 1)[0] + '.webp'
            try:
                if self.storage.exists(webp_name):
                    resolved = self.storage.url(webp_name)
            except (OSError, ValueError, SuspiciousFileOperation) as exc:
                logger.warning('Failed to resolve WebP image URL for %s: %s', self.name, exc)
        if not resolved:
            try:
                resolved = super().url
            except (OSError, ValueError, SuspiciousFileOperation) as exc:
                logger.warning('Failed to resolve image URL for %s: %s', self.name, exc)
                resolved = ''
        self._resolved_url_cache = (self.name, resolved)
        return resolved

    @property
    def path(self) -> str:
        if (
            not self.name
            or self.name.lstrip().lower().startswith('data:')
            or self.name.startswith(('http://', 'https://', '//', '/'))
        ):
            return ''
        try:
            return super().path
        except (ValueError, OSError, SuspiciousFileOperation) as exc:
            logger.warning('Failed to resolve image path for %s: %s', self.name, exc)
            return ''


class DishImageField(models.ImageField):
    attr_class = DishImageFieldFile

    def deconstruct(self) -> tuple[Any, ...]:
        name, _path, args, kwargs = super().deconstruct()
        return name, 'django.db.models.ImageField', args, kwargs


class Category(models.Model):
    name = models.CharField('Назва', max_length=100)
    slug = models.SlugField('Slug', max_length=120, unique=True)
    icon = models.CharField('Іконка', max_length=50, blank=True, default='')
    display_order = models.PositiveIntegerField('Порядок відображення', default=0)
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Категорія'
        verbose_name_plural = 'Категорії'
        ordering = ('display_order', 'name')
        indexes = [
            models.Index(fields=['is_active', 'display_order'], name='cat_active_order_idx'),
        ]

    def clean(self) -> None:
        super().clean()
        if self.slug:
            validate_slug(self.slug)

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.slug:
            self.slug = _generate_unique_slug(
                Category,
                self.name,
                fallback='category-item',
                max_length=120,
                instance_pk=self.pk,
            )
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse('menu:dish_detail', kwargs={'dish_slug': self.slug})


class Dish(models.Model):
    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name='dishes',
        verbose_name='Категорія',
    )
    title = models.CharField('Назва страви', max_length=200)
    slug = models.SlugField('Slug', max_length=220, unique=True)
    description = models.TextField('Опис', blank=True)
    price = models.DecimalField(
        'Ціна (грн)',
        max_digits=8,
        decimal_places=2,
        db_index=True,
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
    calories = models.PositiveIntegerField('Калорійність (ккал)', default=0, db_index=True)
    allergens = models.CharField('Алергени', max_length=255, blank=True)
    is_vegetarian = models.BooleanField('Вегетаріанська', default=False, db_index=True)
    is_spicy = models.BooleanField('Гостра', default=False, db_index=True)
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
            models.Index(fields=['is_available', 'category'], name='dish_avail_cat_idx'),
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
        if self.slug:
            validate_slug(self.slug)
        if self.price is not None:
            try:
                price_val = self.price if isinstance(self.price, Decimal) else Decimal(str(self.price))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise ValidationError({'price': 'Некоректне значення ціни.'}) from exc
            if not price_val.is_finite() or price_val <= Decimal('0.00'):
                raise ValidationError({'price': 'Ціна повинна бути більшою за 0.'})
            self.price = price_val
        if self.weight_grams is not None:
            try:
                weight_val = (
                    self.weight_grams
                    if isinstance(self.weight_grams, int) and not isinstance(self.weight_grams, bool)
                    else int(self.weight_grams)
                )
            except (ValueError, TypeError) as exc:
                raise ValidationError({'weight_grams': 'Некоректне значення ваги.'}) from exc
            if weight_val <= 0:
                raise ValidationError({'weight_grams': 'Вага повинна бути більшою за 0.'})
            self.weight_grams = weight_val

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.slug:
            self.slug = _generate_unique_slug(
                Dish,
                self.title,
                fallback='dish-item',
                max_length=220,
                instance_pk=self.pk,
            )
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.title} ({self.price} грн)"

    def get_absolute_url(self) -> str:
        return reverse(
            'menu:category_dish_detail',
            kwargs={'category_slug': self.category.slug, 'dish_slug': self.slug},
        )


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
        if self.price_delta is not None:
            try:
                delta_val = (
                    self.price_delta
                    if isinstance(self.price_delta, Decimal)
                    else Decimal(str(self.price_delta))
                )
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise ValidationError({'price_delta': 'Некоректне значення зміни ціни.'}) from exc
            if not delta_val.is_finite():
                raise ValidationError({'price_delta': 'Некоректне значення зміни ціни.'})
            self.price_delta = delta_val
        if self.dish_id and self.price_delta is not None:
            try:
                dish_price = (
                    self.dish.price
                    if isinstance(self.dish.price, Decimal)
                    else Decimal(str(self.dish.price))
                )
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise ValidationError({'price_delta': 'Некоректне значення ціни страви.'}) from exc
            if dish_price + self.price_delta < Decimal('0.00'):
                raise ValidationError({'price_delta': 'Підсумкова ціна страви не може бути меншою за 0.'})

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.dish.title} - {self.name} (+{self.price_delta} грн)"
