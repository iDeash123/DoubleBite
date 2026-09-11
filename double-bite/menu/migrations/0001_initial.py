import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Category',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='Назва')),
                ('slug', models.SlugField(allow_unicode=True, max_length=120, unique=True, verbose_name='Slug')),
                ('icon', models.CharField(blank=True, default='', max_length=50, verbose_name='Іконка')),
                ('display_order', models.PositiveIntegerField(default=0, verbose_name='Порядок відображення')),
                ('is_active', models.BooleanField(default=True, verbose_name='Активна')),
            ],
            options={
                'verbose_name': 'Категорія',
                'verbose_name_plural': 'Категорії',
                'ordering': ['display_order', 'name'],
            },
        ),
        migrations.CreateModel(
            name='Dish',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200, verbose_name='Назва страви')),
                ('slug', models.SlugField(allow_unicode=True, max_length=220, unique=True, verbose_name='Slug')),
                ('description', models.TextField(blank=True, verbose_name='Опис')),
                ('price', models.DecimalField(decimal_places=2, max_digits=8, validators=[django.core.validators.MinValueValidator(Decimal('0.01'))], verbose_name='Ціна (грн)')),
                ('image', models.URLField(blank=True, max_length=500, verbose_name='URL зображення')),
                ('weight_grams', models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)], verbose_name='Вага (г)')),
                ('calories', models.PositiveIntegerField(default=0, verbose_name='Калорійність (ккал)')),
                ('allergens', models.CharField(blank=True, max_length=255, verbose_name='Алергени')),
                ('is_vegetarian', models.BooleanField(default=False, verbose_name='Вегетаріанська')),
                ('is_spicy', models.BooleanField(default=False, verbose_name='Гостра')),
                ('is_available', models.BooleanField(db_index=True, default=True, verbose_name='В наявності')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Створено')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Оновлено')),
                ('category', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='dishes', to='menu.category', verbose_name='Категорія')),
            ],
            options={
                'verbose_name': 'Страва',
                'verbose_name_plural': 'Страви',
                'ordering': ['title'],
            },
        ),
        migrations.CreateModel(
            name='DishOption',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='Назва опції / модифікатора')),
                ('price_delta', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=8, verbose_name='Зміна ціни (грн)')),
                ('dish', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='options', to='menu.dish', verbose_name='Страва')),
            ],
            options={
                'verbose_name': 'Опція страви',
                'verbose_name_plural': 'Опції страв',
                'ordering': ['dish', 'price_delta', 'name'],
            },
        ),
    ]
