import json
import logging
from decimal import Decimal
from typing import Any

from config.constants import DEFAULT_FREE_DELIVERY_THRESHOLD
from django.conf import settings
from django.core.exceptions import SynchronousOnlyOperation
from django.db import DatabaseError, connection
from django.db.models import Q, QuerySet
from django.templatetags.static import static

from .models import Category, Dish

logger = logging.getLogger('menu')


def _showcase_static_url(filename: str) -> str:
    relative_path = f'images/showcase/{filename}'
    try:
        return static(relative_path)
    except ValueError:
        static_url = getattr(settings, 'STATIC_URL', '/static/') or '/static/'
        if not static_url.startswith('/'):
            static_url = f'/{static_url}'
        return f'{static_url.rstrip("/")}/{relative_path}'


DEFAULT_HERO_DISHES: list[dict[str, Any]] = [
    {
        'id': None,
        'title': 'Truffle Wagyu Burger',
        'subtitle': 'Бріош · Мармурова яловичина · Трюфельний айолі',
        'tag': '01 · HAUTE BURGER',
        'badge': 'ФЛАГМАНСЬКА СТРАВА',
        'description': (
            'Фірмовий бургер з витриманою мармуровою яловичиною сухого визрівання, '
            'соусом з чорного трюфеля, карамелізованою цибулею та вершковим чедером '
            'у свіжоспеченій булочці бріош.'
        ),
        'taste_notes': (
            'Насичений умамі, земляні ноти чорного трюфеля, соковита мармурова текстура'
        ),
        'price': '485',
        'weight_grams': 380,
        'calories': 680,
        'prep_time': '15 хв',
        'image': _showcase_static_url('photo-1568901346375-23c9450c58cd.webp'),
        'bg_watermark': 'HAUTE BURGER',
    },
    {
        'id': None,
        'title': 'Quattro Formaggi al Tartufo',
        'subtitle': 'Горгонзола · Fior di Latte · Пармезан 24 міс. · Трюфельний мед',
        'tag': '02 · NEAPOLITAN PIZZA',
        'badge': '48H FERMENTATION',
        'description': (
            'Біла неаполітанська піца на повітряному тісті 48-годинної холодної '
            'ферментації з чотирма витриманими сирами та акацієвим медом з білим трюфелем.'
        ),
        'taste_notes': (
            'Вершково-пікантний контраст, медово-трюфельний посмак, хрусткий бортик'
        ),
        'price': '420',
        'weight_grams': 460,
        'calories': 820,
        'prep_time': '12 хв',
        'image': _showcase_static_url('photo-1513104890138-7c749659a591.webp'),
        'bg_watermark': 'NEAPOLITAN PIZZA',
    },
    {
        'id': None,
        'title': 'Wild Salmon & Avocado Bowl',
        'subtitle': 'Норвезький лосось · Авокадо Hass · Кіноа · Едамаме',
        'tag': '03 · FRESH BOWL',
        'badge': 'HEALTHY FLOW',
        'description': (
            'Збалансований боул з охолодженим норвезьким лососем, стиглим кремовим '
            'авокадо Hass, органічною кіноа, бобами едамаме та цитрусово-кунжутною заправкою.'
        ),
        'taste_notes': (
            'Свіжий океанічний смак, оксамитова ніжність авокадо, хрусткі нутрієнти'
        ),
        'price': '390',
        'weight_grams': 340,
        'calories': 420,
        'prep_time': '10 хв',
        'image': _showcase_static_url('photo-1546069901-ba9599a7e63c.webp'),
        'bg_watermark': 'FRESH BOWL',
    },
    {
        'id': None,
        'title': 'Basque Burnt Cheesecake',
        'subtitle': 'Карамелізована скоринка · Ваніль Bourbon · Тануча серцевина',
        'tag': '04 · CRAFT PASTRY',
        'badge': 'CHEF SIGNATURE',
        'description': (
            'Легендарний сан-себастьянський чізкейк з випаленою до гірко-солодкого '
            'карамельного відтінку поверхнею та оксамитовою рідкою текстурою в середині.'
        ),
        'taste_notes': (
            'Карамелізована скоринка, тануча вершкова ніжність, '
            'бурбонська мадагаскарська ваніль'
        ),
        'price': '220',
        'weight_grams': 190,
        'calories': 340,
        'prep_time': '5 хв',
        'image': _showcase_static_url('photo-1533134242443-d4fd215305ad.webp'),
        'bg_watermark': 'BURNT CHEESECAKE',
    },
]

DEFAULT_COLLECTIONS_DATA: list[dict[str, Any]] = [
    {
        'index': 1,
        'tag': '01 · HAUTE BURGERS',
        'slug': 'burgers',
        'title': 'Авторські Бургери',
        'subtitle': 'Булочки бріош власної випічки · Мармурова яловичина',
        'description': (
            'Фірмова серія бургерів з добірної витриманої мармурової яловичини, '
            'авторськими трюфельними соусами та хрусткими булочками бріош із власної пекарні.'
        ),
        'dishes': [
            {
                'id': None,
                'title': 'Truffle Wagyu Burger',
                'description': (
                    'Котлета з мармурової яловичини, трюфельний крем-сир, '
                    'карамелізована цибуля у бріоші.'
                ),
                'price': '485',
                'weight_grams': 380,
                'calories': 680,
                'image': _showcase_static_url('photo-1568901346375-23c9450c58cd.webp'),
            },
            {
                'id': None,
                'title': 'Double Smoky Cheddar',
                'description': (
                    'Подвійна яловича котлета, копчений сир чедер, бекон, '
                    'соус BBQ з бурбоном.'
                ),
                'price': '440',
                'weight_grams': 410,
                'calories': 790,
                'image': _showcase_static_url('photo-1550547660-d9450f859349.webp'),
            },
            {
                'id': None,
                'title': 'Crispy Truffle Chicken',
                'description': (
                    'Хрустке куряче філе панко, трюфельний айолі, '
                    'мариновані огірки та айсберг.'
                ),
                'price': '380',
                'weight_grams': 360,
                'calories': 610,
                'image': _showcase_static_url('photo-1625813506062-0aeb1d7a094b.webp'),
            },
        ],
    },
    {
        'index': 2,
        'tag': '02 · NEAPOLITAN PIZZA',
        'slug': 'pizza',
        'title': 'Неаполітанська Піца',
        'subtitle': '48-годинна ферментація тіста · Томати San Marzano',
        'description': (
            'Традиційна піца за неаполітанським стандартом: італійське борошно Caputo, '
            'випікання за 450°C, солодкі томати пелаті та молода моцарела.'
        ),
        'dishes': [
            {
                'id': None,
                'title': 'Quattro Formaggi al Tartufo',
                'description': (
                    'Горгонзола, fior di latte, фонтіна, пармезан 24 міс., '
                    'білий трюфельний мед.'
                ),
                'price': '420',
                'weight_grams': 460,
                'calories': 820,
                'image': _showcase_static_url('photo-1513104890138-7c749659a591.webp'),
            },
            {
                'id': None,
                'title': 'Margherita D.O.P.',
                'description': (
                    'Томати San Marzano, моцарела fior di latte, свіжий зелений '
                    'базилік, оливкова олія.'
                ),
                'price': '290',
                'weight_grams': 420,
                'calories': 720,
                'image': _showcase_static_url('photo-1574071318508-1cdbab80d002.webp'),
            },
            {
                'id': None,
                'title': 'Pepperoni Hot Honey',
                'description': (
                    'Пікантна салямі піканте, плавлена моцарела, томатний соус, '
                    'пластівці чилі та гострий мед.'
                ),
                'price': '350',
                'weight_grams': 440,
                'calories': 880,
                'image': _showcase_static_url('photo-1628840042765-356cda07504e.webp'),
            },
        ],
    },
    {
        'index': 3,
        'tag': '03 · FRESH BOWLS',
        'slug': 'bowls-salads',
        'title': 'Свіжі Боули & Салати',
        'subtitle': 'Дикий лосось · Стигле авокадо Hass · Органічна кіноа',
        'description': (
            'Легкі та нутрієнтно насичені композиції зі свіжою рибою, '
            'суперфудами, бобами едамаме та авторськими цитрусовими дресингами.'
        ),
        'dishes': [
            {
                'id': None,
                'title': 'Wild Salmon & Avocado Bowl',
                'description': (
                    'Норвезький лосось, авокадо Hass, кіноа, боби едамаме, '
                    'огірок, кунжутний дресинг.'
                ),
                'price': '390',
                'weight_grams': 340,
                'calories': 420,
                'image': _showcase_static_url('photo-1546069901-ba9599a7e63c.webp'),
            },
            {
                'id': None,
                'title': 'Burrata Con Pomodorini',
                'description': (
                    'Вершкова фермерська бурата, печені чері, базиліковий песто, '
                    'хрустка фокача.'
                ),
                'price': '360',
                'weight_grams': 310,
                'calories': 380,
                'image': _showcase_static_url('photo-1592417817098-8f3d69109853.webp'),
            },
            {
                'id': None,
                'title': 'Tuna Tataki Poke Bowl',
                'description': (
                    'Опалений тунець у кунжуті, чука, маринований імбир, '
                    'соус понзу, рис Akita.'
                ),
                'price': '410',
                'weight_grams': 350,
                'calories': 440,
                'image': _showcase_static_url('photo-1579871494447-9811cf80d66c.webp'),
            },
        ],
    },
    {
        'index': 4,
        'tag': '04 · CRAFT PASTRY & DRINKS',
        'slug': 'desserts',
        'title': 'Десерти & Спешелті Напої',
        'subtitle': 'Баскський чізкейк · Церемоніальна матча з Кіото',
        'description': (
            'Витончений фінал гастрономічного досвіду: класичний сан-себастьянський '
            'чізкейк та автентична японська матча найвищого ґатунку.'
        ),
        'dishes': [
            {
                'id': None,
                'title': 'Basque Burnt Cheesecake',
                'description': (
                    'Карамелізований баскський чізкейк з ніжною танучою серцевиною '
                    'та мадагаскарською ваніллю.'
                ),
                'price': '220',
                'weight_grams': 190,
                'calories': 340,
                'image': _showcase_static_url('photo-1533134242443-d4fd215305ad.webp'),
            },
            {
                'id': None,
                'title': 'Ceremonial Matcha Latte',
                'description': (
                    'Церемоніальний японський зелений чай з Кіото на рослинному '
                    'вівсяному молоці.'
                ),
                'price': '160',
                'weight_grams': 250,
                'calories': 120,
                'image': _showcase_static_url('photo-1536256263959-770b48d82b0a.webp'),
            },
            {
                'id': None,
                'title': 'Almond Croissant & Espresso',
                'description': (
                    'Листковий круасан з мигдалевим франжипаном та подвійний шот '
                    'арабіки Ефіопія.'
                ),
                'price': '180',
                'weight_grams': 180,
                'calories': 390,
                'image': _showcase_static_url('photo-1509440159596-0249088772ff.webp'),
            },
        ],
    },
    {
        'index': 5,
        'tag': "05 · CHEF'S TASTING SETS",
        'slug': 'sets',
        'title': 'Дегустаційні Сети',
        'subtitle': 'Авторські гастро-сети для компаній та подій',
        'description': (
            'Вивірені сети страв від бренд-шефа, створені для бездоганного '
            'поєднання смаків та святкової атмосфери вдома чи в офісі.'
        ),
        'dishes': [
            {
                'id': None,
                'title': 'Double Bite Signature Duo',
                'description': (
                    'Truffle Wagyu Burger, картопляні діпи з трюфельним айолі '
                    'та крафтовий лимонад.'
                ),
                'price': '560',
                'weight_grams': 650,
                'calories': 980,
                'image': _showcase_static_url('photo-1568901346375-23c9450c58cd.webp'),
            },
            {
                'id': None,
                'title': 'Italian Evening Tasting Set',
                'description': (
                    'Піца Quattro Formaggi, бурата con pomodorini, фокача '
                    'та два десерти на вибір.'
                ),
                'price': '890',
                'weight_grams': 990,
                'calories': 1520,
                'image': _showcase_static_url('photo-1513104890138-7c749659a591.webp'),
            },
            {
                'id': None,
                'title': 'Healthy Balance Gourmet Set',
                'description': (
                    'Wild Salmon Bowl, зелений смузі суперфуд та чіа-пудинг '
                    'на мигдалевому молоці.'
                ),
                'price': '520',
                'weight_grams': 580,
                'calories': 620,
                'image': _showcase_static_url('photo-1546069901-ba9599a7e63c.webp'),
            },
        ],
    },
]


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


SORT_OPTIONS: dict[str, list[str]] = {
    'default':       ['category__display_order', 'title'],
    'price_asc':     ['price', 'title'],
    'price_desc':    ['-price', 'title'],
    'name_asc':      ['title'],
    'name_desc':     ['-title'],
    'calories_asc':  ['calories', 'title'],
    'calories_desc': ['-calories', 'title'],
}


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
    ordering: str | None = None,
    prefetch_options: bool = False,
) -> QuerySet[Dish]:
    queryset = Dish.objects.select_related('category')
    if prefetch_options:
        queryset = queryset.prefetch_related('options')

    if only_available:
        queryset = queryset.filter(is_available=True, category__is_active=True)

    if category_slug:
        queryset = queryset.filter(category__slug=category_slug)

    if query:
        cleaned_query = query.strip()
        if cleaned_query:
            if connection.vendor == 'sqlite' and not cleaned_query.isascii():
                variants = {
                    cleaned_query,
                    cleaned_query.lower(),
                    cleaned_query.capitalize(),
                    cleaned_query.upper(),
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
            else:
                queryset = queryset.filter(
                    Q(title__icontains=cleaned_query)
                    | Q(description__icontains=cleaned_query)
                    | Q(allergens__icontains=cleaned_query)
                    | Q(category__name__icontains=cleaned_query)
                )

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
            if not cleaned_allergen:
                continue
            if connection.vendor == 'sqlite' and not cleaned_allergen.isascii():
                variants = {
                    cleaned_allergen,
                    cleaned_allergen.lower(),
                    cleaned_allergen.capitalize(),
                    cleaned_allergen.upper(),
                }
                q_exclude = Q()
                for variant in variants:
                    q_exclude |= Q(allergens__icontains=variant)
                queryset = queryset.exclude(q_exclude)
            else:
                queryset = queryset.exclude(allergens__icontains=cleaned_allergen)

    order_fields = SORT_OPTIONS.get(ordering or '', SORT_OPTIONS['default'])
    return queryset.order_by(*order_fields)


def search_dishes_for_agent(
    *,
    query: str = '',
    category_slug: str | None = None,
    max_price: Decimal | float | None = None,
    is_vegetarian: bool | None = None,
    max_calories: int | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    dishes = list(
        filter_dishes(
            category_slug=category_slug,
            query=query,
            max_price=max_price,
            is_vegetarian=is_vegetarian,
            max_calories=max_calories,
            only_available=True,
            prefetch_options=True,
        ).order_by('id')[:limit]
    )

    if not dishes and query:
        try:
            from support.vector_search import search_dishes_semantic

            dishes = search_dishes_semantic(
                query=query,
                limit=limit,
                category_slug=category_slug,
                only_available=True,
                max_price=max_price,
                is_vegetarian=is_vegetarian,
                max_calories=max_calories,
            )
        except (
            ImportError,
            DatabaseError,
            SynchronousOnlyOperation,
            ValueError,
            TypeError,
            RuntimeError,
            OSError,
        ) as exc:
            logger.warning('Semantic dish search failed for query %r: %s', query, exc)

    results: list[dict[str, Any]] = []
    for dish in dishes[:limit]:
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


def _match_hero_dishes(hero_dishes: list[dict[str, Any]], db_dishes: list[Dish]) -> None:
    for hero in hero_dishes:
        matched = None
        hero_title_lower = hero['title'].lower()
        for dish in db_dishes:
            dish_title_lower = dish.title.lower()
            if hero_title_lower in dish_title_lower or dish_title_lower in hero_title_lower:
                matched = dish
                break
        if not matched:
            keywords = [k for k in hero_title_lower.split() if len(k) > 4]
            for dish in db_dishes:
                dish_title_lower = dish.title.lower()
                if any(k in dish_title_lower for k in keywords):
                    matched = dish
                    break
        if matched:
            hero['id'] = matched.id
            hero['slug'] = matched.slug
            hero['price'] = str(int(matched.price) if matched.price % 1 == 0 else matched.price)
            if matched.image and matched.image.url:
                hero['image'] = matched.image.url
            if matched.weight_grams:
                hero['weight_grams'] = matched.weight_grams
            if matched.calories:
                hero['calories'] = matched.calories


def _match_collection_dishes(collections_data: list[dict[str, Any]], db_dishes: list[Dish]) -> None:
    for col in collections_data:
        col_slug = col['slug']
        matching_db_dishes = [
            d
            for d in db_dishes
            if (d.category and col_slug in d.category.slug.lower())
            or col['title'].lower() in d.title.lower()
        ]
        if not matching_db_dishes:
            continue
        for idx, item in enumerate(col['dishes']):
            if idx >= len(matching_db_dishes):
                break
            real_d = matching_db_dishes[idx]
            item['id'] = real_d.id
            item['title'] = real_d.title
            item['description'] = real_d.description or item['description']
            item['price'] = str(int(real_d.price) if real_d.price % 1 == 0 else real_d.price)
            item['weight_grams'] = real_d.weight_grams or item['weight_grams']
            item['calories'] = real_d.calories or item['calories']
            if real_d.image and real_d.image.url:
                item['image'] = real_d.image.url


def get_home_page_context(total_amount: Decimal | None = None) -> dict[str, Any]:
    hero_dishes = [dict(d, id=None) for d in DEFAULT_HERO_DISHES]
    collections_data = [
        {
            'index': c['index'],
            'tag': c['tag'],
            'slug': c['slug'],
            'title': c['title'],
            'subtitle': c['subtitle'],
            'description': c['description'],
            'dishes': [dict(item, id=None) for item in c['dishes']],
        }
        for c in DEFAULT_COLLECTIONS_DATA
    ]
    featured_dishes: list[Dish] = []
    categories: list[Category] = []
    try:
        db_dishes = list(
            Dish.objects.filter(is_available=True, category__is_active=True).select_related('category')
        )
        if db_dishes:
            featured_dishes = db_dishes[:8]
            _match_hero_dishes(hero_dishes, db_dishes)
            _match_collection_dishes(collections_data, db_dishes)

        categories = list(Category.objects.filter(is_active=True).order_by('display_order', 'name'))
    except (SynchronousOnlyOperation, DatabaseError) as exc:
        logger.warning('Failed to load showcase data from database: %s', exc)

    free_delivery_threshold = getattr(
        settings, 'FREE_DELIVERY_THRESHOLD', DEFAULT_FREE_DELIVERY_THRESHOLD
    )
    threshold_dec = (
        free_delivery_threshold
        if isinstance(free_delivery_threshold, Decimal)
        else Decimal(str(free_delivery_threshold))
    )
    current_total = total_amount if total_amount is not None else Decimal('0.00')
    if not isinstance(current_total, Decimal):
        current_total = Decimal(str(current_total))

    if threshold_dec <= Decimal('0.00'):
        meets_free_delivery = True
        free_delivery_remaining = Decimal('0.00')
        free_delivery_percent = 100
    else:
        meets_free_delivery = current_total >= threshold_dec
        free_delivery_remaining = max(Decimal('0.00'), threshold_dec - current_total)
        if current_total >= threshold_dec:
            free_delivery_percent = 100
        elif current_total <= Decimal('0.00'):
            free_delivery_percent = 0
        else:
            free_delivery_percent = min(100, max(0, int((current_total * 100) // threshold_dec)))

    return {
        'hero_dishes': hero_dishes,
        'hero_dishes_json': json.dumps(hero_dishes, ensure_ascii=False),
        'collections_data': collections_data,
        'collections_json': json.dumps(collections_data, ensure_ascii=False),
        'featured_dishes': featured_dishes,
        'categories': categories,
        'free_delivery_threshold': free_delivery_threshold,
        'free_delivery_remaining': free_delivery_remaining,
        'meets_free_delivery': meets_free_delivery,
        'free_delivery_percent': free_delivery_percent,
    }

