import base64
import hashlib
import hmac

from django.conf import settings
from django.db import migrations, models, transaction
from django.db.models import Count
from django.utils import timezone


def _session_key_to_guest_token(session_key: str) -> str:
    if not session_key:
        return ''
    secret = (
        settings.SECRET_KEY
        if isinstance(settings.SECRET_KEY, bytes)
        else str(settings.SECRET_KEY).encode('utf-8')
    )
    digest = hmac.new(
        secret,
        session_key.encode('utf-8'),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def migrate_forward(apps, schema_editor) -> None:
    db_alias = schema_editor.connection.alias
    ChatSession = apps.get_model('support', 'ChatSession')
    SupportTicket = apps.get_model('support', 'SupportTicket')
    active_statuses = ['ACTIVE', 'ESCALATED']
    open_ticket_statuses = ['OPEN', 'IN_PROGRESS']

    with transaction.atomic(using=db_alias):
        ChatSession.objects.using(db_alias).filter(
            user_id__isnull=False
        ).exclude(guest_token='').update(guest_token='')

        for chat_session in (
            ChatSession.objects.using(db_alias)
            .filter(user_id__isnull=True, session_key__isnull=False)
            .exclude(session_key='')
            .only('session_uuid', 'session_key')
            .iterator()
        ):
            ChatSession.objects.using(db_alias).filter(pk=chat_session.pk).update(
                guest_token=_session_key_to_guest_token(chat_session.session_key)
            )

        dup_users = list(
            ChatSession.objects.using(db_alias)
            .filter(user_id__isnull=False, status__in=active_statuses)
            .order_by()
            .values('user_id')
            .annotate(cnt=Count('session_uuid'))
            .filter(cnt__gt=1)
            .values_list('user_id', flat=True)
        )
        for uid in dup_users:
            sessions = list(
                ChatSession.objects.using(db_alias)
                .filter(user_id=uid, status__in=active_statuses)
                .order_by('-updated_at', '-created_at', '-session_uuid')
            )
            older_pks = [s.pk for s in sessions[1:]]
            if older_pks:
                ChatSession.objects.using(db_alias).filter(pk__in=older_pks).update(
                    status='RESOLVED',
                    is_escalated=False,
                )

        dup_tokens = list(
            ChatSession.objects.using(db_alias)
            .filter(user_id__isnull=True, status__in=active_statuses)
            .exclude(guest_token='')
            .order_by()
            .values('guest_token')
            .annotate(cnt=Count('session_uuid'))
            .filter(cnt__gt=1)
            .values_list('guest_token', flat=True)
        )
        for token in dup_tokens:
            sessions = list(
                ChatSession.objects.using(db_alias)
                .filter(
                    user_id__isnull=True,
                    guest_token=token,
                    status__in=active_statuses,
                )
                .order_by('-updated_at', '-created_at', '-session_uuid')
            )
            older_pks = [s.pk for s in sessions[1:]]
            if older_pks:
                ChatSession.objects.using(db_alias).filter(pk__in=older_pks).update(
                    status='RESOLVED',
                    is_escalated=False,
                )

        dup_ticket_sessions = list(
            SupportTicket.objects.using(db_alias)
            .filter(status__in=open_ticket_statuses)
            .order_by()
            .values('session_id')
            .annotate(cnt=Count('id'))
            .filter(cnt__gt=1)
            .values_list('session_id', flat=True)
        )
        now = timezone.now()
        for session_id in dup_ticket_sessions:
            tickets = list(
                SupportTicket.objects.using(db_alias)
                .filter(session_id=session_id, status__in=open_ticket_statuses)
                .order_by('-created_at', '-id')
            )
            older_ids = [t.id for t in tickets[1:]]
            if older_ids:
                SupportTicket.objects.using(db_alias).filter(id__in=older_ids).update(
                    status='RESOLVED',
                    resolved_at=now,
                )


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('support', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='chatsession',
            name='guest_token',
            field=models.CharField(
                blank=True,
                db_index=True,
                default='',
                max_length=64,
                verbose_name='Токен гостя',
            ),
        ),
        migrations.RunPython(migrate_forward, reverse_code=migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='chatsession',
            name='session_key',
        ),
        migrations.AlterField(
            model_name='chatmessage',
            name='role',
            field=models.CharField(
                choices=[
                    ('user', 'Користувач'),
                    ('assistant', 'Асистент'),
                    ('operator', 'Оператор'),
                    ('tool', 'Інструмент'),
                    ('system', 'Система'),
                ],
                max_length=20,
                verbose_name='Роль',
            ),
        ),
        migrations.AlterModelOptions(
            name='chatmessage',
            options={
                'ordering': ('created_at', 'id'),
                'verbose_name': 'Повідомлення чату',
                'verbose_name_plural': 'Повідомлення чату',
            },
        ),
        migrations.AddConstraint(
            model_name='chatsession',
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ('status__in', ['ACTIVE', 'ESCALATED']),
                    ('user__isnull', False),
                ),
                fields=('user',),
                name='unique_active_chat_session_per_user',
            ),
        ),
        migrations.AddConstraint(
            model_name='chatsession',
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ('status__in', ['ACTIVE', 'ESCALATED']),
                    ('user__isnull', True),
                    models.Q(('guest_token', ''), _negated=True),
                ),
                fields=('guest_token',),
                name='unique_active_guest_chat_session_per_token',
            ),
        ),
        migrations.AddConstraint(
            model_name='supportticket',
            constraint=models.UniqueConstraint(
                condition=models.Q(('status__in', ['OPEN', 'IN_PROGRESS'])),
                fields=('session',),
                name='unique_open_ticket_per_session',
            ),
        ),
    ]
