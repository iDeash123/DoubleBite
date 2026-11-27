from django.apps import AppConfig


class MenuConfig(AppConfig):
    name = 'menu'

    def ready(self) -> None:
        from menu.signals import connect_menu_signals

        connect_menu_signals()
