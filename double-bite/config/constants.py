import ipaddress
import os
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ImproperlyConfigured

DEFAULT_MIN_ORDER_AMOUNT = Decimal(200)
DEFAULT_FREE_DELIVERY_THRESHOLD = Decimal('500.00')
DEFAULT_DELIVERY_FEE = Decimal('65.00')
DEFAULT_RESTAURANT_ADDRESS = 'вул. Володимирська, 42, Київ'
DEFAULT_RESTAURANT_HOTLINE = '+380 44 123 45 67'
DEFAULT_SUPPORT_WORK_START_HOUR = 10
DEFAULT_SUPPORT_WORK_END_HOUR = 22
DEFAULT_GEMINI_MODEL = 'gemini-2.5-flash'
DEFAULT_GEMINI_FALLBACK_MODEL = 'gemini-2.5-flash-lite'
DEFAULT_GEMINI_VISION_MODEL = ''
DEFAULT_GEMINI_EMBEDDING_MODEL = 'gemini-embedding-2'
DEFAULT_GEMINI_TEMPERATURE = Decimal('0.3')
DEFAULT_GEMINI_MAX_TOKENS = 1024
DEFAULT_GEMINI_TIMEOUT_MS = 30000
DEFAULT_GEMINI_REASONING_EFFORT = 'low'
DEFAULT_GEMINI_CIRCUIT_BREAKER_THRESHOLD = 3
DEFAULT_GEMINI_CIRCUIT_BREAKER_COOLDOWN_SEC = 120
DEFAULT_AI_MENU_CACHE_TTL_SEC = 300
DEFAULT_AI_MEMORY_SUMMARY_TRIGGER_MSGS = 16
DEFAULT_AI_CHAT_MAX_ATTACHMENTS = 3
DEFAULT_AI_CHAT_MAX_ATTACHMENT_MB = 5
DEFAULT_TELEGRAM_MEDIA_TIMEOUT_SEC = 6
DEFAULT_RECEIPT_MERCHANT_NAME = 'ТОВ «ДАБЛ БАЙТ»'
DEFAULT_RECEIPT_MERCHANT_TAX_ID = '44891205'
DEFAULT_RECEIPT_FISCAL_POS_ID = 'FN-4000918273'
UNSPECIFIED_IPV4 = str(ipaddress.IPv4Address(0))
DISALLOWED_ALLOWED_HOSTS = frozenset({'testserver', UNSPECIFIED_IPV4})


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = raw.strip().lower()
    if not cleaned:
        return default
    return cleaned in ('true', '1', 'yes', 'on')


def env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = raw.strip()
    return cleaned or default


def env_decimal(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = raw.strip()
    if not cleaned:
        return default
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ImproperlyConfigured(f'Invalid decimal value for {name}: {raw!r}') from exc
    if not value.is_finite():
        raise ImproperlyConfigured(f'Invalid finite decimal value for {name}: {raw!r}')
    return value


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = raw.strip()
    if not cleaned:
        return default
    try:
        return int(cleaned)
    except ValueError as exc:
        raise ImproperlyConfigured(f'Invalid integer value for {name}: {raw!r}') from exc


def normalize_admin_url(raw_url: str | None) -> str:
    cleaned = (raw_url or 'admin/').strip().strip('/')
    if not cleaned:
        cleaned = 'admin'
    return f'{cleaned}/'
