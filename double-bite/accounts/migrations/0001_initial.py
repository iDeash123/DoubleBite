import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.CreateModel(
            name='User',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('password', models.CharField(max_length=128, verbose_name='password')),
                ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
                ('is_superuser', models.BooleanField(default=False, help_text='Designates that this user has all permissions without explicitly assigning them.', verbose_name='superuser status')),
                ('first_name', models.CharField(blank=True, max_length=150, verbose_name='first name')),
                ('last_name', models.CharField(blank=True, max_length=150, verbose_name='last name')),
                ('is_staff', models.BooleanField(default=False, help_text='Designates whether the user can log into this admin site.', verbose_name='staff status')),
                ('is_active', models.BooleanField(default=True, help_text='Designates whether this user should be treated as active. Unselect this instead of deleting accounts.', verbose_name='active')),
                ('date_joined', models.DateTimeField(default=django.utils.timezone.now, verbose_name='date joined')),
                ('email', models.EmailField(max_length=254, unique=True, verbose_name='Електронна пошта')),
                ('phone', models.CharField(blank=True, max_length=20, verbose_name='Номер телефону')),
                ('role', models.CharField(choices=[('CUSTOMER', 'Клієнт'), ('RESTAURANT_ADMIN', 'Менеджер'), ('COURIER', 'Кур\'єр')], default='CUSTOMER', max_length=20, verbose_name='Роль')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Дата створення')),
                ('groups', models.ManyToManyField(blank=True, help_text='The groups this user belongs to. A user will get all permissions granted to each of their groups.', related_name='user_set', related_query_name='user', to='auth.group', verbose_name='groups')),
                ('user_permissions', models.ManyToManyField(blank=True, help_text='Specific permissions for this user.', related_name='user_set', related_query_name='user', to='auth.permission', verbose_name='user permissions')),
            ],
            options={
                'verbose_name': 'Користувач',
                'verbose_name_plural': 'Користувачі',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='DeliveryAddress',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(default='Дім', max_length=50, verbose_name='Назва адреси')),
                ('city', models.CharField(default='Київ', max_length=100, verbose_name='Місто')),
                ('street', models.CharField(max_length=255, verbose_name='Вулиця')),
                ('building', models.CharField(max_length=20, verbose_name='Будинок')),
                ('apartment', models.CharField(blank=True, max_length=20, verbose_name='Квартира / Офіс')),
                ('floor', models.CharField(blank=True, max_length=10, verbose_name='Поверх')),
                ('intercom', models.CharField(blank=True, max_length=20, verbose_name='Домофон')),
                ('is_default', models.BooleanField(default=False, verbose_name='Основна адреса')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Дата створення')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='addresses', to=settings.AUTH_USER_MODEL, verbose_name='Користувач')),
            ],
            options={
                'verbose_name': 'Адреса доставки',
                'verbose_name_plural': 'Адреси доставки',
                'ordering': ['-is_default', '-created_at'],
            },
        ),
    ]
