from decimal import Decimal
from typing import Any

from django.db.models import Q, QuerySet

from .models import Category, Dish


def get_active_categories() -> QuerySet[Category]:
    return Category.objects.filter(is_active=True).order_by('display_order', 'name')


def get_available_dishes() -> QuerySet[Dish]:
    return (
        Dish.objects.filter(is_available=True, category__is_active=True)
        .select_related('category')
        .prefetch_related('options')
        .order_by('category__display_order', 'title')
    )


def get_dish_by_slug(category_slug: str, dish_slug: str) -> Dish | None:
    try:
        return (
            Dish.objects.select_related('category')
            .prefetch_related('options')
            .get(category__slug=category_slug, slug=dish_slug)
        )
    except Dish.DoesNotExist:
        return None


def get_available_dish_by_slug(dish_slug: str, category_slug: str | None = None) -> Dish | None:
    try:
        qs = (
            Dish.objects.filter(is_available=True, category__is_active=True)
            .select_related('category')
            .prefetch_related('options')
        )
        if category_slug:
            qs = qs.filter(category__slug=category_slug)
        return qs.get(slug=dish_slug)
    except Dish.DoesNotExist:
        return None


def get_dish_by_id(dish_id: int) -> Dish | None:
    try:
        return (
            Dish.objects.select_related('category')
            .prefetch_related('options')
            .get(pk=dish_id)
        )
    except Dish.DoesNotExist:
        return None


def filter_dishes(
    *,
    category_slug: str | None = None,
    query: str | None = None,
    max_price: Decimal | float | None = None,
    is_vegetarian: bool | None = None,
    is_spicy: bool | None = None,
    max_calories: int | None = None,
    exclude_allergens: list[str] | None = None,
    only_available: bool = True,
) -> QuerySet[Dish]:
    queryset = Dish.objects.select_related('category').prefetch_related('options')

    if only_available:
        queryset = queryset.filter(is_available=True, category__is_active=True)

    if category_slug:
        queryset = queryset.filter(category__slug=category_slug)

    if query:
        cleaned_query = query.strip()
        if cleaned_query:
            variants = {
                cleaned_query,
                cleaned_query.lower(),
                cleaned_query.capitalize(),
                cleaned_query.upper(),
                cleaned_query.title(),
            }
            q_filter = Q()
            for v in variants:
                q_filter |= (
                    Q(title__icontains=v)
                    | Q(description__icontains=v)
                    | Q(allergens__icontains=v)
                    | Q(category__name__icontains=v)
                )
            queryset = queryset.filter(q_filter)

    if max_price is not None:
        queryset = queryset.filter(price__lte=Decimal(str(max_price)))

    if is_vegetarian is not None:
        queryset = queryset.filter(is_vegetarian=is_vegetarian)

    if is_spicy is not None:
        queryset = queryset.filter(is_spicy=is_spicy)

    if max_calories is not None:
        queryset = queryset.filter(calories__lte=max_calories)

    if exclude_allergens:
        for allergen in exclude_allergens:
            cleaned_allergen = allergen.strip()
            if cleaned_allergen:
                queryset = queryset.exclude(allergens__icontains=cleaned_allergen)

    return queryset.order_by('category__display_order', 'title')


def search_dishes_for_agent(
    *,
    query: str = '',
    category_slug: str | None = None,
    max_price: Decimal | float | None = None,
    is_vegetarian: bool | None = None,
    max_calories: int | None = None,
) -> list[dict[str, Any]]:
    dishes = filter_dishes(
        category_slug=category_slug,
        query=query,
        max_price=max_price,
        is_vegetarian=is_vegetarian,
        max_calories=max_calories,
        only_available=True,
    ).order_by('id')

    results: list[dict[str, Any]] = []
    for dish in dishes[:10]:
        options = [
            {'name': opt.name, 'price_delta': float(opt.price_delta)}
            for opt in dish.options.all()
        ]
        results.append(
            {
                'id': dish.id,
                'title': dish.title,
                'category': dish.category.name,
                'price': float(dish.price),
                'weight_grams': dish.weight_grams,
                'calories': dish.calories,
                'allergens': dish.allergens,
                'is_vegetarian': dish.is_vegetarian,
                'is_spicy': dish.is_spicy,
                'options': options,
            }
        )
    return results
