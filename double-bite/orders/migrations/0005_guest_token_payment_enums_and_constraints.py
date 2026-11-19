import base64
import hashlib
import hmac
import json

from django.conf import settings
from django.db import migrations, models, transaction
from django.db.models import Count


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


def _merge_cart_group(Cart, CartItem, db_alias: str, carts: list) -> None:
    primary = carts[0]
    existing_items: dict[tuple[int, str], object] = {}

    for item in CartItem.objects.using(db_alias).filter(cart_id=primary.pk).order_by('id'):
        key = (
            item.dish_id,
            json.dumps(item.selected_options or [], sort_keys=True, ensure_ascii=False),
        )
        if key in existing_items:
            target = existing_items[key]
            new_qty = min(99, max(1, (target.quantity or 0) + (item.quantity or 0)))
            if new_qty != target.quantity:
                target.quantity = new_qty
                target.save(using=db_alias, update_fields=['quantity'])
            item.delete(using=db_alias)
        else:
            if (item.quantity or 0) > 99:
                item.quantity = 99
                item.save(using=db_alias, update_fields=['quantity'])
            existing_items[key] = item

    duplicate_ids = [c.pk for c in carts[1:]]
    for dup_item in CartItem.objects.using(db_alias).filter(cart_id__in=duplicate_ids).order_by('id'):
        key = (
            dup_item.dish_id,
            json.dumps(dup_item.selected_options or [], sort_keys=True, ensure_ascii=False),
        )
        if key in existing_items:
            target = existing_items[key]
            new_qty = min(99, max(1, (target.quantity or 0) + (dup_item.quantity or 0)))
            if new_qty != target.quantity:
                target.quantity = new_qty
                target.save(using=db_alias, update_fields=['quantity'])
            dup_item.delete(using=db_alias)
        else:
            dup_item.cart_id = primary.pk
            update_fields = ['cart_id']
            if (dup_item.quantity or 0) > 99:
                dup_item.quantity = 99
                update_fields.append('quantity')
            dup_item.save(using=db_alias, update_fields=update_fields)
            existing_items[key] = dup_item

    Cart.objects.using(db_alias).filter(pk__in=duplicate_ids).delete()


def migrate_forward(apps, schema_editor) -> None:
    db_alias = schema_editor.connection.alias
    Cart = apps.get_model('orders', 'Cart')
    CartItem = apps.get_model('orders', 'CartItem')
    Order = apps.get_model('orders', 'Order')

    with transaction.atomic(using=db_alias):
        for order in (
            Order.objects.using(db_alias)
            .filter(session_key__isnull=False)
            .exclude(session_key='')
            .only('id', 'session_key')
            .iterator()
        ):
            Order.objects.using(db_alias).filter(pk=order.pk).update(
                guest_token=_session_key_to_guest_token(order.session_key)
            )

        Cart.objects.using(db_alias).filter(user_id__isnull=False).exclude(
            guest_token=''
        ).update(guest_token='')

        dup_users = list(
            Cart.objects.using(db_alias)
            .filter(user_id__isnull=False)
            .order_by()
            .values('user_id')
            .annotate(cnt=Count('id'))
            .filter(cnt__gt=1)
            .values_list('user_id', flat=True)
        )
        for uid in dup_users:
            carts = list(
                Cart.objects.using(db_alias)
                .filter(user_id=uid)
                .order_by('-updated_at', '-id')
            )
            if len(carts) > 1:
                _merge_cart_group(Cart, CartItem, db_alias, carts)

        for cart in (
            Cart.objects.using(db_alias)
            .filter(user_id__isnull=True, session_key__isnull=False)
            .exclude(session_key='')
            .only('id', 'session_key')
            .iterator()
        ):
            Cart.objects.using(db_alias).filter(pk=cart.pk).update(
                guest_token=_session_key_to_guest_token(cart.session_key)
            )

        dup_tokens = list(
            Cart.objects.using(db_alias)
            .filter(user_id__isnull=True)
            .exclude(guest_token='')
            .order_by()
            .values('guest_token')
            .annotate(cnt=Count('id'))
            .filter(cnt__gt=1)
            .values_list('guest_token', flat=True)
        )
        for token in dup_tokens:
            carts = list(
                Cart.objects.using(db_alias)
                .filter(user_id__isnull=True, guest_token=token)
                .order_by('-updated_at', '-id')
            )
            if len(carts) > 1:
                _merge_cart_group(Cart, CartItem, db_alias, carts)

        Order.objects.using(db_alias).filter(payment_method='STRIPE').update(
            payment_method='ONLINE'
        )
        Order.objects.using(db_alias).filter(payment_status='COMPLETED').update(
            payment_status='PAID'
        )


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('orders', '0004_order_requires_manual_refund_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='cart',
            name='guest_token',
            field=models.CharField(
                blank=True,
                db_index=True,
                default='',
                max_length=64,
                verbose_name='Токен гостя',
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='guest_token',
            field=models.CharField(
                blank=True,
                db_index=True,
                default='',
                max_length=64,
                verbose_name='Токен гостя',
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='customer_email',
            field=models.EmailField(
                blank=True,
                default='',
                max_length=254,
                verbose_name='Email клієнта',
            ),
        ),
        migrations.RunPython(migrate_forward, reverse_code=migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='cart',
            name='session_key',
        ),
        migrations.RemoveField(
            model_name='order',
            name='session_key',
        ),
        migrations.AlterField(
            model_name='order',
            name='payment_method',
            field=models.CharField(
                choices=[
                    ('ONLINE', 'Онлайн-оплата карткою (Stripe)'),
                    ('CASH', "Готівкою кур'єру"),
                    ('CARD', "Терміналом кур'єру"),
                ],
                default='ONLINE',
                max_length=20,
                verbose_name='Спосіб оплати',
            ),
        ),
        migrations.AlterField(
            model_name='order',
            name='payment_status',
            field=models.CharField(
                choices=[
                    ('PENDING', 'Очікує оплати'),
                    ('PAID', 'Оплачено'),
                    ('FAILED', 'Помилка оплати'),
                    ('REFUNDED', 'Повернено'),
                    ('PARTIALLY_REFUNDED', 'Частково повернено'),
                ],
                default='PENDING',
                max_length=20,
                verbose_name='Статус оплати',
            ),
        ),
        migrations.AddConstraint(
            model_name='cart',
            constraint=models.UniqueConstraint(
                condition=models.Q(('user__isnull', False)),
                fields=('user',),
                name='unique_cart_per_user',
            ),
        ),
        migrations.AddConstraint(
            model_name='cart',
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ('user__isnull', True),
                    models.Q(('guest_token', ''), _negated=True),
                ),
                fields=('guest_token',),
                name='unique_guest_cart_per_token',
            ),
        ),
    ]
