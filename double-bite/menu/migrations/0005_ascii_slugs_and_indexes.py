import re
from decimal import Decimal

import django.core.validators
from django.db import migrations, models
from django.utils.text import slugify

from menu.models import transliterate_to_ascii

_ASCII_SLUG_RE = re.compile(r'^[-a-zA-Z0-9_]+\Z')


def _migrate_model_slugs(
    model_cls,
    db_alias: str,
    *,
    source_field: str,
    fallback: str,
    max_length: int,
) -> None:
    qs = model_cls.objects.using(db_alias).order_by('pk')
    for obj in qs:
        current_slug = obj.slug or ''
        if current_slug and _ASCII_SLUG_RE.fullmatch(current_slug):
            continue
        source_text = current_slug or getattr(obj, source_field, '') or ''
        base = slugify(transliterate_to_ascii(source_text), allow_unicode=False) or fallback
        base = base[:max_length].rstrip('-') or fallback[:max_length]
        candidate = base
        counter = 2
        others = model_cls.objects.using(db_alias).exclude(pk=obj.pk)
        while others.filter(slug=candidate).exists():
            suffix = f'-{counter}'
            trimmed = (
                base[: max_length - len(suffix)].rstrip('-')
                or fallback[: max_length - len(suffix)]
            )
            candidate = f'{trimmed}{suffix}'
            counter += 1
        obj.slug = candidate
        obj.save(update_fields=['slug'])


def transliterate_existing_slugs(apps, schema_editor) -> None:
    db_alias = schema_editor.connection.alias
    Category = apps.get_model('menu', 'Category')
    Dish = apps.get_model('menu', 'Dish')
    _migrate_model_slugs(
        Category,
        db_alias,
        source_field='name',
        fallback='category-item',
        max_length=120,
    )
    _migrate_model_slugs(
        Dish,
        db_alias,
        source_field='title',
        fallback='dish-item',
        max_length=220,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('menu', '0004_add_pgvector_and_dish_embedding'),
    ]

    operations = [
        migrations.RunPython(
            transliterate_existing_slugs,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='category',
            name='slug',
            field=models.SlugField(max_length=120, unique=True, verbose_name='Slug'),
        ),
        migrations.AlterField(
            model_name='dish',
            name='slug',
            field=models.SlugField(max_length=220, unique=True, verbose_name='Slug'),
        ),
        migrations.AlterField(
            model_name='dish',
            name='calories',
            field=models.PositiveIntegerField(
                db_index=True,
                default=0,
                verbose_name='Калорійність (ккал)',
            ),
        ),
        migrations.AlterField(
            model_name='dish',
            name='is_spicy',
            field=models.BooleanField(
                db_index=True,
                default=False,
                verbose_name='Гостра',
            ),
        ),
        migrations.AlterField(
            model_name='dish',
            name='is_vegetarian',
            field=models.BooleanField(
                db_index=True,
                default=False,
                verbose_name='Вегетаріанська',
            ),
        ),
        migrations.AlterField(
            model_name='dish',
            name='price',
            field=models.DecimalField(
                db_index=True,
                decimal_places=2,
                max_digits=8,
                validators=[django.core.validators.MinValueValidator(Decimal('0.01'))],
                verbose_name='Ціна (грн)',
            ),
        ),
        migrations.AddIndex(
            model_name='category',
            index=models.Index(
                fields=['is_active', 'display_order'],
                name='cat_active_order_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='dish',
            index=models.Index(
                fields=['is_available', 'category'],
                name='dish_avail_cat_idx',
            ),
        ),
    ]
