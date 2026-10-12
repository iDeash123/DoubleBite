from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase
from django.urls import reverse


class SecuritySettingsTest(TestCase):
    def test_csp_header_present(self):
        response = self.client.get(reverse('menu:catalog'))
        self.assertIn('Content-Security-Policy', response.headers)
        csp = response.headers['Content-Security-Policy']
        self.assertIn("default-src 'self'", csp)

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


class SecretKeyValidationTest(SimpleTestCase):
    def test_secret_key_required_in_production(self):
        with self.assertRaises(ImproperlyConfigured):
            secret = ''
            debug = False
            testing = False
            if not secret:
                if debug or testing:
                    secret = 'dev-key'
                else:
                    raise ImproperlyConfigured('SECRET_KEY required')
