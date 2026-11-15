import re

from django.core.validators import RegexValidator

PHONE_REGEX = r'^\+?380[0-9]{9}\Z'
PHONE_VALIDATION_MESSAGE = 'Введіть коректний номер телефону у форматі +380XXXXXXXXX'

phone_validator = RegexValidator(
    regex=PHONE_REGEX,
    message=PHONE_VALIDATION_MESSAGE,
)

_PHONE_SEPARATOR_RE = re.compile(r'[\s\-\(\)\u2010-\u2015]+')
_NON_ASCII_DIGIT_RE = re.compile(r'[^0-9]+')


def normalize_phone(raw: str | None) -> str:
    if not raw:
        return ''
    cleaned = _PHONE_SEPARATOR_RE.sub('', str(raw).strip())
    if not cleaned:
        return ''
    if cleaned.isascii() and cleaned.isdigit():
        if len(cleaned) == 10 and cleaned.startswith('0'):
            return f'+380{cleaned[1:]}'
        if len(cleaned) == 12 and cleaned.startswith('380'):
            return f'+{cleaned}'
    return cleaned


def extract_phone_digits(raw: str | None, max_len: int = 20) -> str:
    if not raw or max_len <= 0:
        return ''
    digits = _NON_ASCII_DIGIT_RE.sub('', str(raw))
    return digits[:max_len]


def normalize_email(email: str | None) -> str:
    if not email:
        return ''
    return str(email).strip().lower()
