from config.emails import resolve_site_domain, resolve_site_protocol
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from support.telegram import (
    call_telegram_api,
    get_telegram_bot_token,
    get_telegram_support_chat_id,
    get_telegram_webhook_secret,
    is_telegram_enabled,
)


class Command(BaseCommand):
    help = 'Реєстрація або перевірка Telegram Bot webhook для підтримки Double Bite.'

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            '--url',
            type=str,
            default='',
            help='Повний HTTPS URL вебхука (за замовчуванням будується з SITE_PROTOCOL та SITE_DOMAIN).',
        )
        parser.add_argument(
            '--check',
            action='store_true',
            help='Лише перевірити поточний статус бота та вебхука (getMe + getWebhookInfo).',
        )
        parser.add_argument(
            '--delete',
            action='store_true',
            help='Видалити поточний Telegram webhook.',
        )
        parser.add_argument(
            '--drop-pending-updates',
            action='store_true',
            help='Скинути накопичені оновлення при setWebhook / deleteWebhook.',
        )

    def handle(self, *args, **options) -> None:
        token = get_telegram_bot_token()
        if not token:
            raise CommandError('TELEGRAM_BOT_TOKEN не налаштовано.')

        if options['check']:
            me = call_telegram_api('getMe')
            if not isinstance(me, dict):
                raise CommandError('Не вдалося отримати інформацію про бота (getMe).')
            info = call_telegram_api('getWebhookInfo')
            if not isinstance(info, dict):
                raise CommandError('Не вдалося отримати статус вебхука (getWebhookInfo).')
            self.stdout.write(
                f"Bot: @{me.get('username', '?')} (id={me.get('id', '?')})"
            )
            self.stdout.write(
                f"Enabled: {is_telegram_enabled()} | Support chat ID: {get_telegram_support_chat_id()}"
            )
            self.stdout.write(f"Webhook URL: {info.get('url') or '(не встановлено)'}")
            self.stdout.write(
                f"Pending updates: {info.get('pending_update_count', 0)}"
            )
            if info.get('last_error_message'):
                self.stdout.write(
                    self.style.WARNING(
                        f"Last error: {info['last_error_message']}"
                    )
                )
            return

        if options['delete']:
            result = call_telegram_api(
                'deleteWebhook',
                {'drop_pending_updates': bool(options['drop_pending_updates'])},
            )
            if result is None:
                raise CommandError('Не вдалося видалити Telegram webhook.')
            self.stdout.write(self.style.SUCCESS('Telegram webhook успішно видалено.'))
            return

        secret = get_telegram_webhook_secret()
        if not secret:
            raise CommandError(
                'TELEGRAM_WEBHOOK_SECRET не налаштовано. Задайте секрет перед реєстрацією webhook.'
            )

        explicit_url = str(options.get('url') or '').strip()
        if explicit_url:
            webhook_url = explicit_url
        else:
            protocol = resolve_site_protocol()
            domain = resolve_site_domain()
            path = reverse('support:telegram_webhook')
            webhook_url = f'{protocol}://{domain}{path}'

        if not webhook_url.startswith('https://'):
            raise CommandError(
                f'Telegram вимагає HTTPS URL для webhook (отримано: {webhook_url}). '
                'Передайте --url https://... або налаштуйте SITE_PROTOCOL=https та SITE_DOMAIN.'
            )

        result = call_telegram_api(
            'setWebhook',
            {
                'url': webhook_url,
                'secret_token': secret,
                'allowed_updates': ['message', 'callback_query'],
                'drop_pending_updates': bool(options['drop_pending_updates']),
            },
        )
        if result is None:
            raise CommandError('Не вдалося зареєструвати Telegram webhook.')

        self.stdout.write(
            self.style.SUCCESS(f'Telegram webhook зареєстровано: {webhook_url}')
        )
