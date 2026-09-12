import uuid

from django.conf import settings
from django.db import models


class ChatSessionStatus(models.TextChoices):
    ACTIVE = 'ACTIVE', 'Активна'
    ESCALATED = 'ESCALATED', 'Ескальована'
    RESOLVED = 'RESOLVED', 'Завершена'


class MessageRole(models.TextChoices):
    USER = 'user', 'Користувач'
    ASSISTANT = 'assistant', 'Асистент'
    TOOL = 'tool', 'Інструмент'
    SYSTEM = 'system', 'Система'


class TicketReason(models.TextChoices):
    COMPLAINT = 'COMPLAINT', 'Скарга'
    ORDER_ISSUE = 'ORDER_ISSUE', 'Проблема із замовленням'
    HUMAN_REQUESTED = 'HUMAN_REQUESTED', 'Запит людини'
    BOT_CONFUSED = 'BOT_CONFUSED', 'Бот не зрозумів'


class TicketStatus(models.TextChoices):
    OPEN = 'OPEN', 'Відкрито'
    IN_PROGRESS = 'IN_PROGRESS', 'В обробці'
    RESOLVED = 'RESOLVED', 'Вирішено'


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
    session_key = models.CharField(
        'Ключ сесії гостя',
        max_length=40,
        blank=True,
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
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = 'Сесія чату'
        verbose_name_plural = 'Сесії чату'
        ordering = ('-created_at',)

    def __str__(self) -> str:
        owner = self.user.email if self.user else f"Гість ({self.session_key[:8] if self.session_key else 'anon'})"
        return f"Чат {self.session_uuid} — {owner} [{self.status}]"


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
    content = models.TextField(
        'Вміст повідомлення',
        blank=True,
    )
    tool_calls = models.JSONField(
        'Виклики інструментів',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Створено', auto_now_add=True)

    class Meta:
        verbose_name = 'Повідомлення чату'
        verbose_name_plural = 'Повідомлення чату'
        ordering = ('created_at',)

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
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    resolved_at = models.DateTimeField('Вирішено о', null=True, blank=True)

    class Meta:
        verbose_name = 'Тікет підтримки'
        verbose_name_plural = 'Тікети підтримки'
        ordering = ('-created_at',)

    def __str__(self) -> str:
        return f"Тікет #{self.id or 0} [{self.get_reason_display()}] — {self.status}"


class FAQKnowledge(models.Model):
    question = models.CharField('Запитання', max_length=255)
    answer = models.TextField('Відповідь')
    category = models.CharField('Категорія', max_length=100, blank=True, default='')
    is_active = models.BooleanField('Активне', default=True, db_index=True)
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = 'База знань FAQ'
        verbose_name_plural = 'База знань FAQ'
        ordering = ('category', 'question')

    def __str__(self) -> str:
        return self.question
