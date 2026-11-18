from importlib import import_module

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = 'accounts'

    def ready(self) -> None:
        import_module('accounts.signals')
