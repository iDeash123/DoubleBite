from decimal import Decimal, InvalidOperation
from typing import Any

from config.partials import render_partial_or_full
from django.conf import settings
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.generic import TemplateView

from .models import Category, Dish
from .selectors import (
    SORT_OPTIONS,
    filter_dishes,
    get_active_categories,
    get_available_dish_by_slug,
    get_home_page_context,
)

DISHES_PER_PAGE = 12


class HomeView(TemplateView):
    template_name = 'home/index.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        home_ctx = get_home_page_context()
        for key in (
            'meets_free_delivery',
            'free_delivery_remaining',
            'free_delivery_percent',
            'free_delivery_threshold',
        ):
            home_ctx.pop(key, None)
        context.update(home_ctx)
        return context


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

    try:
        page_number = int(request.GET.get('page', 1))
        page_number = max(page_number, 1)
    except (ValueError, TypeError):
        page_number = 1

    dishes_qs = filter_dishes(
        category_slug=category_slug,
        query=query,
        max_price=max_price,
        is_vegetarian=is_vegetarian,
        is_spicy=is_spicy,
        only_available=True,
        ordering=ordering,
        prefetch_options=True,
    )
    is_htmx_partial = (
        request.headers.get('HX-Request', '').lower() in ('true', '1')
        and request.headers.get('HX-History-Restore-Request', '').lower() not in ('true', '1')
    )
    categories = [] if is_htmx_partial else get_active_categories()

    paginator = Paginator(dishes_qs, DISHES_PER_PAGE)
    page_obj = paginator.get_page(page_number)

    context = {
        'dishes': page_obj,
        'page_obj': page_obj,
        'paginator': paginator,
        'categories': categories,
        'selected_category': category_slug,
        'search_query': query or '',
        'is_vegetarian': is_vegetarian,
        'is_spicy': is_spicy,
        'max_price': max_price_raw if max_price is not None else '',
        'sort': ordering,
        'total_dishes': paginator.count,
    }

    template_name = 'menu/catalog.html#dish-grid' if is_htmx_partial else 'menu/catalog.html'
    response = render_partial_or_full(request, template_name, context)
    response['Vary'] = 'HX-Request'
    return response


def menu_slug_dispatch_view(request: HttpRequest, dish_slug: str) -> HttpResponse:
    category = Category.objects.filter(slug=dish_slug, is_active=True).first()
    if category:
        return catalog_view(request, category_slug=dish_slug)

    dish = get_available_dish_by_slug(dish_slug=dish_slug)
    if dish:
        return dish_detail_view(request, dish_slug=dish_slug, dish=dish)

    raise Http404('Страву або категорію не знайдено або вона тимчасово недоступна.')


def _resolve_dish_schema_image(request: HttpRequest, dish: Dish) -> str:
    raw_url = dish.image.url if dish.image else ''
    if raw_url and not raw_url.lstrip().lower().startswith('data:'):
        return request.build_absolute_uri(raw_url)
    try:
        favicon_url = static('favicon/favicon.png')
    except ValueError:
        static_url = getattr(settings, 'STATIC_URL', '/static/') or '/static/'
        if not static_url.startswith('/'):
            static_url = f'/{static_url}'
        favicon_url = f'{static_url.rstrip("/")}/favicon/favicon.png'
    return request.build_absolute_uri(favicon_url)


def dish_detail_view(
    request: HttpRequest,
    dish_slug: str,
    category_slug: str | None = None,
    dish: Dish | None = None,
) -> HttpResponse:
    if dish is None:
        dish = get_available_dish_by_slug(dish_slug=dish_slug, category_slug=category_slug)
    if not dish:
        raise Http404('Страву не знайдено або вона тимчасово недоступна')

    dish_json_ld = {
        '@context': 'https://schema.org',
        '@type': 'Product',
        'name': dish.title,
        'description': dish.description or dish.title,
        'url': request.build_absolute_uri(dish.get_absolute_url()),
        'image': _resolve_dish_schema_image(request, dish),
        'offers': {
            '@type': 'Offer',
            'price': f'{dish.price:.2f}',
            'priceCurrency': 'UAH',
            'availability': (
                'https://schema.org/InStock'
                if dish.is_available
                else 'https://schema.org/OutOfStock'
            ),
        },
        'nutrition': {
            '@type': 'NutritionInformation',
            'calories': f'{dish.calories} cal',
        },
    }

    context = {
        'dish': dish,
        'options': list(dish.options.all()),
        'dish_json_ld': dish_json_ld,
        'breadcrumbs': [
            {'title': 'Головна', 'url': '/'},
            {'title': 'Меню', 'url': '/menu/'},
            {'title': dish.category.name, 'url': f'/menu/?category={dish.category.slug}'},
            {'title': dish.title, 'url': ''},
        ],
    }
    return render(request, 'menu/dish_detail.html', context)


def robots_txt_view(request: HttpRequest) -> HttpResponse:
    sitemap_url = request.build_absolute_uri('/sitemap.xml')
    admin_prefix = settings.DJANGO_ADMIN_URL.strip('/')
    content = (
        'User-agent: *\n'
        'Allow: /\n'
        f'Disallow: /{admin_prefix}/\n'
        'Disallow: /accounts/\n'
        'Disallow: /orders/\n'
        'Disallow: /support/\n'
        'Disallow: /support/chat/\n'
        f'Sitemap: {sitemap_url}\n'
    )
    return HttpResponse(content, content_type='text/plain')

