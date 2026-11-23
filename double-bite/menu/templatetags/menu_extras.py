from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlencode

from django import template

register = template.Library()

_TWO_PLACES = Decimal("0.01")


def _to_price_decimal(value) -> Decimal | None:
    """Parse numeric / Decimal / string / lazy values into a quantized Decimal."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        amount = value
    else:
        raw = str(value).strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
        if not raw:
            return None
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            return None
    if not amount.is_finite():
        return None
    quantized = amount.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    return Decimal("0.00") if quantized == 0 else quantized


def _format_magnitude(amount: Decimal) -> str:
    """Format a non-negative quantized Decimal without currency suffix."""
    if amount == amount.to_integral_value():
        return str(int(amount))
    return f"{amount:.2f}".replace(".", ",")


@register.simple_tag(takes_context=True)
def query_transform(context, **kwargs):
    """Return the current GET querystring with the given kwargs overridden.

    Usage in template:
        href="?{% query_transform page=3 %}"
    """
    request = context.get("request")
    if request is None:
        return urlencode(kwargs)
    params = request.GET.copy()
    for key, value in kwargs.items():
        params[key] = value
    return params.urlencode()


@register.filter
def format_price(value) -> str:
    """Format a monetary amount as '485 ₴' (whole) or '485,50 ₴' (fractional)."""
    amount = _to_price_decimal(value)
    if amount is None:
        return ""
    sign = "-" if amount < 0 else ""
    return f"{sign}{_format_magnitude(abs(amount))} ₴"


@register.filter
def format_price_delta(value) -> str:
    """Format an option price delta as '+60 ₴', '+25,50 ₴', '-25 ₴', or '0 ₴'."""
    amount = _to_price_decimal(value)
    if amount is None:
        return ""
    if amount > 0:
        return f"+{_format_magnitude(amount)} ₴"
    if amount < 0:
        return f"-{_format_magnitude(abs(amount))} ₴"
    return "0 ₴"

