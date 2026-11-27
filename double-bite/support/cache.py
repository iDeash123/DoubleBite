import logging
from typing import Any

from config.constants import DEFAULT_AI_MENU_CACHE_TTL_SEC
from django.conf import settings
from django.core.cache import DEFAULT_CACHE_ALIAS, caches
from django.db.models import Prefetch, Q
from django.utils import timezone
from menu.models import Category, Dish, DishOption, Promotion
from redis.exceptions import RedisError

from support.models import FAQKnowledge

logger = logging.getLogger('support')

STATIC_AI_PREFIX_CACHE_KEY = 'support:ai:static_prefix:v1'
_CACHE_ERRORS = (RedisError, OSError, TimeoutError, ValueError, RuntimeError)


def _get_cache_ttl() -> int:
    raw = getattr(settings, 'AI_MENU_CACHE_TTL_SEC', DEFAULT_AI_MENU_CACHE_TTL_SEC)
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_AI_MENU_CACHE_TTL_SEC
    return parsed if parsed >= 1 else DEFAULT_AI_MENU_CACHE_TTL_SEC


def _normalize_single_line(value: Any) -> str:
    return ' '.join(str(value or '').split())


def _format_dish_entry(dish: Dish) -> str:
    if dish.allergen_codes:
        allergens = ','.join(sorted(str(code) for code in dish.allergen_codes))
    else:
        allergens = _normalize_single_line(dish.allergens) or '-'

    flags: list[str] = []
    if dish.is_vegetarian:
        flags.append('veg')
    if dish.is_vegan:
        flags.append('vegan')
    if dish.is_spicy:
        flags.append('spicy')
    if dish.contains_raw_fish:
        flags.append('raw_fish')
    flags.append('preg_ok' if dish.safe_for_pregnancy else 'preg_no')
    flags.append(f'group:{dish.upsell_group}')

    options = list(dish.options.all())
    if options:
        options_str = ';'.join(
            f'{_normalize_single_line(opt.name)}:{opt.price_delta:+.2f}'
            for opt in options
        )
    else:
        options_str = '-'

    ingredients = _normalize_single_line(dish.ingredients) or '-'
    return (
        f'{dish.id}|{_normalize_single_line(dish.title)}|'
        f'{_normalize_single_line(dish.category.name)}|{dish.price:.2f}|'
        f'{dish.weight_grams}g|{dish.calories}kcal|'
        f'{dish.proteins_grams}/{dish.fats_grams}/{dish.carbs_grams}|'
        f'{allergens}|{",".join(flags)}|{options_str}|{ingredients}'
    )


def _format_promotion_entry(promo: Promotion) -> str:
    cat_slugs = [cat.slug for cat in promo.applicable_categories.all()]
    dish_ids = [str(dish.id) for dish in promo.applicable_dishes.all()]
    scope_parts: list[str] = []
    if cat_slugs:
        scope_parts.append(f'cats:{",".join(cat_slugs)}')
    if dish_ids:
        scope_parts.append(f'dishes:{",".join(dish_ids)}')
    scope = ';'.join(scope_parts) if scope_parts else 'all'
    return (
        f'{promo.id}|{_normalize_single_line(promo.title)}|{promo.promo_type}|'
        f'{promo.discount_value:.2f}|min:{promo.min_order_amount:.2f}|'
        f'{scope}|{_normalize_single_line(promo.description)}'
    )


def _format_faq_entry(faq: FAQKnowledge) -> str:
    category = _normalize_single_line(faq.category) or 'general'
    return (
        f'{faq.id}|{category}|{_normalize_single_line(faq.question)}|'
        f'{_normalize_single_line(faq.answer)}'
    )


def build_static_ai_prefix() -> str:
    from support.agent.prompts import SYSTEM_PROMPT

    dishes = (
        Dish.objects.filter(is_available=True, category__is_active=True)
        .select_related('category')
        .prefetch_related(
            Prefetch(
                'options',
                queryset=DishOption.objects.order_by('price_delta', 'name', 'id'),
            )
        )
        .order_by('category__display_order', 'category__id', 'id')
    )
    dish_lines = [_format_dish_entry(dish) for dish in dishes]

    now = timezone.now()
    promotions = (
        Promotion.objects.filter(is_active=True)
        .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
        .prefetch_related(
            Prefetch(
                'applicable_categories',
                queryset=Category.objects.order_by('display_order', 'id'),
            ),
            Prefetch(
                'applicable_dishes',
                queryset=Dish.objects.order_by('id'),
            ),
        )
        .order_by('id')
    )
    promo_lines = [_format_promotion_entry(promo) for promo in promotions]

    faqs = FAQKnowledge.objects.filter(is_active=True).order_by('category', 'id')
    faq_lines = [_format_faq_entry(faq) for faq in faqs]

    menu_block = '\n'.join(dish_lines) if dish_lines else '(немає доступних страв)'
    promo_block = '\n'.join(promo_lines) if promo_lines else '(немає активних акцій)'
    faq_block = '\n'.join(faq_lines) if faq_lines else '(немає активних записів FAQ)'

    return (
        f'{SYSTEM_PROMPT}\n\n'
        f'[АКТУАЛЬНИЙ РЕЄСТР ДОСТУПНОГО МЕНЮ]\n{menu_block}\n\n'
        f'[АКТИВНІ АКЦІЇ]\n{promo_block}\n\n'
        f'[ПРАВИЛА СЕРВІСУ ТА FAQ]\n{faq_block}'
    )


def get_static_ai_prefix(
    *,
    force_refresh: bool = False,
    cache_backend: Any = None,
) -> str:
    backend = cache_backend if cache_backend is not None else caches[DEFAULT_CACHE_ALIAS]
    if not force_refresh:
        try:
            cached = backend.get(STATIC_AI_PREFIX_CACHE_KEY)
            if isinstance(cached, str) and cached:
                return cached
        except _CACHE_ERRORS as exc:
            logger.warning(
                'Failed to read static AI prefix cache (%s), falling back to DB: %s',
                STATIC_AI_PREFIX_CACHE_KEY,
                exc,
            )

    prefix = build_static_ai_prefix()
    try:
        backend.set(STATIC_AI_PREFIX_CACHE_KEY, prefix, timeout=_get_cache_ttl())
    except _CACHE_ERRORS as exc:
        logger.warning(
            'Failed to store static AI prefix cache (%s): %s',
            STATIC_AI_PREFIX_CACHE_KEY,
            exc,
        )
    return prefix


def invalidate_static_ai_prefix(*, cache_backend: Any = None) -> bool:
    backend = cache_backend if cache_backend is not None else caches[DEFAULT_CACHE_ALIAS]
    try:
        backend.delete(STATIC_AI_PREFIX_CACHE_KEY)
        return True
    except _CACHE_ERRORS as exc:
        logger.warning(
            'Failed to invalidate static AI prefix cache (%s): %s',
            STATIC_AI_PREFIX_CACHE_KEY,
            exc,
        )
        return False
