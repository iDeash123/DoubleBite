import django.core.validators
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('menu', '0005_ascii_slugs_and_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='dish',
            name='allergen_codes',
            field=models.JSONField(blank=True, default=list, verbose_name='Коди алергенів'),
        ),
        migrations.AddField(
            model_name='dish',
            name='carbs_grams',
            field=models.PositiveSmallIntegerField(default=0, verbose_name='Вуглеводи (г)'),
        ),
        migrations.AddField(
            model_name='dish',
            name='contains_raw_fish',
            field=models.BooleanField(db_index=True, default=False, verbose_name='Містить сиру рибу'),
        ),
        migrations.AddField(
            model_name='dish',
            name='fats_grams',
            field=models.PositiveSmallIntegerField(default=0, verbose_name='Жири (г)'),
        ),
        migrations.AddField(
            model_name='dish',
            name='ingredients',
            field=models.TextField(blank=True, default='', verbose_name='Склад'),
        ),
        migrations.AddField(
            model_name='dish',
            name='is_vegan',
            field=models.BooleanField(db_index=True, default=False, verbose_name='Веганська'),
        ),
        migrations.AddField(
            model_name='dish',
            name='proteins_grams',
            field=models.PositiveSmallIntegerField(default=0, verbose_name='Білки (г)'),
        ),
        migrations.AddField(
            model_name='dish',
            name='safe_for_pregnancy',
            field=models.BooleanField(db_index=True, default=True, verbose_name='Підходить вагітним'),
        ),
        migrations.AddField(
            model_name='dish',
            name='upsell_group',
            field=models.CharField(choices=[('main', 'Основна страва'), ('drink', 'Напій'), ('salad_starter', 'Салат або закуска'), ('dessert', 'Десерт'), ('sauce', 'Соус')], db_index=True, default='main', max_length=20, verbose_name='Група допродажу'),
        ),
        migrations.CreateModel(
            name='Promotion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=160, verbose_name='Назва акції')),
                ('slug', models.SlugField(max_length=180, unique=True, verbose_name='Slug')),
                ('description', models.TextField(blank=True, verbose_name='Умови акції')),
                ('promo_type', models.CharField(choices=[('PERCENT', 'Відсоток'), ('FIXED_AMOUNT', 'Фіксована сума'), ('BUNDLE_GIFT', 'Подарунок або комбо')], max_length=20, verbose_name='Тип акції')),
                ('discount_value', models.DecimalField(decimal_places=2, max_digits=8, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))], verbose_name='Розмір знижки')),
                ('min_order_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=8, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))], verbose_name='Мінімальна сума замовлення (грн)')),
                ('is_active', models.BooleanField(db_index=True, default=True, verbose_name='Активна')),
                ('starts_at', models.DateTimeField(blank=True, db_index=True, null=True, verbose_name='Початок дії')),
                ('ends_at', models.DateTimeField(blank=True, db_index=True, null=True, verbose_name='Завершення дії')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Створено')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Оновлено')),
                ('applicable_categories', models.ManyToManyField(blank=True, related_name='promotions', to='menu.category', verbose_name='Категорії дії акції')),
                ('applicable_dishes', models.ManyToManyField(blank=True, related_name='promotions', to='menu.dish', verbose_name='Страви дії акції')),
            ],
            options={
                'verbose_name': 'Акція',
                'verbose_name_plural': 'Акції',
                'ordering': ('-created_at', 'id'),
                'constraints': [models.CheckConstraint(condition=models.Q(('discount_value__gte', Decimal('0.00'))), name='promotion_discount_value_gte_zero'), models.CheckConstraint(condition=models.Q(('min_order_amount__gte', Decimal('0.00'))), name='promotion_min_order_amount_gte_zero')],
            },
        ),
    ]
