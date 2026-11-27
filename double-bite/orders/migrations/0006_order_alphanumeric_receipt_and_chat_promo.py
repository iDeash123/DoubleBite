import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('menu', '0006_dish_nutrition_allergens_and_promotions'),
        ('orders', '0005_guest_token_payment_enums_and_constraints'),
        ('support', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='applied_promotion',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='orders', to='menu.promotion', verbose_name='Застосована акція'),
        ),
        migrations.AddField(
            model_name='order',
            name='chat_idempotency_key',
            field=models.CharField(blank=True, db_index=True, default='', max_length=64, verbose_name='Ключ ідемпотентності чату'),
        ),
        migrations.AddField(
            model_name='order',
            name='delivery_fee_amount',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=8, verbose_name='Вартість доставки (грн)'),
        ),
        migrations.AddField(
            model_name='order',
            name='discount_amount',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=10, verbose_name='Знижка (грн)'),
        ),
        migrations.AddField(
            model_name='order',
            name='receipt_fiscal_code',
            field=models.CharField(blank=True, default='', max_length=32, verbose_name='Фіскальний код чека'),
        ),
        migrations.AddField(
            model_name='order',
            name='receipt_issued_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Час видачі чека'),
        ),
        migrations.AddField(
            model_name='order',
            name='receipt_number',
            field=models.CharField(blank=True, db_index=True, default='', max_length=32, verbose_name='Номер чека'),
        ),
        migrations.AddField(
            model_name='order',
            name='receipt_sent_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Чек надіслано о'),
        ),
        migrations.AddField(
            model_name='order',
            name='source_chat_session',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_orders', to='support.chatsession', verbose_name='Сесія чату'),
        ),
        migrations.AddField(
            model_name='order',
            name='subtotal_amount',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=10, verbose_name='Сума страв (грн)'),
        ),
        migrations.AddConstraint(
            model_name='order',
            constraint=models.UniqueConstraint(condition=models.Q(('chat_idempotency_key', ''), _negated=True), fields=('chat_idempotency_key',), name='unique_order_chat_idempotency_key'),
        ),
        migrations.AddConstraint(
            model_name='order',
            constraint=models.UniqueConstraint(condition=models.Q(('receipt_number', ''), _negated=True), fields=('receipt_number',), name='unique_order_receipt_number'),
        ),
    ]
