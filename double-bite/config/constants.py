import os
from decimal import Decimal

DEFAULT_MIN_ORDER_AMOUNT = Decimal(200)
DEFAULT_FREE_DELIVERY_THRESHOLD = Decimal('500.00')
DEFAULT_DELIVERY_FEE = Decimal('65.00')
DEFAULT_RESTAURANT_ADDRESS = 'вул. Володимирська, 42, Київ'
DEFAULT_RESTAURANT_HOTLINE = '+380 44 123 45 67'


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('true', '1', 'yes', 'on')


def normalize_admin_url(raw_url: str | None) -> str:
    cleaned = (raw_url or 'admin/').strip().strip('/')
    if not cleaned:
        cleaned = 'admin'
    return f'{cleaned}/'


MIN_ORDER_AMOUNT = Decimal(os.getenv('MIN_ORDER_AMOUNT', str(DEFAULT_MIN_ORDER_AMOUNT)))
FREE_DELIVERY_THRESHOLD = Decimal(
    os.getenv('FREE_DELIVERY_THRESHOLD', str(DEFAULT_FREE_DELIVERY_THRESHOLD))
)
DELIVERY_FEE = Decimal(os.getenv('DELIVERY_FEE', str(DEFAULT_DELIVERY_FEE)))
RESTAURANT_ADDRESS = os.getenv('RESTAURANT_ADDRESS', DEFAULT_RESTAURANT_ADDRESS)
RESTAURANT_HOTLINE = os.getenv('RESTAURANT_HOTLINE', DEFAULT_RESTAURANT_HOTLINE)
