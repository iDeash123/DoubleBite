from decimal import Decimal, InvalidOperation

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render

from config.partials import render_partial_or_full
from .models import Category
from .selectors import (
    SORT_OPTIONS,
    filter_dishes,
    get_active_categories,
    get_available_dish_by_slug,
)


def catalog_view(request: HttpRequest, category_slug: str | None = None) -> HttpResponse:
    category_slug = category_slug or request.GET.get('category', '').strip() or None
    query = (request.GET.get('q') or request.GET.get('search', '')).strip() or None

    max_price_raw = request.GET.get('max_price', '').strip()
    max_price: Decimal | None = None
    if max_price_raw:
        try:
            max_price = Decimal(max_price_raw)
            if max_price < 0:
                max_price = None
        except InvalidOperation:
            max_price = None

    is_veg_raw = request.GET.get('is_vegetarian', '').strip().lower()
    is_vegetarian: bool | None = None
    if is_veg_raw in ('true', '1', 'on'):
        is_vegetarian = True
    elif is_veg_raw in ('false', '0'):
        is_vegetarian = False

    is_spicy_raw = request.GET.get('is_spicy', '').strip().lower()
    is_spicy: bool | None = None
    if is_spicy_raw in ('true', '1', 'on'):
        is_spicy = True
    elif is_spicy_raw in ('false', '0'):
        is_spicy = False

    ordering_raw = request.GET.get('sort', '').strip()
    ordering = ordering_raw if ordering_raw in SORT_OPTIONS else 'default'

    dishes = filter_dishes(
        category_slug=category_slug,
        query=query,
        max_price=max_price,
        is_vegetarian=is_vegetarian,
        is_spicy=is_spicy,
        only_available=True,
        ordering=ordering,
    )
    categories = get_active_categories()

    context = {
        'dishes': dishes,
        'categories': categories,
        'selected_category': category_slug,
        'search_query': query or '',
        'is_vegetarian': is_vegetarian,
        'is_spicy': is_spicy,
        'max_price': max_price_raw if max_price is not None else '',
        'sort': ordering,
    }

    if request.headers.get('HX-Request'):
        return render_partial_or_full(request, 'menu/catalog.html#dish-grid', context)

    return render_partial_or_full(request, 'menu/catalog.html', context)


def menu_slug_dispatch_view(request: HttpRequest, dish_slug: str) -> HttpResponse:
    dish = get_available_dish_by_slug(dish_slug=dish_slug)
    if dish:
        return dish_detail_view(request, dish_slug=dish_slug)

    category = Category.objects.filter(slug=dish_slug, is_active=True).first()
    if category:
        return catalog_view(request, category_slug=dish_slug)

    raise Http404('Страву або категорію не знайдено або вона тимчасово недоступна.')


def dish_detail_view(
    request: HttpRequest,
    dish_slug: str,
    category_slug: str | None = None,
) -> HttpResponse:
    dish = get_available_dish_by_slug(dish_slug=dish_slug, category_slug=category_slug)
    if not dish:
        raise Http404('Страву не знайдено або вона тимчасово недоступна')

    context = {
        'dish': dish,
        'options': list(dish.options.all()),
        'breadcrumbs': [
            {'title': 'Головна', 'url': '/'},
            {'title': 'Меню', 'url': '/menu/'},
            {'title': dish.category.name, 'url': f'/menu/?category={dish.category.slug}'},
            {'title': dish.title, 'url': ''},
        ],
    }
    return render(request, 'menu/dish_detail.html', context)
