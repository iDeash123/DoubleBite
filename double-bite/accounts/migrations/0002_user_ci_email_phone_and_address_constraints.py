import django.core.validators
import django.db.models.functions.text
from django.db import migrations, models


def normalize_emails_and_deduplicate_defaults(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    user_model = apps.get_model('accounts', 'User')
    address_model = apps.get_model('accounts', 'DeliveryAddress')

    seen_user_emails: dict[str, int] = {}
    users_to_update = []
    for user in user_model.objects.using(db_alias).order_by('id').iterator():
        normalized = (user.email or '').strip().lower()
        if normalized in seen_user_emails:
            raise RuntimeError(
                f'Case-insensitive User email collision detected for {normalized!r} '
                f'(user ids {seen_user_emails[normalized]} and {user.id}).'
            )
        seen_user_emails[normalized] = user.id
        if user.email != normalized:
            user.email = normalized
            users_to_update.append(user)

    for user in users_to_update:
        user.save(update_fields=['email'])

    try:
        email_address_model = apps.get_model('account', 'EmailAddress')
    except LookupError:
        email_address_model = None

    if email_address_model is not None:
        seen_user_email_pairs: dict[tuple[int, str], int] = {}
        seen_verified_emails: dict[str, int] = {}
        addresses_to_update = []
        for address in (
            email_address_model.objects.using(db_alias).order_by('id').iterator()
        ):
            normalized = (address.email or '').strip().lower()
            pair_key = (address.user_id, normalized)
            if pair_key in seen_user_email_pairs:
                raise RuntimeError(
                    f'Case-insensitive EmailAddress collision detected for user '
                    f'{address.user_id} and email {normalized!r} '
                    f'(ids {seen_user_email_pairs[pair_key]} and {address.id}).'
                )
            seen_user_email_pairs[pair_key] = address.id
            if address.verified:
                if normalized in seen_verified_emails:
                    raise RuntimeError(
                        f'Case-insensitive verified EmailAddress collision detected '
                        f'for {normalized!r} (ids {seen_verified_emails[normalized]} '
                        f'and {address.id}).'
                    )
                seen_verified_emails[normalized] = address.id
            if address.email != normalized:
                address.email = normalized
                addresses_to_update.append(address)

        for address in addresses_to_update:
            address.save(update_fields=['email'])

    default_addresses = (
        address_model.objects.using(db_alias)
        .filter(is_default=True)
        .order_by('user_id', '-created_at', '-id')
    )
    seen_users: set[int] = set()
    duplicate_ids: list[int] = []
    for addr in default_addresses.iterator():
        if addr.user_id in seen_users:
            duplicate_ids.append(addr.id)
        else:
            seen_users.add(addr.user_id)
    if duplicate_ids:
        address_model.objects.using(db_alias).filter(id__in=duplicate_ids).update(
            is_default=False
        )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
        ('account', '0009_emailaddress_unique_primary_email'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(
            normalize_emails_and_deduplicate_defaults,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='user',
            name='phone',
            field=models.CharField(
                blank=True,
                max_length=20,
                validators=[
                    django.core.validators.RegexValidator(
                        message='Введіть коректний номер телефону у форматі +380XXXXXXXXX',
                        regex='^\\+?380[0-9]{9}\\Z',
                    )
                ],
                verbose_name='Номер телефону',
            ),
        ),
        migrations.AddConstraint(
            model_name='deliveryaddress',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_default', True)),
                fields=('user',),
                name='unique_default_delivery_address_per_user',
            ),
        ),
        migrations.AddConstraint(
            model_name='user',
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower('email'),
                name='unique_user_email_ci',
            ),
        ),
    ]
