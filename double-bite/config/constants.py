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
