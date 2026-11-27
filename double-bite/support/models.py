import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from pgvector.django import HnswIndex, VectorField


class ChatSessionStatus(models.TextChoices):
    ACTIVE = 'ACTIVE', 'Активна'
    ESCALATED = 'ESCALATED', 'Ескальована'
    RESOLVED = 'RESOLVED', 'Завершена'


class MessageRole(models.TextChoices):
    USER = 'user', 'Користувач'
    ASSISTANT = 'assistant', 'Асистент'
    OPERATOR = 'operator', 'Оператор'
    TOOL = 'tool', 'Інструмент'
    SYSTEM = 'system', 'Система'


class TicketReason(models.TextChoices):
    COMPLAINT = 'COMPLAINT', 'Скарга'
    ORDER_ISSUE = 'ORDER_ISSUE', 'Проблема із замовленням'
    HUMAN_REQUESTED = 'HUMAN_REQUESTED', 'Запит людини'
    BOT_CONFUSED = 'BOT_CONFUSED', 'Бот не зрозумів'
    OPERATOR_TAKEOVER = 'OPERATOR_TAKEOVER', 'Перехоплення менеджером'


class MessageSource(models.TextChoices):
    WEB = 'WEB', 'Веб-інтерфейс'
    AI = 'AI', 'AI-агент'
    TELEGRAM = 'TELEGRAM', 'Telegram'
    SYSTEM = 'SYSTEM', 'Система'


class TicketStatus(models.TextChoices):
    OPEN = 'OPEN', 'Відкрито'
    IN_PROGRESS = 'IN_PROGRESS', 'В обробці'
    RESOLVED = 'RESOLVED', 'Вирішено'


class StaffAuditDomain(models.TextChoices):
    MENU = 'MENU', 'Меню'
    PROMO = 'PROMO', 'Акції'
    FAQ = 'FAQ', 'FAQ'
    SUPPORT = 'SUPPORT', 'Підтримка'
    ORDER = 'ORDER', 'Замовлення'


class ChatSession(models.Model):
    session_uuid = models.UUIDField(
        'UUID сесії',
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='chat_sessions',
        db_index=True,
        verbose_name='Користувач',
    )
    guest_token = models.CharField(
        'Токен гостя',
        max_length=64,
        blank=True,
        default='',
        db_index=True,
    )
    status = models.CharField(
        'Статус',
        max_length=20,
        choices=ChatSessionStatus.choices,
        default=ChatSessionStatus.ACTIVE,
        db_index=True,
    )
    is_escalated = models.BooleanField(
        'Ескальовано на оператора',
        default=False,
        db_index=True,
    )
    escalation_reason = models.TextField(
        'Причина ескалації',
        blank=True,
    )
    assigned_operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_chat_sessions',
        db_index=True,
        verbose_name='Призначений оператор',
    )
    assigned_at = models.DateTimeField('Призначено о', null=True, blank=True)
    last_message_at = models.DateTimeField(
        'Останнє повідомлення',
        null=True,
        blank=True,
        db_index=True,
    )
    operator_last_read_id = models.PositiveBigIntegerField(
        'Останнє прочитане повідомлення оператором',
        default=0,
    )
    compressed_summary = models.TextField(
        'Стисле резюме діалогу',
        blank=True,
        default='',
    )
    summarized_up_to_msg_id = models.PositiveBigIntegerField(
        'Стиснуто до ID повідомлення',
        default=0,
    )
    upsell_offered_in_order_cycle = models.BooleanField(
        'Допродаж вже запропоновано',
        default=False,
    )
    complaint_details_requested = models.BooleanField(
        'Уточнення скарги надіслано',
        default=False,
    )
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = 'Сесія чату'
        verbose_name_plural = 'Сесії чату'
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=['user'],
                condition=Q(
                    user__isnull=False,
                    status__in=[
                        ChatSessionStatus.ACTIVE,
                        ChatSessionStatus.ESCALATED,
                    ],
                ),
                name='unique_active_chat_session_per_user',
            ),
            models.UniqueConstraint(
                fields=['guest_token'],
                condition=(
                    Q(
                        user__isnull=True,
                        status__in=[
                            ChatSessionStatus.ACTIVE,
                            ChatSessionStatus.ESCALATED,
                        ],
                    )
                    & ~Q(guest_token='')
                ),
                name='unique_active_guest_chat_session_per_token',
            ),
        ]

    def __str__(self) -> str:
        token_preview = self.guest_token[:8] if self.guest_token else 'anon'
        owner = self.user.email if self.user else f'Гість ({token_preview})'
        return f'Чат {self.session_uuid} — {owner} [{self.status}]'


class ChatMessage(models.Model):
    session = models.ForeignKey(
        ChatSession,
        on_delete=models.CASCADE,
        related_name='messages',
        verbose_name='Сесія чату',
    )
    role = models.CharField(
        'Роль',
        max_length=20,
        choices=MessageRole.choices,
    )
    sender_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_support_messages',
        verbose_name='Відправник (персонал)',
    )
    source = models.CharField(
        'Джерело',
        max_length=16,
        choices=MessageSource.choices,
        default=MessageSource.WEB,
    )
    telegram_message_id = models.BigIntegerField(
        'Telegram Message ID',
        null=True,
        blank=True,
        unique=True,
    )
    content = models.TextField(
        'Вміст повідомлення',
        blank=True,
    )
    tool_calls = models.JSONField(
        'Виклики інструментів',
        null=True,
        blank=True,
    )
    structured_payload = models.JSONField(
        'Структуровані дані',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Створено', auto_now_add=True)

    class Meta:
        verbose_name = 'Повідомлення чату'
        verbose_name_plural = 'Повідомлення чату'
        ordering = ('created_at', 'id')

    def __str__(self) -> str:
        return f"[{self.role}] {self.content[:40]}..."


Message = ChatMessage


class SupportTicket(models.Model):
    session = models.ForeignKey(
        ChatSession,
        on_delete=models.CASCADE,
        related_name='tickets',
        verbose_name='Сесія чату',
    )
    order = models.ForeignKey(
        'orders.Order',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='support_tickets',
        verbose_name='Замовлення',
    )
    customer_phone = models.CharField(
        'Телефон клієнта',
        max_length=20,
        blank=True,
    )
    reason = models.CharField(
        'Причина звернення',
        max_length=50,
        choices=TicketReason.choices,
        default=TicketReason.HUMAN_REQUESTED,
    )
    details = models.TextField(
        'Деталі звернення',
        blank=True,
    )
    status = models.CharField(
        'Статус тікета',
        max_length=20,
        choices=TicketStatus.choices,
        default=TicketStatus.OPEN,
        db_index=True,
    )
    assigned_operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_support_tickets',
        verbose_name='Призначений оператор',
    )
    telegram_thread_id = models.BigIntegerField(
        'Telegram Topic ID',
        null=True,
        blank=True,
        db_index=True,
    )
    telegram_control_message_id = models.BigIntegerField(
        'Telegram Control Message ID',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    resolved_at = models.DateTimeField('Вирішено о', null=True, blank=True)

    class Meta:
        verbose_name = 'Тікет підтримки'
        verbose_name_plural = 'Тікети підтримки'
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=['session'],
                condition=Q(
                    status__in=[
                        TicketStatus.OPEN,
                        TicketStatus.IN_PROGRESS,
                    ],
                ),
                name='unique_open_ticket_per_session',
            ),
        ]

    def __str__(self) -> str:
        return f"Тікет #{self.id or 0} [{self.get_reason_display()}] — {self.status}"


class FAQKnowledge(models.Model):
    question = models.CharField('Запитання', max_length=255)
    answer = models.TextField('Відповідь')
    category = models.CharField('Категорія', max_length=100, blank=True, default='')
    is_active = models.BooleanField('Активне', default=True, db_index=True)
    embedding = VectorField(
        'Векторний ембедінг',
        dimensions=1024,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = 'База знань FAQ'
        verbose_name_plural = 'База знань FAQ'
        ordering = ('category', 'question')
        indexes = [
            HnswIndex(
                name='faq_embedding_hnsw_idx',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
        ]

    def __str__(self) -> str:
        return self.question


class ChatAttachment(models.Model):
    session = models.ForeignKey(
        ChatSession,
        on_delete=models.CASCADE,
        related_name='attachments',
        verbose_name='Сесія чату',
    )
    message = models.ForeignKey(
        ChatMessage,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='attachments',
        verbose_name='Повідомлення чату',
    )
    uploader_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='uploaded_chat_attachments',
        verbose_name='Автор завантаження',
    )
    guest_token = models.CharField(
        'Токен гостя',
        max_length=64,
        blank=True,
        default='',
        db_index=True,
    )
    file = models.ImageField(
        'Файл зображення',
        upload_to='support_attachments/%Y/%m/',
        max_length=500,
    )
    original_mime = models.CharField(
        'Початковий MIME-тип',
        max_length=64,
        default='image/webp',
    )
    width = models.PositiveIntegerField('Ширина (px)')
    height = models.PositiveIntegerField('Висота (px)')
    size_bytes = models.PositiveIntegerField('Розмір (байт)')
    sha256_hash = models.CharField('SHA-256 хеш', max_length=64, db_index=True)
    vision_analysis = models.JSONField(
        'Результат розпізнавання',
        null=True,
        blank=True,
    )
    telegram_file_unique_id = models.CharField(
        'Telegram File Unique ID',
        max_length=128,
        blank=True,
        default='',
        db_index=True,
    )
    synced_to_telegram_at = models.DateTimeField(
        'Синхронізовано в Telegram о',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Створено', auto_now_add=True)

    class Meta:
        verbose_name = 'Вкладення чату'
        verbose_name_plural = 'Вкладення чату'
        ordering = ('created_at', 'id')

    def __str__(self) -> str:
        return f'Вкладення #{self.id or 0} ({self.width}x{self.height})'


class CustomerMemory(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='ai_memory',
        verbose_name='Користувач',
    )
    guest_token = models.CharField(
        'Токен гостя',
        max_length=64,
        blank=True,
        default='',
        db_index=True,
    )
    dietary_restrictions = models.JSONField(
        'Дієтичні обмеження',
        default=list,
        blank=True,
    )
    disliked_ingredients = models.JSONField(
        'Небажані інгредієнти',
        default=list,
        blank=True,
    )
    favorite_dish_ids = models.JSONField(
        'Улюблені страви (ID)',
        default=list,
        blank=True,
    )
    preferred_name = models.CharField(
        "Ім'я для замовлень",
        max_length=100,
        blank=True,
        default='',
    )
    preferred_phone = models.CharField(
        'Телефон для замовлень',
        max_length=20,
        blank=True,
        default='',
    )
    preferred_email = models.EmailField(
        'Email для чеків',
        blank=True,
        default='',
    )
    preferred_address = models.TextField(
        'Адреса доставки',
        blank=True,
        default='',
    )
    rolling_profile_summary = models.TextField(
        'Стислий профіль клієнта',
        blank=True,
        default='',
    )
    last_order_snapshot = models.JSONField(
        'Знімок останнього замовлення',
        null=True,
        blank=True,
    )
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = "Пам'ять клієнта"
        verbose_name_plural = "Пам'ять клієнтів"
        constraints = [
            models.UniqueConstraint(
                fields=['guest_token'],
                condition=Q(user__isnull=True) & ~Q(guest_token=''),
                name='unique_customer_memory_per_guest_token',
            ),
        ]

    def __str__(self) -> str:
        owner = self.user.email if self.user_id and self.user else f'Гість ({self.guest_token[:8]})'
        return f"Пам'ять клієнта — {owner}"


class AITurnAuditLog(models.Model):
    session = models.ForeignKey(
        ChatSession,
        on_delete=models.CASCADE,
        related_name='ai_audit_logs',
        verbose_name='Сесія чату',
    )
    user_message = models.ForeignKey(
        ChatMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Запит клієнта',
    )
    assistant_message = models.ForeignKey(
        ChatMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Відповідь асистента',
    )
    model_used = models.CharField('Модель AI', max_length=100, db_index=True)
    fallback_used = models.BooleanField(
        'Використано резервну модель',
        default=False,
        db_index=True,
    )
    reasoning_effort = models.CharField(
        'Рівень міркувань',
        max_length=32,
        blank=True,
        default='',
    )
    rounds_used = models.PositiveSmallIntegerField('Кількість раундів', default=1)
    prompt_tokens = models.PositiveIntegerField('Вхідні токени', default=0)
    cached_tokens = models.PositiveIntegerField('Кешовані токени', default=0)
    completion_tokens = models.PositiveIntegerField('Вихідні токени', default=0)
    latency_ms = models.PositiveIntegerField('Затримка (мс)', default=0)
    tools_invoked = models.JSONField(
        'Викликані інструменти',
        default=list,
        blank=True,
    )
    verifier_passed = models.BooleanField(
        'Пройшов перевірку',
        default=True,
        db_index=True,
    )
    verifier_corrections = models.JSONField(
        'Коригування верифікатора',
        default=list,
        blank=True,
    )
    safety_flags = models.JSONField(
        'Прапорці безпеки',
        default=list,
        blank=True,
    )
    created_at = models.DateTimeField('Створено', auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'Аудит ходу AI'
        verbose_name_plural = 'Аудит ходів AI'
        ordering = ('-created_at', '-id')

    def __str__(self) -> str:
        return f'AI Audit #{self.id or 0} [{self.model_used}]'


class StaffAuditLog(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='staff_audit_logs',
        verbose_name='Співробітник',
    )
    actor_role = models.CharField(
        'Роль співробітника',
        max_length=32,
        blank=True,
        default='',
    )
    domain = models.CharField(
        'Домен',
        max_length=24,
        choices=StaffAuditDomain.choices,
        db_index=True,
    )
    action = models.CharField('Дія', max_length=48, db_index=True)
    target_model = models.CharField('Модель цілі', max_length=64)
    target_id = models.CharField('ID цілі', max_length=64, db_index=True)
    before_state = models.JSONField('Стан до зміни', null=True, blank=True)
    after_state = models.JSONField('Стан після зміни', null=True, blank=True)
    ip_address = models.CharField('IP-адреса', max_length=64, blank=True, default='')
    created_at = models.DateTimeField('Створено', auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'Аудит дій персоналу'
        verbose_name_plural = 'Аудит дій персоналу'
        ordering = ('-created_at', '-id')

    def __str__(self) -> str:
        return f'Staff Audit #{self.id or 0} [{self.domain}:{self.action}]'
