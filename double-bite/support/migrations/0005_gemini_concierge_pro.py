import django.db.models.deletion
import pgvector.django.indexes
import pgvector.django.vector
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('menu', '0004_add_pgvector_and_dish_embedding'),
        ('support', '0004_chatmessage_telegram_message_id_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AITurnAuditLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('model_used', models.CharField(db_index=True, max_length=100, verbose_name='Модель AI')),
                ('fallback_used', models.BooleanField(db_index=True, default=False, verbose_name='Використано резервну модель')),
                ('reasoning_effort', models.CharField(blank=True, default='', max_length=32, verbose_name='Рівень міркувань')),
                ('rounds_used', models.PositiveSmallIntegerField(default=1, verbose_name='Кількість раундів')),
                ('prompt_tokens', models.PositiveIntegerField(default=0, verbose_name='Вхідні токени')),
                ('cached_tokens', models.PositiveIntegerField(default=0, verbose_name='Кешовані токени')),
                ('completion_tokens', models.PositiveIntegerField(default=0, verbose_name='Вихідні токени')),
                ('latency_ms', models.PositiveIntegerField(default=0, verbose_name='Затримка (мс)')),
                ('tools_invoked', models.JSONField(blank=True, default=list, verbose_name='Викликані інструменти')),
                ('verifier_passed', models.BooleanField(db_index=True, default=True, verbose_name='Пройшов перевірку')),
                ('verifier_corrections', models.JSONField(blank=True, default=list, verbose_name='Коригування верифікатора')),
                ('safety_flags', models.JSONField(blank=True, default=list, verbose_name='Прапорці безпеки')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Створено')),
            ],
            options={
                'verbose_name': 'Аудит ходу AI',
                'verbose_name_plural': 'Аудит ходів AI',
                'ordering': ('-created_at', '-id'),
            },
        ),
        migrations.CreateModel(
            name='ChatAttachment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('guest_token', models.CharField(blank=True, db_index=True, default='', max_length=64, verbose_name='Токен гостя')),
                ('file', models.ImageField(max_length=500, upload_to='support_attachments/%Y/%m/', verbose_name='Файл зображення')),
                ('original_mime', models.CharField(default='image/webp', max_length=64, verbose_name='Початковий MIME-тип')),
                ('width', models.PositiveIntegerField(verbose_name='Ширина (px)')),
                ('height', models.PositiveIntegerField(verbose_name='Висота (px)')),
                ('size_bytes', models.PositiveIntegerField(verbose_name='Розмір (байт)')),
                ('sha256_hash', models.CharField(db_index=True, max_length=64, verbose_name='SHA-256 хеш')),
                ('vision_analysis', models.JSONField(blank=True, null=True, verbose_name='Результат розпізнавання')),
                ('telegram_file_unique_id', models.CharField(blank=True, db_index=True, default='', max_length=128, verbose_name='Telegram File Unique ID')),
                ('synced_to_telegram_at', models.DateTimeField(blank=True, null=True, verbose_name='Синхронізовано в Telegram о')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Створено')),
            ],
            options={
                'verbose_name': 'Вкладення чату',
                'verbose_name_plural': 'Вкладення чату',
                'ordering': ('created_at', 'id'),
            },
        ),
        migrations.CreateModel(
            name='CustomerMemory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('guest_token', models.CharField(blank=True, db_index=True, default='', max_length=64, verbose_name='Токен гостя')),
                ('dietary_restrictions', models.JSONField(blank=True, default=list, verbose_name='Дієтичні обмеження')),
                ('disliked_ingredients', models.JSONField(blank=True, default=list, verbose_name='Небажані інгредієнти')),
                ('favorite_dish_ids', models.JSONField(blank=True, default=list, verbose_name='Улюблені страви (ID)')),
                ('preferred_name', models.CharField(blank=True, default='', max_length=100, verbose_name="Ім'я для замовлень")),
                ('preferred_phone', models.CharField(blank=True, default='', max_length=20, verbose_name='Телефон для замовлень')),
                ('preferred_email', models.EmailField(blank=True, default='', max_length=254, verbose_name='Email для чеків')),
                ('preferred_address', models.TextField(blank=True, default='', verbose_name='Адреса доставки')),
                ('rolling_profile_summary', models.TextField(blank=True, default='', verbose_name='Стислий профіль клієнта')),
                ('last_order_snapshot', models.JSONField(blank=True, null=True, verbose_name='Знімок останнього замовлення')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Оновлено')),
            ],
            options={
                'verbose_name': "Пам'ять клієнта",
                'verbose_name_plural': "Пам'ять клієнтів",
            },
        ),
        migrations.CreateModel(
            name='StaffAuditLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('actor_role', models.CharField(blank=True, default='', max_length=32, verbose_name='Роль співробітника')),
                ('domain', models.CharField(choices=[('MENU', 'Меню'), ('PROMO', 'Акції'), ('FAQ', 'FAQ'), ('SUPPORT', 'Підтримка'), ('ORDER', 'Замовлення')], db_index=True, max_length=24, verbose_name='Домен')),
                ('action', models.CharField(db_index=True, max_length=48, verbose_name='Дія')),
                ('target_model', models.CharField(max_length=64, verbose_name='Модель цілі')),
                ('target_id', models.CharField(db_index=True, max_length=64, verbose_name='ID цілі')),
                ('before_state', models.JSONField(blank=True, null=True, verbose_name='Стан до зміни')),
                ('after_state', models.JSONField(blank=True, null=True, verbose_name='Стан після зміни')),
                ('ip_address', models.CharField(blank=True, default='', max_length=64, verbose_name='IP-адреса')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Створено')),
            ],
            options={
                'verbose_name': 'Аудит дій персоналу',
                'verbose_name_plural': 'Аудит дій персоналу',
                'ordering': ('-created_at', '-id'),
            },
        ),
        migrations.AddField(
            model_name='chatmessage',
            name='structured_payload',
            field=models.JSONField(blank=True, null=True, verbose_name='Структуровані дані'),
        ),
        migrations.AddField(
            model_name='chatsession',
            name='complaint_details_requested',
            field=models.BooleanField(default=False, verbose_name='Уточнення скарги надіслано'),
        ),
        migrations.AddField(
            model_name='chatsession',
            name='compressed_summary',
            field=models.TextField(blank=True, default='', verbose_name='Стисле резюме діалогу'),
        ),
        migrations.AddField(
            model_name='chatsession',
            name='summarized_up_to_msg_id',
            field=models.PositiveBigIntegerField(default=0, verbose_name='Стиснуто до ID повідомлення'),
        ),
        migrations.AddField(
            model_name='chatsession',
            name='upsell_offered_in_order_cycle',
            field=models.BooleanField(default=False, verbose_name='Допродаж вже запропоновано'),
        ),
        migrations.AddField(
            model_name='faqknowledge',
            name='embedding',
            field=pgvector.django.vector.VectorField(blank=True, dimensions=1024, null=True, verbose_name='Векторний ембедінг'),
        ),
        migrations.AddIndex(
            model_name='faqknowledge',
            index=pgvector.django.indexes.HnswIndex(ef_construction=64, fields=['embedding'], m=16, name='faq_embedding_hnsw_idx', opclasses=['vector_cosine_ops']),
        ),
        migrations.AddField(
            model_name='aiturnauditlog',
            name='assistant_message',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='support.chatmessage', verbose_name='Відповідь асистента'),
        ),
        migrations.AddField(
            model_name='aiturnauditlog',
            name='session',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ai_audit_logs', to='support.chatsession', verbose_name='Сесія чату'),
        ),
        migrations.AddField(
            model_name='aiturnauditlog',
            name='user_message',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='support.chatmessage', verbose_name='Запит клієнта'),
        ),
        migrations.AddField(
            model_name='chatattachment',
            name='message',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='attachments', to='support.chatmessage', verbose_name='Повідомлення чату'),
        ),
        migrations.AddField(
            model_name='chatattachment',
            name='session',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attachments', to='support.chatsession', verbose_name='Сесія чату'),
        ),
        migrations.AddField(
            model_name='chatattachment',
            name='uploader_user',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='uploaded_chat_attachments', to=settings.AUTH_USER_MODEL, verbose_name='Автор завантаження'),
        ),
        migrations.AddField(
            model_name='customermemory',
            name='user',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='ai_memory', to=settings.AUTH_USER_MODEL, verbose_name='Користувач'),
        ),
        migrations.AddField(
            model_name='staffauditlog',
            name='actor',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='staff_audit_logs', to=settings.AUTH_USER_MODEL, verbose_name='Співробітник'),
        ),
        migrations.AddConstraint(
            model_name='customermemory',
            constraint=models.UniqueConstraint(condition=models.Q(('user__isnull', True), models.Q(('guest_token', ''), _negated=True)), fields=('guest_token',), name='unique_customer_memory_per_guest_token'),
        ),
    ]
