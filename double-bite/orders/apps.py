import os

import stripe
from django.apps import AppConfig
from django.conf import settings


class OrdersConfig(AppConfig):
    name = 'orders'

    def ready(self) -> None:
        secret_key = getattr(settings, 'STRIPE_SECRET_KEY', '') or os.getenv('STRIPE_SECRET_KEY', '')
        if secret_key:
            stripe.api_key = secret_key
        import orders.signals  # noqa
