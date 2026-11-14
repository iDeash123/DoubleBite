import os
import re
import subprocess
import sys
import textwrap

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

    def test_database_rejects_default_or_placeholder_password_in_production(self):
        for bad_password in ('postgres', 'change_this_to_a_secure_db_password'):
            proc = self._run_settings_import(DB_PASSWORD=bad_password)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('ImproperlyConfigured', proc.stderr)

    def test_site_domain_falls_back_to_first_allowed_host_in_production(self):
        env = os.environ.copy()
        env.update(self.BASE_PROD_ENV)
        env['ALLOWED_HOSTS'] = 'shop.doublebite.ua,www.shop.doublebite.ua'
        env.pop('SITE_DOMAIN', None)
        env.pop('SITE_PROTOCOL', None)
        code = (
            'import config.settings as s; '
            'assert s.SITE_DOMAIN == "shop.doublebite.ua", s.SITE_DOMAIN; '
            'assert s.SITE_PROTOCOL == "https", s.SITE_PROTOCOL'
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

        empty_override = self._run_settings_import(
            code=code,
            ALLOWED_HOSTS='shop.doublebite.ua',
            SITE_DOMAIN='',
        )
        self.assertEqual(empty_override.returncode, 0, empty_override.stderr)

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

    def test_dev_static_and_media_cache_control_no_cache(self):
        env = os.environ.copy()
        env.update(self.BASE_PROD_ENV)
        env['DEBUG'] = 'True'
        env['DJANGO_TESTING'] = '0'
        env['DB_ENGINE'] = 'django.db.backends.sqlite3'
        code = textwrap.dedent("""
            import asyncio, pathlib, tempfile, config.asgi as asgi_mod, config.urls as urls_mod
            from django.test import RequestFactory

            async def check_asgi():
                events = []
                reqs = [{"type": "http.request", "body": b"", "more_body": False}]
                async def receive():
                    if reqs:
                        return reqs.pop(0)
                    await asyncio.sleep(1)
                    return {"type": "http.disconnect"}
                async def send(msg):
                    events.append(msg)
                scope = {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/static/css/fonts.css",
                    "raw_path": b"/static/css/fonts.css",
                    "query_string": b"",
                    "headers": [],
                }
                await asgi_mod.application(scope, receive, send)
                start = next(e for e in events if e["type"] == "http.response.start")
                assert start["status"] == 200, start
                hdrs = {k.lower(): v for k, v in start["headers"]}
                assert hdrs.get(b"cache-control") == b"no-cache, must-revalidate", hdrs

            asyncio.run(check_asgi())
            rf = RequestFactory()
            with tempfile.TemporaryDirectory() as tmp:
                pathlib.Path(tmp, "sample.txt").write_text("ok", encoding="utf-8")
                resp = urls_mod.cached_media_serve(
                    rf.get("/media/sample.txt"), "sample.txt", document_root=tmp
                )
                try:
                    assert resp.status_code == 200
                    assert resp["Cache-Control"] == "no-cache, must-revalidate", resp.headers
                finally:
                    resp.close()
        """)
        proc = subprocess.run(
            [sys.executable, '-c', code],
            cwd=str(settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_debug_true_while_testing_does_not_crash_url_resolution(self):
        code = textwrap.dedent("""
            import django
            from django.conf import settings
            from django.template.loader import render_to_string
            from django.urls import resolve, reverse

            django.setup()
            assert settings.DEBUG is True
            assert 'debug_toolbar' not in settings.INSTALLED_APPS
            assert reverse('home') == '/'
            assert reverse('menu:catalog') == '/menu/'
            assert resolve('/').url_name == 'home'
            rendered = render_to_string(
                'emails/password_reset_email.html',
                {'protocol': 'http', 'domain': 'testserver', 'uid': 'MQ', 'token': 'abc'},
            )
            assert '/accounts/reset/MQ/abc/' in rendered
        """)
        proc = self._run_settings_import(
            code=code,
            DEBUG='True',
            DJANGO_TESTING='1',
            DB_ENGINE='django.db.backends.sqlite3',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_debug_true_when_not_testing_mounts_debug_toolbar(self):
        code = textwrap.dedent("""
            import django
            from django.conf import settings
            from django.urls import resolve, reverse

            django.setup()
            assert settings.DEBUG is True
            assert 'debug_toolbar' in settings.INSTALLED_APPS
            assert reverse('home') == '/'
            assert resolve('/__debug__/render_panel/').app_name == 'djdt'
        """)
        proc = self._run_settings_import(
            code=code,
            DEBUG='True',
            DJANGO_TESTING='0',
            DB_ENGINE='django.db.backends.sqlite3',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_empty_env_vars_fall_back_to_clean_defaults(self):
        code = textwrap.dedent("""
            import config.constants as c
            import config.settings as s

            assert s.MIN_ORDER_AMOUNT == c.DEFAULT_MIN_ORDER_AMOUNT
            assert s.FREE_DELIVERY_THRESHOLD == c.DEFAULT_FREE_DELIVERY_THRESHOLD
            assert s.DELIVERY_FEE == c.DEFAULT_DELIVERY_FEE
            assert s.RESTAURANT_ADDRESS == c.DEFAULT_RESTAURANT_ADDRESS
            assert s.RESTAURANT_HOTLINE == c.DEFAULT_RESTAURANT_HOTLINE
            assert s.EMAIL_PORT == 1025
            assert s.EMAIL_TIMEOUT == 10
            assert s.SECURE_HSTS_SECONDS == 31536000
            assert s.DEFAULT_FROM_EMAIL == 'Double Bite <noreply@doublebite.ua>'
            assert s.STRIPE_ORDER_SUCCESS_STATUS == 'PAID'
            assert s.ADMIN_NAME == 'Double Bite Admin'
            for attr in (
                'MIN_ORDER_AMOUNT',
                'FREE_DELIVERY_THRESHOLD',
                'DELIVERY_FEE',
                'RESTAURANT_ADDRESS',
                'RESTAURANT_HOTLINE',
            ):
                assert not hasattr(c, attr), f'Duplicate env global {attr} in config.constants'
        """)
        proc = self._run_settings_import(
            code=code,
            MIN_ORDER_AMOUNT='',
            FREE_DELIVERY_THRESHOLD='   ',
            DELIVERY_FEE='',
            RESTAURANT_ADDRESS='',
            RESTAURANT_HOTLINE='   ',
            EMAIL_PORT='',
            EMAIL_TIMEOUT='',
            SECURE_HSTS_SECONDS='',
            DEFAULT_FROM_EMAIL='   ',
            STRIPE_ORDER_SUCCESS_STATUS='',
            ADMIN_NAME='',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_invalid_decimal_env_raises_improperly_configured(self):
        for bad_value in ('not-a-number', 'NaN', 'Infinity'):
            proc = self._run_settings_import(MIN_ORDER_AMOUNT=bad_value)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('ImproperlyConfigured', proc.stderr)
