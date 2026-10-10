import secrets
from django.db import migrations, models


def gen_tokens(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    used = set()
    for order in Order.objects.all():
        token = secrets.token_urlsafe(32)
        while token in used:
            token = secrets.token_urlsafe(32)
        used.add(token)
        order.access_token = token
        order.save(update_fields=['access_token'])


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0002_order_stripe_payment_intent_id_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProcessedStripeEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_id', models.CharField(db_index=True, max_length=255, unique=True, verbose_name='ID події Stripe')),
                ('event_type', models.CharField(max_length=100, verbose_name='Тип події')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Створено')),
            ],
            options={
                'verbose_name': 'Оброблена подія Stripe',
                'verbose_name_plural': 'Оброблені події Stripe',
                'ordering': ('-created_at',),
            },
        ),
        migrations.AddField(
            model_name='order',
            name='access_token',
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                verbose_name='Токен доступу',
            ),
        ),
        migrations.RunPython(gen_tokens, reverse_code=migrations.RunPython.noop),
        migrations.AlterField(
            model_name='order',
            name='access_token',
            field=models.CharField(
                db_index=True,
                default=secrets.token_urlsafe,
                editable=False,
                max_length=64,
                unique=True,
                verbose_name='Токен доступу',
            ),
        ),
    ]
