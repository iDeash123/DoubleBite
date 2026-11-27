import logging
import math
import pickle
from datetime import datetime
from typing import Any

from config.constants import DEFAULT_AI_MENU_CACHE_TTL_SEC
from django.conf import settings
from django.core.cache import DEFAULT_CACHE_ALIAS, caches
from django.db.models import Prefetch
from django.utils import timezone
from menu.models import Category, Dish, DishOption, Promotion
from redis.exceptions import RedisError

from support.models import FAQKnowledge

logger = logging.getLogger('support')

STATIC_AI_PREFIX_CACHE_KEY = 'support:ai:static_prefix:v1'
_CACHE_ERRORS = (
    RedisError,
    OSError,
    TimeoutError,
    ValueError,
    RuntimeError,
    EOFError,
    pickle.PickleError,
)


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
        normalized_codes = sorted(
            {
                str(code).strip().lower()
                for code in dish.allergen_codes
                if str(code).strip()
            }
        )
        allergens = ','.join(normalized_codes) or '-'
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


def _format_promotion_entry(
    promo: Promotion,
    active_categories: list[Category],
    available_dishes: list[Dish],
) -> str:
    cat_slugs = [cat.slug for cat in active_categories]
    dish_ids = [str(dish.id) for dish in available_dishes]
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


def _clamp_ttl_to_boundary(current_ttl: int, now: datetime, boundary: datetime) -> int:
    remaining = (boundary - now).total_seconds()
    if remaining <= 0:
        return current_ttl
    boundary_ttl = max(1, math.ceil(remaining))
    return min(current_ttl, boundary_ttl)


def _build_static_ai_prefix_with_ttl(base_ttl: int) -> tuple[str, int]:
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
    effective_ttl = max(1, int(base_ttl))
    promotions = (
        Promotion.objects.filter(is_active=True)
        .prefetch_related(
            Prefetch(
                'applicable_categories',
                queryset=Category.objects.order_by('display_order', 'id'),
            ),
            Prefetch(
                'applicable_dishes',
                queryset=Dish.objects.select_related('category').order_by('id'),
            ),
        )
        .order_by('id')
    )
    promo_lines: list[str] = []
    for promo in promotions:
        if promo.starts_at is not None and promo.starts_at > now:
            effective_ttl = _clamp_ttl_to_boundary(effective_ttl, now, promo.starts_at)
            continue
        if promo.ends_at is not None and promo.ends_at <= now:
            continue
        if promo.ends_at is not None:
            effective_ttl = _clamp_ttl_to_boundary(effective_ttl, now, promo.ends_at)

        raw_cats = list(promo.applicable_categories.all())
        raw_dishes = list(promo.applicable_dishes.all())
        active_cats = [cat for cat in raw_cats if cat.is_active]
        avail_dishes = [
            dish for dish in raw_dishes if dish.is_available and dish.category.is_active
        ]
        if (raw_cats or raw_dishes) and not (active_cats or avail_dishes):
            continue
        promo_lines.append(_format_promotion_entry(promo, active_cats, avail_dishes))

    faqs = FAQKnowledge.objects.filter(is_active=True).order_by('category', 'id')
    faq_lines = [_format_faq_entry(faq) for faq in faqs]

    menu_block = '\n'.join(dish_lines) if dish_lines else '(немає доступних страв)'
    promo_block = '\n'.join(promo_lines) if promo_lines else '(немає активних акцій)'
    faq_block = '\n'.join(faq_lines) if faq_lines else '(немає активних записів FAQ)'

    prefix = (
        f'{SYSTEM_PROMPT}\n\n'
        f'[АКТУАЛЬНИЙ РЕЄСТР ДОСТУПНОГО МЕНЮ]\n{menu_block}\n\n'
        f'[АКТИВНІ АКЦІЇ]\n{promo_block}\n\n'
        f'[ПРАВИЛА СЕРВІСУ ТА FAQ]\n{faq_block}'
    )
    return prefix, effective_ttl


def build_static_ai_prefix() -> str:
    prefix, _ttl = _build_static_ai_prefix_with_ttl(_get_cache_ttl())
    return prefix


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

    prefix, effective_ttl = _build_static_ai_prefix_with_ttl(_get_cache_ttl())
    try:
        backend.set(STATIC_AI_PREFIX_CACHE_KEY, prefix, timeout=effective_ttl)
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
