import json
from decimal import Decimal
from typing import Any

from django.core.exceptions import SynchronousOnlyOperation
from django.db import DatabaseError

DEFAULT_HERO_DISHES: list[dict[str, Any]] = [
    {
        'id': 1,
        'title': 'Truffle Wagyu Burger',
        'subtitle': 'Бріош · Мармурова яловичина · Трюфельний айолі',
        'tag': '01 · HAUTE BURGER',
        'badge': 'ФЛАГМАНСЬКА СТРАВА',
        'description': 'Фірмовий бургер з витриманою мармуровою яловичиною сухого визрівання, соусом з чорного трюфеля, карамелізованою цибулею та вершковим чедером у свіжоспеченій булочці бріош.',
        'taste_notes': 'Насичений умамі, земляні ноти чорного трюфеля, соковита мармурова текстура',
        'price': '485',
        'weight_grams': 380,
        'calories': 680,
        'prep_time': '15 хв',
        'image': 'https://images.unsplash.com/photo-1568901346375-23c9450c58cd?auto=format&fit=crop&w=1000&q=85',
        'bg_watermark': 'HAUTE BURGER',
    },
    {
        'id': 2,
        'title': 'Quattro Formaggi al Tartufo',
        'subtitle': 'Горгонзола · Fior di Latte · Пармезан 24 міс. · Трюфельний мед',
        'tag': '02 · NEAPOLITAN PIZZA',
        'badge': '48H FERMENTATION',
        'description': 'Біла неаполітанська піца на повітряному тісті 48-годинної холодної ферментації з чотирма витриманими сирами та акацієвим медом з білим трюфелем.',
        'taste_notes': 'Вершково-пікантний контраст, медово-трюфельний посмак, хрусткий бортик',
        'price': '420',
        'weight_grams': 460,
        'calories': 820,
        'prep_time': '12 хв',
        'image': 'https://images.unsplash.com/photo-1513104890138-7c749659a591?auto=format&fit=crop&w=1000&q=85',
        'bg_watermark': 'NEAPOLITAN PIZZA',
    },
    {
        'id': 3,
        'title': 'Wild Salmon & Avocado Bowl',
        'subtitle': 'Норвезький лосось · Авокадо Hass · Кіноа · Едамаме',
        'tag': '03 · FRESH BOWL',
        'badge': 'HEALTHY FLOW',
        'description': 'Збалансований боул з охолодженим норвезьким лососем, стиглим кремовим авокадо Hass, органічною кіноа, бобами едамаме та цитрусово-кунжутною заправкою.',
        'taste_notes': 'Свіжий океанічний смак, оксамитова ніжність авокадо, хрусткі нутрієнти',
        'price': '390',
        'weight_grams': 340,
        'calories': 420,
        'prep_time': '10 хв',
        'image': 'https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=1000&q=85',
        'bg_watermark': 'FRESH BOWL',
    },
    {
        'id': 4,
        'title': 'Basque Burnt Cheesecake',
        'subtitle': 'Карамелізована скоринка · Ваніль Bourbon · Тануча серцевина',
        'tag': '04 · CRAFT PASTRY',
        'badge': 'CHEF SIGNATURE',
        'description': 'Легендарний сан-себастьянський чізкейк з випаленою до гірко-солодкого карамельного відтінку поверхнею та оксамитовою рідкою текстурою в середині.',
        'taste_notes': 'Карамелізована скоринка, тануча вершкова ніжність, бурбонська мадагаскарська ваніль',
        'price': '220',
        'weight_grams': 190,
        'calories': 340,
        'prep_time': '5 хв',
        'image': 'https://images.unsplash.com/photo-1533134242443-d4fd215305ad?auto=format&fit=crop&w=1000&q=85',
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
        'description': 'Фірмова серія бургерів з добірної витриманої мармурової яловичини, авторськими трюфельними соусами та хрусткими булочками бріош із власної пекарні.',
        'dishes': [
            {
                'id': 1,
                'title': 'Truffle Wagyu Burger',
                'description': 'Котлета з мармурової яловичини, трюфельний крем-сир, карамелізована цибуля у бріоші.',
                'price': '485',
                'weight_grams': 380,
                'calories': 680,
                'image': 'https://images.unsplash.com/photo-1568901346375-23c9450c58cd?auto=format&fit=crop&w=600&q=80',
            },
            {
                'id': 11,
                'title': 'Double Smoky Cheddar',
                'description': 'Подвійна яловича котлета, копчений сир чедер, бекон, соус BBQ з бурбоном.',
                'price': '440',
                'weight_grams': 410,
                'calories': 790,
                'image': 'https://images.unsplash.com/photo-1550547660-d9450f859349?auto=format&fit=crop&w=600&q=80',
            },
            {
                'id': 12,
                'title': 'Crispy Truffle Chicken',
                'description': 'Хрустке куряче філе панко, трюфельний айолі, мариновані огірки та айсберг.',
                'price': '380',
                'weight_grams': 360,
                'calories': 610,
                'image': 'https://images.unsplash.com/photo-1625813506062-0aeb1d7a094b?auto=format&fit=crop&w=600&q=80',
            },
        ],
    },
    {
        'index': 2,
        'tag': '02 · NEAPOLITAN PIZZA',
        'slug': 'pizza',
        'title': 'Неаполітанська Піца',
        'subtitle': '48-годинна ферментація тіста · Томати San Marzano',
        'description': 'Традиційна піца за неаполітанським стандартом: італійське борошно Caputo, випікання за 450°C, солодкі томати пелаті та молода моцарела.',
        'dishes': [
            {
                'id': 2,
                'title': 'Quattro Formaggi al Tartufo',
                'description': 'Горгонзола, fior di latte, фонтіна, пармезан 24 міс., білий трюфельний мед.',
                'price': '420',
                'weight_grams': 460,
                'calories': 820,
                'image': 'https://images.unsplash.com/photo-1513104890138-7c749659a591?auto=format&fit=crop&w=600&q=80',
            },
            {
                'id': 21,
                'title': 'Margherita D.O.P.',
                'description': 'Томати San Marzano, моцарела fior di latte, свіжий зелений базилік, оливкова олія.',
                'price': '290',
                'weight_grams': 420,
                'calories': 720,
                'image': 'https://images.unsplash.com/photo-1574071318508-1cdbab80d002?auto=format&fit=crop&w=600&q=80',
            },
            {
                'id': 22,
                'title': 'Pepperoni Hot Honey',
                'description': 'Пікантна салямі піканте, плавлена моцарела, томатний соус, пластівці чилі та гострий мед.',
                'price': '350',
                'weight_grams': 440,
                'calories': 880,
                'image': 'https://images.unsplash.com/photo-1628840042765-356cda07504e?auto=format&fit=crop&w=600&q=80',
            },
        ],
    },
    {
        'index': 3,
        'tag': '03 · FRESH BOWLS',
        'slug': 'bowls',
        'title': 'Свіжі Боули & Салати',
        'subtitle': 'Дикий лосось · Стигле авокадо Hass · Органічна кіноа',
        'description': 'Легкі та нутрієнтно насичені композиції зі свіжою рибою, суперфудами, бобами едамаме та авторськими цитрусовими дресингами.',
        'dishes': [
            {
                'id': 3,
                'title': 'Wild Salmon & Avocado Bowl',
                'description': 'Норвезький лосось, авокадо Hass, кіноа, боби едамаме, огірок, кунжутний дресинг.',
                'price': '390',
                'weight_grams': 340,
                'calories': 420,
                'image': 'https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=600&q=80',
            },
            {
                'id': 31,
                'title': 'Burrata Con Pomodorini',
                'description': 'Вершкова фермерська бурата, печені чері, базиліковий песто, хрустка фокача.',
                'price': '360',
                'weight_grams': 310,
                'calories': 380,
                'image': 'https://images.unsplash.com/photo-1592417817098-8f3d69109853?auto=format&fit=crop&w=600&q=80',
            },
            {
                'id': 32,
                'title': 'Tuna Tataki Poke Bowl',
                'description': 'Опалений тунець у кунжуті, чука, маринований імбир, соус понзу, рис Akita.',
                'price': '410',
                'weight_grams': 350,
                'calories': 440,
                'image': 'https://images.unsplash.com/photo-1579871494447-9811cf80d66c?auto=format&fit=crop&w=600&q=80',
            },
        ],
    },
    {
        'index': 4,
        'tag': '04 · CRAFT PASTRY & DRINKS',
        'slug': 'desserts',
        'title': 'Десерти & Спешелті Напої',
        'subtitle': 'Баскський чізкейк · Церемоніальна матча з Кіото',
        'description': 'Витончений фінал гастрономічного досвіду: класичний сан-себастьянський чізкейк та автентична японська матча найвищого ґатунку.',
        'dishes': [
            {
                'id': 4,
                'title': 'Basque Burnt Cheesecake',
                'description': 'Карамелізований баскський чізкейк з ніжною танучою серцевиною та мадагаскарською ваніллю.',
                'price': '220',
                'weight_grams': 190,
                'calories': 340,
                'image': 'https://images.unsplash.com/photo-1533134242443-d4fd215305ad?auto=format&fit=crop&w=600&q=80',
            },
            {
                'id': 41,
                'title': 'Ceremonial Matcha Latte',
                'description': 'Церемоніальний японський зелений чай з Кіото на рослинному вівсяному молоці.',
                'price': '160',
                'weight_grams': 250,
                'calories': 120,
                'image': 'https://images.unsplash.com/photo-1536256263959-770b48d82b0a?auto=format&fit=crop&w=600&q=80',
            },
            {
                'id': 42,
                'title': 'Almond Croissant & Espresso',
                'description': 'Листковий круасан з мигдалевим франжипаном та подвійний шот арабіки Ефіопія.',
                'price': '180',
                'weight_grams': 180,
                'calories': 390,
                'image': 'https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=600&q=80',
            },
        ],
    },
    {
        'index': 5,
        'tag': '05 · CHEF\'S TASTING SETS',
        'slug': 'sets',
        'title': 'Дегустаційні Сети',
        'subtitle': 'Авторські гастро-сети для компаній та подій',
        'description': 'Вивірені сети страв від бренд-шефа, створені для бездоганного поєднання смаків та святкової атмосфери вдома чи в офісі.',
        'dishes': [
            {
                'id': 51,
                'title': 'Double Bite Signature Duo',
                'description': 'Truffle Wagyu Burger, картопляні діпи з трюфельним айолі та крафтовий лимонад.',
                'price': '560',
                'weight_grams': 650,
                'calories': 980,
                'image': 'https://images.unsplash.com/photo-1568901346375-23c9450c58cd?auto=format&fit=crop&w=600&q=80',
            },
            {
                'id': 52,
                'title': 'Italian Evening Tasting Set',
                'description': 'Піца Quattro Formaggi, бурата con pomodorini, фокача та два десерти на вибір.',
                'price': '890',
                'weight_grams': 990,
                'calories': 1520,
                'image': 'https://images.unsplash.com/photo-1513104890138-7c749659a591?auto=format&fit=crop&w=600&q=80',
            },
            {
                'id': 53,
                'title': 'Healthy Balance Gourmet Set',
                'description': 'Wild Salmon Bowl, зелений смузі суперфуд та чіа-пудинг на мигдалевому молоці.',
                'price': '520',
                'weight_grams': 580,
                'calories': 620,
                'image': 'https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=600&q=80',
            },
        ],
    },
]


def get_home_page_context(total_amount: Decimal | None = None) -> dict[str, Any]:
    hero_dishes = [dict(d) for d in DEFAULT_HERO_DISHES]
    collections_data = [
        {
            'index': c['index'],
            'tag': c['tag'],
            'slug': c['slug'],
            'title': c['title'],
            'subtitle': c['subtitle'],
            'description': c['description'],
            'dishes': [dict(item) for item in c['dishes']],
        }
        for c in DEFAULT_COLLECTIONS_DATA
    ]
    featured_dishes = []
    categories = []
    try:
        from menu.models import Category, Dish
        db_dishes = list(Dish.objects.filter(is_available=True).select_related('category'))
        if db_dishes:
            featured_dishes = db_dishes[:8]
            for hero in hero_dishes:
                matched = None
                for dish in db_dishes:
                    if hero['title'].lower() in dish.title.lower() or dish.title.lower() in hero['title'].lower():
                        matched = dish
                        break
                if not matched:
                    for dish in db_dishes:
                        if any(k in dish.title.lower() for k in hero['title'].lower().split() if len(k) > 4):
                            matched = dish
                            break
                if matched:
                    hero['id'] = matched.id
                    hero['slug'] = matched.slug
                    hero['price'] = str(int(matched.price) if matched.price % 1 == 0 else matched.price)
                    if matched.image:
                        hero['image'] = matched.image.url or str(matched.image)
                    if matched.weight_grams:
                        hero['weight_grams'] = matched.weight_grams
                    if matched.calories:
                        hero['calories'] = matched.calories
            for i, hero in enumerate(hero_dishes):
                if not any(d.id == hero.get('id') for d in db_dishes):
                    hero['id'] = db_dishes[i % len(db_dishes)].id

            for col in collections_data:
                col_slug = col['slug']
                matching_db_dishes = [
                    d for d in db_dishes
                    if (d.category and col_slug in d.category.slug.lower())
                    or col['title'].lower() in d.title.lower()
                ]
                if matching_db_dishes:
                    for idx, item in enumerate(col['dishes']):
                        if idx < len(matching_db_dishes):
                            real_d = matching_db_dishes[idx]
                            item['id'] = real_d.id
                            item['title'] = real_d.title
                            item['description'] = real_d.description or item['description']
                            item['price'] = str(int(real_d.price) if real_d.price % 1 == 0 else real_d.price)
                            item['weight_grams'] = real_d.weight_grams or item['weight_grams']
                            item['calories'] = real_d.calories or item['calories']
                            if real_d.image:
                                item['image'] = real_d.image.url or str(real_d.image)
                else:
                    for idx, item in enumerate(col['dishes']):
                        item['id'] = db_dishes[idx % len(db_dishes)].id

        categories = list(Category.objects.filter(is_active=True).order_by('display_order', 'name'))
    except (SynchronousOnlyOperation, DatabaseError, Exception):
        pass

    free_delivery_threshold = Decimal('500.00')
    current_total = total_amount if total_amount is not None else Decimal('0.00')
    meets_free_delivery = current_total >= free_delivery_threshold
    free_delivery_remaining = max(Decimal('0.00'), free_delivery_threshold - current_total)

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
    }
