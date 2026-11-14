import os
import re
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse


class SecuritySettingsTest(TestCase):
    def test_csp_header_present(self):
        response = self.client.get(reverse('menu:catalog'))
        self.assertIn('Content-Security-Policy', response.headers)
        csp = response.headers['Content-Security-Policy']
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'self'", csp)
        nonce_match = re.search(r"'nonce-([^']+)'", csp)
        self.assertIsNotNone(nonce_match)
        script_src_match = re.search(r"script-src ([^;]+)", csp)
        self.assertIsNotNone(script_src_match)
        self.assertNotIn("'unsafe-inline'", script_src_match.group(1))
        html = response.content.decode('utf-8')
        self.assertIn(f'nonce="{nonce_match.group(1)}"', html)

    def test_cookie_security_settings(self):
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, 'Lax')

    def test_allowed_hosts_no_wildcard_zero(self):
        self.assertNotIn('0.0.0.0', settings.ALLOWED_HOSTS)

    def test_logging_uses_rotating_file_handler(self):
        file_handler_cfg = settings.LOGGING['handlers']['file']
        self.assertEqual(file_handler_cfg['class'], 'logging.handlers.RotatingFileHandler')
        self.assertGreater(file_handler_cfg['maxBytes'], 0)
        self.assertGreater(file_handler_cfg['backupCount'], 0)
        self.assertEqual(file_handler_cfg['formatter'], 'verbose')
        self.assertEqual(settings.LOGGING['handlers']['console']['formatter'], 'verbose')
        for logger_name in ('accounts', 'menu', 'config', 'django.request', 'django.security'):
            self.assertIn(logger_name, settings.LOGGING['loggers'])

    def test_whitenoise_middleware_and_unified_constants_configured(self):
        sec_idx = settings.MIDDLEWARE.index('django.middleware.security.SecurityMiddleware')
        self.assertEqual(
            settings.MIDDLEWARE[sec_idx + 1],
            'whitenoise.middleware.WhiteNoiseMiddleware',
        )
        self.assertIn(
            'django.template.context_processors.csp',
            settings.TEMPLATES[0]['OPTIONS']['context_processors'],
        )
        for attr in (
            'MIN_ORDER_AMOUNT',
            'FREE_DELIVERY_THRESHOLD',
            'DELIVERY_FEE',
            'RESTAURANT_ADDRESS',
            'RESTAURANT_HOTLINE',
            'DJANGO_ADMIN_URL',
            'SITE_DOMAIN',
            'SITE_PROTOCOL',
        ):
            self.assertTrue(hasattr(settings, attr))


class SecretKeyValidationTest(SimpleTestCase):
    BASE_PROD_ENV = {
        'DEBUG': 'False',
        'DJANGO_TESTING': '0',
        'SECRET_KEY': 'aB3$cD6&eF9*gH2^jK5(lM8)nP1!qR4@sT7#uV0%wX2+yZ4=567',
        'ALLOWED_HOSTS': 'doublebite.ua',
        'DB_ENGINE': 'django.db.backends.postgresql',
        'DB_NAME': 'double_bite_db',
        'DB_USER': 'double_bite',
        'DB_PASSWORD': 'strong-db-password-123',
        'REDIS_URL': 'redis://:redispass@localhost:6379/0',
        'EMAIL_USE_TLS': 'False',
        'EMAIL_USE_SSL': 'False',
        'TRUST_PROXY_HEADERS': 'False',
    }

    def _run_settings_import(self, code: str = 'import config.settings', **overrides):
        env = os.environ.copy()
        env.update(self.BASE_PROD_ENV)
        env.update(overrides)
        return subprocess.run(
            [sys.executable, '-c', code],
            cwd=str(settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_secret_key_required_in_production(self):
        env = os.environ.copy()
        env['DEBUG'] = 'False'
        env['SECRET_KEY'] = ''
        code = 'import config.settings'
        proc = subprocess.run(
            [sys.executable, '-c', code],
            cwd=str(settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('ImproperlyConfigured', proc.stderr)

    def test_secret_key_rejects_weak_values_in_production(self):
        for bad_key in (
            'django-insecure-this-is-not-allowed-in-production-at-all-123456',
            'too-short-secret-key',
            'a' * 60,
        ):
            proc = self._run_settings_import(SECRET_KEY=bad_key)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('ImproperlyConfigured', proc.stderr)

    def test_allowed_hosts_rejects_empty_and_testserver_in_production(self):
        for bad_hosts in ('', 'testserver', 'doublebite.ua,testserver', '*'):
            proc = self._run_settings_import(ALLOWED_HOSTS=bad_hosts)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('ImproperlyConfigured', proc.stderr)

    def test_database_credentials_required_when_not_testing(self):
        for field in ('DB_NAME', 'DB_USER', 'DB_PASSWORD'):
            proc = self._run_settings_import(**{field: ''})
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('ImproperlyConfigured', proc.stderr)

    def test_redis_url_required_in_production(self):
        proc = self._run_settings_import(REDIS_URL='')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('ImproperlyConfigured', proc.stderr)

    def test_email_tls_and_ssl_cannot_both_be_true(self):
        proc = self._run_settings_import(EMAIL_USE_TLS='True', EMAIL_USE_SSL='True')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('ImproperlyConfigured', proc.stderr)

    def test_production_settings_hardening_details(self):
        code = (
            'import config.settings as s; '
            'assert s.STORAGES["staticfiles"]["BACKEND"] == "whitenoise.storage.CompressedManifestStaticFilesStorage"; '
            'assert not hasattr(s, "SECURE_PROXY_SSL_HEADER"); '
            'assert "mail_admins" in s.LOGGING["loggers"]["django.request"]["handlers"]; '
            'assert s.CACHES["default"]["OPTIONS"] == {"socket_connect_timeout": 2, "socket_timeout": 2}; '
            'assert s.DJANGO_ADMIN_URL == "custom-panel/"'
        )
        proc = self._run_settings_import(code=code, DJANGO_ADMIN_URL='/custom-panel')
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proxy_code = (
            'import config.settings as s; '
            'assert s.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")'
        )
        proxy_proc = self._run_settings_import(code=proxy_code, TRUST_PROXY_HEADERS='True')
        self.assertEqual(proxy_proc.returncode, 0, proxy_proc.stderr)

    def test_is_testing_ignores_unrelated_test_flags(self):
        env = os.environ.copy()
        env.update(self.BASE_PROD_ENV)
        env['DEBUG'] = 'True'
        code = 'import sys, config.settings as s; assert s.is_testing is False'
        proc = subprocess.run(
            [sys.executable, '-c', code, '--test'],
            cwd=str(settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_asgi_does_not_enable_django_allow_async_unsafe(self):
        env = os.environ.copy()
        env.pop('DJANGO_ALLOW_ASYNC_UNSAFE', None)
        env.update(self.BASE_PROD_ENV)
        env['DEBUG'] = 'True'
        code = (
            'import os, config.asgi; '
            'assert "DJANGO_ALLOW_ASYNC_UNSAFE" not in os.environ'
        )
        proc = subprocess.run(
            [sys.executable, '-c', code],
            cwd=str(settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
