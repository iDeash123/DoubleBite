from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialApp
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

User = get_user_model()


class OAuthConfigurationTest(TestCase):
    def test_allauth_installed_apps(self):
        expected_apps = [
            'allauth',
            'allauth.account',
            'allauth.socialaccount',
            'allauth.socialaccount.providers.google',
            'allauth.socialaccount.providers.github',
        ]
        for app in expected_apps:
            self.assertIn(app, settings.INSTALLED_APPS)

    def test_authentication_backends(self):
        self.assertIn(
            'allauth.account.auth_backends.AuthenticationBackend',
            settings.AUTHENTICATION_BACKENDS,
        )
        self.assertIn(
            'django.contrib.auth.backends.ModelBackend',
            settings.AUTHENTICATION_BACKENDS,
        )

    def test_account_middleware_installed(self):
        self.assertIn(
            'allauth.account.middleware.AccountMiddleware',
            settings.MIDDLEWARE,
        )

    def test_allauth_custom_user_settings(self):
        self.assertIsNone(settings.ACCOUNT_USER_MODEL_USERNAME_FIELD)
        self.assertTrue(settings.ACCOUNT_EMAIL_REQUIRED)
        self.assertFalse(settings.ACCOUNT_USERNAME_REQUIRED)
        self.assertEqual(settings.ACCOUNT_AUTHENTICATION_METHOD, 'email')
        self.assertTrue(settings.SOCIALACCOUNT_AUTO_SIGNUP)
        self.assertTrue(settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT)

    def test_socialaccount_providers_configured(self):
        self.assertIn('google', settings.SOCIALACCOUNT_PROVIDERS)
        self.assertIn('github', settings.SOCIALACCOUNT_PROVIDERS)
        self.assertEqual(
            settings.SOCIALACCOUNT_PROVIDERS['google']['SCOPE'],
            ['profile', 'email'],
        )
        self.assertEqual(
            settings.SOCIALACCOUNT_PROVIDERS['github']['SCOPE'],
            ['user:email'],
        )

    def test_oauth_urls_resolve(self):
        self.assertEqual(reverse('google_login'), '/accounts/google/login/')
        self.assertEqual(reverse('github_login'), '/accounts/github/login/')

    def test_google_login_post_redirects_to_oauth(self):
        client = Client()
        response = client.post(reverse('google_login'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('accounts.google.com', response.get('Location', ''))

    def test_github_login_post_redirects_to_oauth(self):
        client = Client()
        response = client.post(reverse('github_login'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('github.com', response.get('Location', ''))

    def test_allauth_models_exist_in_db(self):
        self.assertEqual(SocialAccount.objects.count(), 0)
        self.assertEqual(SocialApp.objects.count(), 0)
        self.assertEqual(EmailAddress.objects.count(), 0)
