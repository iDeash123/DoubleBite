import mimetypes
import os
import sys
from decimal import Decimal
from pathlib import Path

mimetypes.add_type('image/webp', '.webp')
mimetypes.add_type('font/woff2', '.woff2')

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')
load_dotenv(BASE_DIR.parent / '.env')

from config.constants import (
    DEFAULT_DELIVERY_FEE,
    DEFAULT_FREE_DELIVERY_THRESHOLD,
    DEFAULT_MIN_ORDER_AMOUNT,
    DEFAULT_RESTAURANT_ADDRESS,
    DEFAULT_RESTAURANT_HOTLINE,
    env_bool,
    normalize_admin_url,
)

MIN_ORDER_AMOUNT = Decimal(os.getenv('MIN_ORDER_AMOUNT', str(DEFAULT_MIN_ORDER_AMOUNT)))
FREE_DELIVERY_THRESHOLD = Decimal(
    os.getenv('FREE_DELIVERY_THRESHOLD', str(DEFAULT_FREE_DELIVERY_THRESHOLD))
)
DELIVERY_FEE = Decimal(os.getenv('DELIVERY_FEE', str(DEFAULT_DELIVERY_FEE)))
RESTAURANT_ADDRESS = os.getenv('RESTAURANT_ADDRESS', DEFAULT_RESTAURANT_ADDRESS)
RESTAURANT_HOTLINE = os.getenv('RESTAURANT_HOTLINE', DEFAULT_RESTAURANT_HOTLINE)

DEBUG = env_bool('DEBUG', False)
is_testing = (
    'pytest' in sys.modules
    or (len(sys.argv) > 1 and sys.argv[1] == 'test')
    or env_bool('DJANGO_TESTING', False)
)

SITE_DOMAIN = os.getenv(
    'SITE_DOMAIN',
    'localhost:8000' if (DEBUG or is_testing) else 'doublebite.ua',
).strip()
SITE_PROTOCOL = os.getenv('SITE_PROTOCOL', 'http' if (DEBUG or is_testing) else 'https').strip()
DJANGO_ADMIN_URL = normalize_admin_url(os.getenv('DJANGO_ADMIN_URL', 'admin/'))

SECRET_KEY = os.getenv('SECRET_KEY', '')
if not is_testing and not DEBUG:
    if (
        not SECRET_KEY
        or SECRET_KEY.startswith('django-insecure-')
        or len(SECRET_KEY) < 50
        or len(set(SECRET_KEY)) < 5
    ):
        raise ImproperlyConfigured(
            'A strong SECRET_KEY (>= 50 chars, >= 5 unique chars, not django-insecure-*) '
            'is required when DEBUG=False'
        )
elif not SECRET_KEY:
    SECRET_KEY = 'django-insecure-doublebite-dev-secret-key-change-in-production-2026!'

ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '').split(',') if h.strip()]
if not is_testing and not DEBUG:
    if (
        not ALLOWED_HOSTS
        or 'testserver' in ALLOWED_HOSTS
        or '*' in ALLOWED_HOSTS
        or '0.0.0.0' in ALLOWED_HOSTS
    ):
        raise ImproperlyConfigured(
            'ALLOWED_HOSTS must be explicitly configured without testserver or wildcards when DEBUG=False'
        )
elif not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ['localhost', '127.0.0.1']
if is_testing and 'testserver' not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append('testserver')

CSRF_TRUSTED_ORIGINS = [
    origin.strip() for origin in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if origin.strip()
]

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',')
    if origin.strip()
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.postgres',
    'django.contrib.sitemaps',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'allauth.socialaccount.providers.github',
    'accounts',
    'menu',
    'orders',
    'support',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.middleware.csp.ContentSecurityPolicyMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'orders.middleware.PreserveSessionKeyMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

SECURE_CSP = {
    'default-src': [CSP.SELF],
    'script-src': [CSP.SELF, CSP.NONCE, CSP.UNSAFE_EVAL, 'https://js.stripe.com'],
    'style-src': [CSP.SELF, CSP.UNSAFE_INLINE],
    'img-src': [CSP.SELF, 'data:', 'https://images.unsplash.com', 'https://*.stripe.com'],
    'font-src': [CSP.SELF, 'data:'],
    'connect-src': [CSP.SELF, 'https://api.stripe.com'],
    'frame-src': [CSP.SELF, 'https://checkout.stripe.com', 'https://js.stripe.com'],
    'frame-ancestors': [CSP.SELF],
    'object-src': [CSP.NONE],
    'base-uri': [CSP.SELF],
}

if not DEBUG:
    if env_bool('TRUST_PROXY_HEADERS', False):
        SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '31536000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin'

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'

if DEBUG and not is_testing:
    INSTALLED_APPS.append('debug_toolbar')
    MIDDLEWARE.insert(0, 'debug_toolbar.middleware.DebugToolbarMiddleware')

INTERNAL_IPS = [
    '127.0.0.1',
    '::1',
    'localhost',
]

DEBUG_TOOLBAR_CONFIG = {
    'SHOW_TOOLBAR_CALLBACK': lambda request: DEBUG and not is_testing and not request.headers.get('HX-Request') and not request.GET.get('no_toolbar'),
}

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.template.context_processors.csp',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'orders.context_processors.cart_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

db_engine = os.getenv('DB_ENGINE', 'django.db.backends.sqlite3')

if is_testing and not os.getenv('TEST_WITH_POSTGRES'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'test_db.sqlite3',
        }
    }
elif 'postgresql' in db_engine:
    db_name = os.getenv('DB_NAME', '').strip()
    db_user = os.getenv('DB_USER', '').strip()
    db_password = os.getenv('DB_PASSWORD', '').strip()
    if not is_testing and (not db_name or not db_user or not db_password):
        raise ImproperlyConfigured(
            'DB_NAME, DB_USER, and DB_PASSWORD environment variables are required for PostgreSQL'
        )
    DATABASES = {
        'default': {
            'ENGINE': db_engine,
            'NAME': db_name,
            'USER': db_user,
            'PASSWORD': db_password,
            'HOST': os.getenv('DB_HOST', 'localhost'),
            'PORT': os.getenv('DB_PORT', '5432'),
        }
    }
else:
    if not is_testing and not DEBUG:
        raise ImproperlyConfigured('PostgreSQL database engine is required when DEBUG=False')
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

AUTH_USER_MODEL = 'accounts.User'

LANGUAGE_CODE = 'uk'

TIME_ZONE = 'Europe/Kyiv'

USE_I18N = True

USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static'] if (BASE_DIR / 'static').exists() else []
STATIC_ROOT = BASE_DIR / 'staticfiles'

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': (
            'whitenoise.storage.CompressedManifestStaticFilesStorage'
            if not is_testing and not DEBUG
            else 'django.contrib.staticfiles.storage.StaticFilesStorage'
        ),
    },
}

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

ADMIN_NAME = os.getenv('ADMIN_NAME', 'Double Bite Admin').strip()
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', 'admin@doublebite.ua').strip()
ADMINS = [(ADMIN_NAME, ADMIN_EMAIL)] if ADMIN_EMAIL else []

LOGS_DIR = BASE_DIR.parent / 'logs' if (BASE_DIR.parent / 'logs').exists() else BASE_DIR / 'logs'
LOG_TO_FILE = not is_testing and env_bool('LOG_TO_FILE', True)
if LOG_TO_FILE:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

ACTIVE_LOG_HANDLERS = ['console', 'file'] if LOG_TO_FILE else ['console']
REQUEST_LOG_HANDLERS = (
    [*ACTIVE_LOG_HANDLERS, 'mail_admins']
    if not DEBUG and not is_testing and ADMINS
    else ACTIVE_LOG_HANDLERS
)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'double_bite.log',
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 5,
            'encoding': 'utf-8',
            'delay': True,
            'formatter': 'verbose',
        },
        'mail_admins': {
            'level': 'ERROR',
            'class': 'django.utils.log.AdminEmailHandler',
        },
    },
    'loggers': {
        'support': {
            'handlers': ACTIVE_LOG_HANDLERS,
            'level': 'INFO',
            'propagate': True,
        },
        'orders': {
            'handlers': ACTIVE_LOG_HANDLERS,
            'level': 'WARNING',
            'propagate': True,
        },
        'accounts': {
            'handlers': ACTIVE_LOG_HANDLERS,
            'level': 'INFO',
            'propagate': True,
        },
        'menu': {
            'handlers': ACTIVE_LOG_HANDLERS,
            'level': 'INFO',
            'propagate': True,
        },
        'config': {
            'handlers': ACTIVE_LOG_HANDLERS,
            'level': 'INFO',
            'propagate': True,
        },
        'django.request': {
            'handlers': REQUEST_LOG_HANDLERS,
            'level': 'ERROR',
            'propagate': False,
        },
        'django.security': {
            'handlers': ACTIVE_LOG_HANDLERS,
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

if is_testing:
    EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
else:
    EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend')

EMAIL_HOST = os.getenv('EMAIL_HOST', 'localhost')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '1025'))
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', False)
EMAIL_USE_SSL = env_bool('EMAIL_USE_SSL', False)
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ImproperlyConfigured('EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be True')
EMAIL_TIMEOUT = int(os.getenv('EMAIL_TIMEOUT', '10'))
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'Double Bite <noreply@doublebite.ua>')

STRIPE_PUBLIC_KEY = os.getenv('STRIPE_PUBLIC_KEY', '')
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET', '')
STRIPE_ORDER_SUCCESS_STATUS = os.getenv('STRIPE_ORDER_SUCCESS_STATUS', 'PAID')

if is_testing:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'double-bite-test-cache',
        }
    }
else:
    redis_url = os.getenv('REDIS_URL', '').strip()
    if not redis_url and not DEBUG:
        raise ImproperlyConfigured('REDIS_URL environment variable is required when DEBUG=False')
    if redis_url:
        CACHES = {
            'default': {
                'BACKEND': 'django.core.cache.backends.redis.RedisCache',
                'LOCATION': redis_url,
                'OPTIONS': {
                    'socket_connect_timeout': 2,
                    'socket_timeout': 2,
                },
            }
        }
    else:
        CACHES = {
            'default': {
                'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            }
        }

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_USERNAME_REQUIRED = False
ACCOUNT_AUTHENTICATION_METHOD = 'email'
ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']
ACCOUNT_EMAIL_VERIFICATION = 'mandatory'
SOCIALACCOUNT_EMAIL_VERIFICATION = 'none'
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
SOCIALACCOUNT_ADAPTER = 'accounts.adapter.AccountSecuritySocialAdapter'
LOGIN_REDIRECT_URL = 'accounts:profile'

SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'APPS': [
            {
                'client_id': os.getenv('GOOGLE_CLIENT_ID', ''),
                'secret': os.getenv('GOOGLE_CLIENT_SECRET', ''),
                'key': '',
            },
        ],
        'SCOPE': [
            'profile',
            'email',
        ],
        'AUTH_PARAMS': {
            'access_type': 'online',
        },
    },
    'github': {
        'APPS': [
            {
                'client_id': os.getenv('GITHUB_CLIENT_ID', ''),
                'secret': os.getenv('GITHUB_CLIENT_SECRET', ''),
                'key': '',
            },
        ],
        'SCOPE': [
            'user:email',
        ],
    },
}

