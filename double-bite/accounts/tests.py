from django.apps import apps
from django.test import TestCase


class AccountsConfigTest(TestCase):
    def test_app_is_installed(self):
        self.assertTrue(apps.is_installed('accounts'))
