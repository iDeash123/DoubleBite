import io
import os
import shutil
import urllib.request
from decimal import Decimal
from pathlib import Path

from accounts.models import DeliveryAddress, Role, User
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from orders.models import (
    Cart,
    CartItem,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from PIL import Image, ImageDraw
from support.models import ChatMessage, ChatSession, FAQKnowledge, SupportTicket
from support.vector_search import generate_mock_embedding

from menu.models import Category, Dish, DishOption

CATEGORIES_DATA = [
    {
        'name': 'Піца',
        'slug': 'pizza',
        'icon': 'pizza',
        'display_order': 1,
        'dishes': [
            {
                'title': 'Піца Маргарита D.O.P.',
                'slug': 'pizza-margherita-dop',
                'description': 'Класична неаполітанська піца на 48-годинному ферментованому тісті, соус із солодких томатів San Marzano, справжня моцарела Fior di Latte, свіжий зелений базилік та краплі першокласної оливкової олії Extra Virgin.',
                'price': Decimal('245.00'),
                'weight_grams': 430,
                'calories': 780,
                'allergens': 'Глютен, лактоза',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1574071318508-1cdbab80d002?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Подвійна моцарела Fior di Latte', Decimal('45.00')),
                    ('Томати чері конфі', Decimal('30.00')),
                    ('Трюфельна олія', Decimal('35.00')),
                ],
            },
            {
                'title': 'Піца Пепероні Діавола',
                'slug': 'pizza-pepperoni-diavola',
                'description': 'Хрусткий бортик, гостра неаполітанська ковбаса салямі піканте, плавлена моцарела, соус San Marzano, пластівці перцю чилі та солодкий медовий відтінок Hot Honey.',
                'price': Decimal('295.00'),
                'weight_grams': 450,
                'calories': 940,
                'allergens': 'Глютен, лактоза',
                'is_vegetarian': False,
                'is_spicy': True,
                'image': 'https://images.unsplash.com/photo-1628840042765-356cda07504e?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра халапеньйо', Decimal('25.00')),
                    ('Подвійна порція пепероні', Decimal('55.00')),
                    ('Соус Hot Honey дип', Decimal('30.00')),
                ],
            },
            {
                'title': 'Піца Кватро Формаджі',
                'slug': 'pizza-quattro-formaggi',
                'description': 'Вишукана біла піца на вершковій основі з балансом чотирьох сирів: молода моцарела, витриманий пармезан Reggiano 24 міс, пікантна горгонзола Piccante та ніжний едам з волоськими горіхами.',
                'price': Decimal('325.00'),
                'weight_grams': 440,
                'calories': 980,
                'allergens': 'Глютен, лактоза, горіхи',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1513104890138-7c749659a591?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Трюфельний мед', Decimal('40.00')),
                    ('Карамелізована груша', Decimal('35.00')),
                    ('Витриманий пармезан тертий', Decimal('40.00')),
                ],
            },
            {
                'title': 'Піца Трюфельна з Прошуто',
                'slug': 'pizza-tartufo-prosciutto',
                'description': 'Делікатесне поєднання пармського прошуто Котто, чорного трюфельного крему, лісових печериць, вершкової страчатели та свіжого листя руколи.',
                'price': Decimal('375.00'),
                'weight_grams': 480,
                'calories': 890,
                'allergens': 'Глютен, лактоза',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1565299624946-b28f40a0ae38?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Свіжа страчателла +50г', Decimal('60.00')),
                    ('Екстра скибочки прошуто', Decimal('65.00')),
                ],
            },
            {
                'title': 'Піца Капрічоза Класика',
                'slug': 'pizza-capricciosa-classic',
                'description': 'Традиційна рецептура: ніжне прошуто Котто, мариновані серця артишоків, соковиті печериці, маслини Каламата та сир моцарела під томатним соусом.',
                'price': Decimal('310.00'),
                'weight_grams': 490,
                'calories': 860,
                'allergens': 'Глютен, лактоза',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1593560708920-61dd98c46a4e?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Маслини Каламата додатково', Decimal('25.00')),
                    ('Артишоки гриль', Decimal('35.00')),
                ],
            },
            {
                'title': 'Піца Мортаделла з Фісташками',
                'slug': 'pizza-mortadella-pistacchio',
                'description': 'Болонська мортаделла найвищого гатунку, серцевина кремової бурати, дроблена сицилійська фісташка та фісташковий песто на легкому пухкому тісті.',
                'price': Decimal('385.00'),
                'weight_grams': 510,
                'calories': 1020,
                'allergens': 'Глютен, лактоза, горіхи',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1604382354936-07c5d9983bd3?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Ціла свіжа бурата (120г)', Decimal('85.00')),
                    ('Дроблена фісташка екстра', Decimal('35.00')),
                ],
            },
            {
                'title': 'Піца Фрутті ді Маре (Морепродукти)',
                'slug': 'pizza-frutti-di-mare',
                'description': 'Тигрові креветки, ніжні бейбі-кальмари та мідії у білому вині, часниковий соус, томати конкасе, каперси та свіжа петрушка.',
                'price': Decimal('390.00'),
                'weight_grams': 470,
                'calories': 720,
                'allergens': 'Глютен, лактоза, молюски, ракоподібні',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1571407970349-bc81e7e96d47?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра тигрові креветки 3 шт', Decimal('75.00')),
                    ('Лимонний соус айолі', Decimal('30.00')),
                ],
            },
            {
                'title': 'Піца Карбонара Романа',
                'slug': 'pizza-carbonara-romana',
                'description': 'Автентичний смак Риму: хрусткий італійський гуанчале, соус на основі яєчних жовтків та сиру Пекоріно Романо, свіжозмелений чорний перець Tellicherry.',
                'price': Decimal('295.00'),
                'weight_grams': 460,
                'calories': 990,
                'allergens': 'Глютен, лактоза, яйця',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1588315029754-2dd089d39a1a?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Хрусткий гуанчале порція', Decimal('45.00')),
                    ('Сир Пекоріно екстра', Decimal('35.00')),
                ],
            },
            {
                'title': 'Піца BBQ М\'ясний Карнавал',
                'slug': 'pizza-bbq-meat-carnival',
                'description': 'Соковитий рваний ростбіф, баварські ковбаски, куряче філе су-від, копчений бекон, соус BBQ з бурбоном та червона маринована цибуля.',
                'price': Decimal('340.00'),
                'weight_grams': 530,
                'calories': 1100,
                'allergens': 'Глютен, лактоза, гірчиця',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1594007654729-407eedc4be65?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Фірмовий соус BBQ з бурбоном', Decimal('25.00')),
                    ('Перчики халапеньйо', Decimal('25.00')),
                ],
            },
            {
                'title': 'Піца Веганська з Артишоками',
                'slug': 'pizza-vegan-artichoke',
                'description': 'Томатний соус San Marzano, мариновані серця артишоків, солодкий перець раміро, шпинат, в\'ялені чері, кеш\'ю-пармезан та свіжий базилік.',
                'price': Decimal('280.00'),
                'weight_grams': 430,
                'calories': 610,
                'allergens': 'Глютен, горіхи',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1534308983496-4fabb1a015ee?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Кеш\'ю-пармезан додатково', Decimal('35.00')),
                    ('Авокадо слайси', Decimal('45.00')),
                ],
            },
            {
                'title': 'Піца Буррата та В\'ялені Томати',
                'slug': 'pizza-burrata-sun-dried-tomatoes',
                'description': 'Ціла вершкова буррата 120г у центрі гарячої піци, ароматний домашній песто з кедровими горішками, томати конфі, рукола та бальзамічний крем з Модени.',
                'price': Decimal('365.00'),
                'weight_grams': 520,
                'calories': 890,
                'allergens': 'Глютен, лактоза, горіхи',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1585238342024-78d387f4a707?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Бальзамічний крем екстра', Decimal('20.00')),
                    ('Прошуто ді Парма слайси', Decimal('60.00')),
                ],
            },
            {
                'title': 'Піца Грибна з Пекоріно та Чебрецем',
                'slug': 'pizza-funghi-pecorino-thyme',
                'description': 'Суміш білих карпатських грибів, гливи та печериць, пасерованих з часником і свіжим чебрецем, вершковий соус, сир Таледжо та стружка Пекоріно.',
                'price': Decimal('315.00'),
                'weight_grams': 460,
                'calories': 810,
                'allergens': 'Глютен, лактоза',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1541745537411-b8046dc6d66c?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Білі гриби екстра порція', Decimal('55.00')),
                    ('Трюфельне масло', Decimal('35.00')),
                ],
            },
            {
                'title': 'Піца Карамелізований Ананас & Прошуто',
                'slug': 'pizza-gourmet-hawaiian',
                'description': 'Гастрономічний погляд на гавайську піцу: солодкий ананас, карамелізований з тростинним цукром і ромом, копчена качина грудка, моцарела та мікрогрін.',
                'price': Decimal('330.00'),
                'weight_grams': 470,
                'calories': 840,
                'allergens': 'Глютен, лактоза',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1565299585323-38d6b0865b47?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Карамелізований ананас порція', Decimal('30.00')),
                    ('Гострий перець халапеньйо', Decimal('25.00')),
                ],
            },
            {
                'title': 'Піца Кальцоне з Рикотою та Шпинатом',
                'slug': 'pizza-calzone-ricotta-spinach',
                'description': 'Закрита традиційна піца, начинена фермерською рикотою, молодою моцарелою, свіжим шпинатом, часниковим маслом та подана з теплим соусом маринара.',
                'price': Decimal('275.00'),
                'weight_grams': 480,
                'calories': 760,
                'allergens': 'Глютен, лактоза',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1528137871618-79d2761e3fd5?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Додати прошуто всередину', Decimal('50.00')),
                    ('Екстра соус маринара', Decimal('25.00')),
                ],
            },
            {
                'title': 'Піца Вогняна Салямі з Ндуєю',
                'slug': 'pizza-fiery-nduja-salami',
                'description': 'Калабрійська пастоподібна ковбаса ндуя, гостра салямі Spianata Calabrese, маринований гострий перець халапеньйо, сир моцарела та соус на томатах пелаті.',
                'price': Decimal('320.00'),
                'weight_grams': 460,
                'calories': 960,
                'allergens': 'Глютен, лактоза',
                'is_vegetarian': False,
                'is_spicy': True,
                'image': 'https://images.unsplash.com/photo-1506354666786-959d6d497f1a?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Калабрійська ндуя екстра', Decimal('45.00')),
                    ('Охолоджуючий соус ранч', Decimal('25.00')),
                ],
            },
        ],
    },
    {
        'name': 'Суші та Роли',
        'slug': 'sushi',
        'icon': 'sushi',
        'display_order': 2,
        'dishes': [
            {
                'title': 'Рол Філадельфія з лососем',
                'slug': 'roll-philadelphia-salmon',
                'description': 'Преміальний охолоджений норвезький лосось, ніжний крем-сир Philadelphia, стигле авокадо Hass, хрусткий свіжий огірок та рис Akita Komachi.',
                'price': Decimal('340.00'),
                'weight_grams': 290,
                'calories': 520,
                'allergens': 'Риба, лактоза',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1579871494447-9811cf80d66c?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Подвійна шапка лосося', Decimal('65.00')),
                    ('Ікра тобіко помаранчева', Decimal('35.00')),
                ],
            },
            {
                'title': 'Рол Каліфорнія з крабом та тобіко',
                'slug': 'roll-california-crab-tobiko',
                'description': 'М\'ясо сніжного краба у японському соусі, стиглий авокадо, огірок, обваляний у хрусткій ікрі летючої риби тобіко найвищого гатунку.',
                'price': Decimal('290.00'),
                'weight_grams': 260,
                'calories': 430,
                'allergens': 'Ракоподібні, риба',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1611143669185-af224c5e3252?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Справжній камчатський краб', Decimal('95.00')),
                    ('Спайсі майонез японський', Decimal('25.00')),
                ],
            },
            {
                'title': 'Рол Зелений Дракон з вугром',
                'slug': 'roll-green-dragon-eel',
                'description': 'Копчений тихоокеанський вугор унагі, крем-сир, огірок, огорнутий тонкими пелюстками свіжого авокадо, политий соусом унагі та посипаний обсмаженим кунжутом.',
                'price': Decimal('380.00'),
                'weight_grams': 300,
                'calories': 560,
                'allergens': 'Риба, лактоза, кунжут, соя',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1563245372-f21724e3856d?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра соус унагі', Decimal('20.00')),
                    ('Ікра тобіко чорна', Decimal('35.00')),
                ],
            },
            {
                'title': 'Рол Золотий Дракон Преміум',
                'slug': 'roll-golden-dragon-premium',
                'description': 'Цілий шар ніжного копченого вугра зовні, всередині — тигрова креветка темпура, ніжний сир, огірок та краплі іскристого соусу унагі з золотими нотками.',
                'price': Decimal('420.00'),
                'weight_grams': 310,
                'calories': 590,
                'allergens': 'Риба, ракоподібні, лактоза, соя, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1617196034796-73dfa7b1fd56?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Харчове золото 24K декор', Decimal('50.00')),
                    ('Подвійний вугор', Decimal('80.00')),
                ],
            },
            {
                'title': 'Рол Філадельфія De Luxe з тунцем',
                'slug': 'roll-philadelphia-tuna-deluxe',
                'description': 'Яскравий жовтоперий тунець Yellowfin, вершковий сир, манговий тартар, свіжий огірок та легкий цитрусовий соус понзу.',
                'price': Decimal('360.00'),
                'weight_grams': 285,
                'calories': 470,
                'allergens': 'Риба, лактоза, соя',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1553621042-f6e147245754?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Трюфельний понзу соус', Decimal('35.00')),
                    ('Мікрогрін дайкон', Decimal('20.00')),
                ],
            },
            {
                'title': 'Рол Спайсі Лосось Aburi',
                'slug': 'roll-spicy-salmon-aburi',
                'description': 'Обпалений пальником лосось з димним ароматом, гострий соус кімчі, крем-сир, хрусткий огірок, зелена цибуля та хрусткі темпура-пластівці.',
                'price': Decimal('335.00'),
                'weight_grams': 280,
                'calories': 530,
                'allergens': 'Риба, лактоза, кунжут, глютен',
                'is_vegetarian': False,
                'is_spicy': True,
                'image': 'https://images.unsplash.com/photo-1583623025817-d180a2221d0a?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра гострий соус Шрірача', Decimal('20.00')),
                    ('Пластівці темпура додатково', Decimal('15.00')),
                ],
            },
            {
                'title': 'Рол Темпура Ебі з тигровою креветкою',
                'slug': 'roll-tempura-ebi-shrimp',
                'description': 'Гаряча хрустка креветка в темпурі, вершковий сир, огірок, авокадо, соус спайсі-майо та солодкий теріякі.',
                'price': Decimal('310.00'),
                'weight_grams': 290,
                'calories': 540,
                'allergens': 'Ракоподібні, лактоза, глютен, яйця',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1615361200141-f45040f367be?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Соус солодкий чилі', Decimal('20.00')),
                    ('Подвійна креветка всередині', Decimal('55.00')),
                ],
            },
            {
                'title': 'Нігірі Асорті Шеф-Вибір (6 шт)',
                'slug': 'nigiri-assorti-chef-selection',
                'description': 'Сет авторських нігірі: 2 шт норвезький лосось, 2 шт тунець Yellowfin, 2 шт копчений вугор з соусом унагі та паростками кінзи.',
                'price': Decimal('390.00'),
                'weight_grams': 220,
                'calories': 380,
                'allergens': 'Риба, соя, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1617196034183-421b4917c92d?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Обпалювання нігірі пальником (Aburi)', Decimal('25.00')),
                    ('Васабі свіжонатертий корінь', Decimal('40.00')),
                ],
            },
            {
                'title': 'Рол Трюфельний Лосось з Авокадо',
                'slug': 'roll-truffle-salmon-avocado',
                'description': 'Норвезький лосось найвищої свіжості, авокадо, крем-сир, заправлений чорною трюфельною пастою та крихтою пармезану.',
                'price': Decimal('370.00'),
                'weight_grams': 285,
                'calories': 510,
                'allergens': 'Риба, лактоза',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1559410545-0bdcd187e0a6?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Трюфельний соус дип', Decimal('35.00')),
                    ('Тости з норі', Decimal('20.00')),
                ],
            },
            {
                'title': 'Рол Філадельфія в Смаженому Кунжуті',
                'slug': 'roll-philadelphia-roasted-sesame',
                'description': 'Класична начинка з лосося, вершкового сиру та огірка, зовні обсипана золотистим обсмаженим кунжутом з горіховим післясмаком.',
                'price': Decimal('285.00'),
                'weight_grams': 270,
                'calories': 480,
                'allergens': 'Риба, лактоза, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1617196035154-1e7e6e28b0db?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Горіховий соус гомадаре', Decimal('25.00')),
                    ('Додатковий лосось', Decimal('50.00')),
                ],
            },
            {
                'title': 'Гункани з Лососем та Червоною Ікрою (4 шт)',
                'slug': 'gunkan-salmon-red-caviar',
                'description': 'Кошики з хрусткого листа норі та преміального рису, наповнені тартаром з лосося у спайсі-соусі та увінчані натуральною червоною ікрою кети.',
                'price': Decimal('360.00'),
                'weight_grams': 180,
                'calories': 340,
                'allergens': 'Риба, соя, кунжут',
                'is_vegetarian': False,
                'is_spicy': True,
                'image': 'https://images.unsplash.com/photo-1623341214825-9f4f963727da?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Додаткова червона ікра', Decimal('60.00')),
                    ('М\'який соус понзу', Decimal('20.00')),
                ],
            },
            {
                'title': 'Рол Веган Авокадо, Чука & Горіховий соус',
                'slug': 'roll-vegan-avocado-chuka',
                'description': 'Свіжий водорість чука, спіле авокадо, японський омлет з тофу, болгарський перець, огірок та ніжний кунжутно-горіховий соус.',
                'price': Decimal('220.00'),
                'weight_grams': 260,
                'calories': 380,
                'allergens': 'Кунжут, горіхи, соя',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1579584425555-c3ce17fd4351?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Горіховий соус екстра', Decimal('25.00')),
                    ('Тофу спайсі кубики', Decimal('30.00')),
                ],
            },
            {
                'title': 'Рол Червоний Дракон з Вугром та Лососем',
                'slug': 'roll-red-dragon-salmon-eel',
                'description': 'Всередині копчений вугор та авокадо, зовні обгорнутий соковитим філе свіжого лосося, прикрашений ікрою тобіко та мікрозеленню.',
                'price': Decimal('395.00'),
                'weight_grams': 305,
                'calories': 570,
                'allergens': 'Риба, лактоза, соя, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1534482421-64566f976cfa?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Подвійна порція ікри тобіко', Decimal('35.00')),
                    ('Унагі соус', Decimal('20.00')),
                ],
            },
            {
                'title': 'Рол Запечений з Морським Гребінцем',
                'slug': 'roll-baked-sea-scallop',
                'description': 'Теплий запечений рол з ніжним морським гребінцем під сирною шапочкою з пармезану та японського майонезу Kewpie, политий соусом кабаякі.',
                'price': Decimal('370.00'),
                'weight_grams': 310,
                'calories': 610,
                'allergens': 'Молюски, лактоза, яйця, соя',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1582450871972-ab5ca641643d?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра запечена сирна шапка', Decimal('40.00')),
                    ('Спайсі краплі чилі', Decimal('15.00')),
                ],
            },
            {
                'title': 'Рол Філадельфія Гріль в Норі',
                'slug': 'roll-philadelphia-grill-nori',
                'description': 'Карамелізований тростинним цукром лосось Aburi, всередині ніжний сир креметте, спаржа на грилі, авокадо та соус теріякі.',
                'price': Decimal('350.00'),
                'weight_grams': 295,
                'calories': 520,
                'allergens': 'Риба, лактоза, соя',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1579871494447-9811cf80d66c?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Спаржа гриль порція', Decimal('35.00')),
                    ('Теріякі соус', Decimal('20.00')),
                ],
            },
        ],
    },
    {
        'name': 'Сети',
        'slug': 'sets',
        'icon': 'bento',
        'display_order': 3,
        'dishes': [
            {
                'title': 'Сет «Імператорський Токіо» (32 шт)',
                'slug': 'set-imperial-tokyo',
                'description': 'Грандіозний сет: Філадельфія з лососем, Золотий Дракон з вугром, Каліфорнія з крабом та Нігірі Асорті. Ідеально на 3-4 персони.',
                'price': Decimal('1190.00'),
                'weight_grams': 1100,
                'calories': 2100,
                'allergens': 'Риба, ракоподібні, лактоза, соя, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1617196034796-73dfa7b1fd56?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Додатковий імбир та васабі', Decimal('25.00')),
                    ('Пляшка соєвого соусу Kikkoman 150ml', Decimal('60.00')),
                ],
            },
            {
                'title': 'Сет «Філадельфія Mania» (24 шт)',
                'slug': 'set-philadelphia-mania',
                'description': 'Тріо легендарних Філадельфій: Класична з лососем, Філадельфія з тунцем De Luxe та Філадельфія у золотистому кунжуті з авокадо.',
                'price': Decimal('890.00'),
                'weight_grams': 850,
                'calories': 1580,
                'allergens': 'Риба, лактоза, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1579871494447-9811cf80d66c?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Подвійний лосось на класичну Філадельфію', Decimal('60.00')),
                    ('Соус солодкий чилі', Decimal('20.00')),
                ],
            },
            {
                'title': 'Сет «Дракони Тріо» (24 шт)',
                'slug': 'set-dragons-trio',
                'description': 'Зелений Дракон з вугром та авокадо, Золотий Дракон з тигровою креветкою та Червоний Дракон з лососем. Багатий смаковий профіль.',
                'price': Decimal('1080.00'),
                'weight_grams': 920,
                'calories': 1720,
                'allergens': 'Риба, ракоподібні, лактоза, соя, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1563245372-f21724e3856d?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра соус унагі 50г', Decimal('25.00')),
                    ('Ікра тобіко декор', Decimal('35.00')),
                ],
            },
            {
                'title': 'Сет «Double Bite Party Box» (40 шт)',
                'slug': 'set-double-bite-party-box',
                'description': 'Найбільший фірмовий сет для гучної компанії: 5 топових ролів включно з запеченими та темпура, плюс соуси та гарніри.',
                'price': Decimal('1450.00'),
                'weight_grams': 1450,
                'calories': 2750,
                'allergens': 'Риба, ракоподібні, лактоза, глютен, соя, кунжут',
                'is_vegetarian': False,
                'is_spicy': True,
                'image': 'https://images.unsplash.com/photo-1553621042-f6e147245754?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Набір соусів Асорті (4 види)', Decimal('70.00')),
                    ('Чука салат 150г додатково', Decimal('85.00')),
                ],
            },
            {
                'title': 'Сет «Преміум Нігірі & Сашімі» (18 шт)',
                'slug': 'set-premium-nigiri-sashimi',
                'description': 'Чистий автентичний смак риби: сашімі з дикого лосося, сашімі з тунця Yellowfin, нігірі з вугром, гребінцем та креветкою амаебі.',
                'price': Decimal('980.00'),
                'weight_grams': 550,
                'calories': 840,
                'allergens': 'Риба, ракоподібні, молюски, соя',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1617196034183-421b4917c92d?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Тертий свіжий корінь васабі Shizuoka', Decimal('55.00')),
                    ('Лимонний соус понзу', Decimal('25.00')),
                ],
            },
            {
                'title': 'Сет «Hot Tempura & Baked Mix» (24 шт)',
                'slug': 'set-hot-tempura-baked-mix',
                'description': 'Теплий та хрусткий сет: Рол Темпура Ебі, Запечений рол з гребінцем та Запечений спайсі-лосось під шапкою сиру чеддер.',
                'price': Decimal('890.00'),
                'weight_grams': 900,
                'calories': 1850,
                'allergens': 'Риба, ракоподібні, молюски, лактоза, глютен, яйця',
                'is_vegetarian': False,
                'is_spicy': True,
                'image': 'https://images.unsplash.com/photo-1615361200141-f45040f367be?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра гострий спайсі соус', Decimal('20.00')),
                    ('Соус кабаякі солодкий', Decimal('20.00')),
                ],
            },
            {
                'title': 'Сет «Романтичний Вечір на Двох» (20 шт)',
                'slug': 'set-romantic-evening-duo',
                'description': 'Вишукана комбінація: Рол з тунцем та полуничним бальзаміком, Філадельфія де Люкс та гункани з червоною ікрою кети.',
                'price': Decimal('790.00'),
                'weight_grams': 650,
                'calories': 1180,
                'allergens': 'Риба, лактоза, соя',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1611143669185-af224c5e3252?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Полуничний бальзамік екстра', Decimal('25.00')),
                    ('Шоколадні трюфелі комплімент', Decimal('50.00')),
                ],
            },
            {
                'title': 'Сет «Бургер Комбо Double Duo»',
                'slug': 'set-burger-combo-double-duo',
                'description': '2 фірмові бургери (Трюфельний Wagyu та Black Angus Cheddar), 2 великі порції картоплі фрі з трюфельним айолі та 2 крафтові лимонади.',
                'price': Decimal('780.00'),
                'weight_grams': 1250,
                'calories': 2400,
                'allergens': 'Глютен, лактоза, яйця, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1586190848861-99aa4a171e90?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Сирні кульки замість фрі', Decimal('45.00')),
                    ('Додатковий сир чеддер у бургери', Decimal('40.00')),
                ],
            },
            {
                'title': 'Сет «Family Pizza & Wings Box»',
                'slug': 'set-family-pizza-wings-box',
                'description': '2 великі піци (Маргарита D.O.P. та Пепероні Діавола), хрусткі курячі крильця в солодкому чилі 12 шт та соуси Блю Чіз і BBQ.',
                'price': Decimal('840.00'),
                'weight_grams': 1500,
                'calories': 2900,
                'allergens': 'Глютен, лактоза, селера',
                'is_vegetarian': False,
                'is_spicy': True,
                'image': 'https://images.unsplash.com/photo-1513104890138-7c749659a591?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра сирний бортик у піци', Decimal('80.00')),
                    ('Крильця BBQ замість чилі', Decimal('0.00')),
                ],
            },
            {
                'title': 'Сет «Великий Лососевий Кілограм» (32 шт)',
                'slug': 'set-salmon-kilogram-feast',
                'description': 'Майже 1.1 кг виключно страв зі свіжого лосося: Філадельфія класік, Спайсі Лосось Aburi, Рол Лосось-Манго та гункани з тартаром.',
                'price': Decimal('1150.00'),
                'weight_grams': 1080,
                'calories': 2020,
                'allergens': 'Риба, лактоза, соя, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1583623025817-d180a2221d0a?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Подвійна порція крем-сиру в усі роли', Decimal('65.00')),
                    ('Ікра тобіко топпінг', Decimal('40.00')),
                ],
            },
            {
                'title': 'Сет «Суші-вечірка XXL» (48 шт)',
                'slug': 'set-sushi-party-xxl',
                'description': 'Величезний бенкетний набір: 6 повних ролів з морепродуктами, лососем, тунцем, вугром та крабом. Для компанії з 5-6 гостей.',
                'price': Decimal('1850.00'),
                'weight_grams': 1750,
                'calories': 3400,
                'allergens': 'Риба, ракоподібні, лактоза, соя, кунжут, глютен',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1579584425555-c3ce17fd4351?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Преміальний набір паличок у подарунок', Decimal('0.00')),
                    ('Додатковий сет соусів (6 шт)', Decimal('95.00')),
                ],
            },
            {
                'title': 'Сет «Street Food Gourmet Feast»',
                'slug': 'set-street-food-gourmet-feast',
                'description': 'Трюфельний Wagyu Бургер, Crispy Chicken Supreme, порція смажених кальмарів у панко та кукурудза гриль з копченою паприкою.',
                'price': Decimal('750.00'),
                'weight_grams': 1100,
                'calories': 2250,
                'allergens': 'Глютен, лактоза, яйця, молюски',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1568901346375-23c9450c58cd?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Картопляні діпи з сирним соусом', Decimal('40.00')),
                    ('Екстра бекон у бургери', Decimal('45.00')),
                ],
            },
            {
                'title': 'Сет «Green Detox & Vegan» (24 шт)',
                'slug': 'set-green-detox-vegan',
                'description': 'Повністю рослинний сет: Рол Веган Авокадо & Чука, Рол з хрустким тофу та манго, боул з кіноа та свіжий зелений смузі.',
                'price': Decimal('590.00'),
                'weight_grams': 800,
                'calories': 980,
                'allergens': 'Соя, кунжут, горіхи',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1540420773420-3366772f4999?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра порція горіхового соусу', Decimal('25.00')),
                    ('Насіння чіа додаток', Decimal('15.00')),
                ],
            },
            {
                'title': 'Сет «Італійське Тріо Піц»',
                'slug': 'set-italian-trio-pizza',
                'description': '3 класичні неаполітанські піци: Маргарита D.O.P., Кватро Формаджі та Трюфельна з прошуто. Для сімейного свята.',
                'price': Decimal('880.00'),
                'weight_grams': 1350,
                'calories': 2650,
                'allergens': 'Глютен, лактоза, горіхи',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1565299624946-b28f40a0ae38?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Крафтовий гострий оливковий соус 100мл', Decimal('35.00')),
                    ('Сирні бортики у всі 3 піци', Decimal('110.00')),
                ],
            },
            {
                'title': 'Сет «Шеф-Дегустація Double Bite»',
                'slug': 'set-chef-tasting-experience',
                'description': 'Ексклюзивна гастрономічна колекція: міні-слайдери з яловичиною Вагю (2 шт), авторський рол Aburi з гребінцем та трюфельний чізкейк.',
                'price': Decimal('920.00'),
                'weight_grams': 850,
                'calories': 1650,
                'allergens': 'Глютен, лактоза, яйця, риба, молюски',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1555396273-367ea4eb4db5?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Келих безалкогольного ігристого', Decimal('65.00')),
                    ('Фірмова листівка шефа', Decimal('0.00')),
                ],
            },
        ],
    },
    {
        'name': 'Бургери',
        'slug': 'burgers',
        'icon': 'burger',
        'display_order': 4,
        'dishes': [
            {
                'title': 'Трюфельний Wagyu Бургер',
                'slug': 'truffle-wagyu-burger',
                'description': 'Котлета з мармурової яловичини Wagyu 180г, бріош на вершковому маслі, сир грюйєр, чорний трюфельний айолі та карамелізована цибуля.',
                'price': Decimal('365.00'),
                'weight_grams': 380,
                'calories': 890,
                'allergens': 'Глютен, лактоза, яйця',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1568901346375-23c9450c58cd?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Додаткова котлета Wagyu', Decimal('120.00')),
                    ('Хрусткий бекон гриль', Decimal('40.00')),
                    ('Трюфельний айолі дип', Decimal('35.00')),
                ],
            },
            {
                'title': 'Double Bite Smash Burger',
                'slug': 'double-bite-smash-burger',
                'description': 'Дві ультрахрусткі котлети smash з добірного ангуса, подвійний плавлений вінтажний чеддер, мариновані корнішони та фірмовий Double соус.',
                'price': Decimal('295.00'),
                'weight_grams': 350,
                'calories': 920,
                'allergens': 'Глютен, лактоза, гірчиця, яйця',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1586190848861-99aa4a171e90?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Третя котлета Smash', Decimal('70.00')),
                    ('Гострий перець халапеньйо', Decimal('25.00')),
                ],
            },
            {
                'title': 'Black Angus Cheddar Burger',
                'slug': 'black-angus-cheddar-burger',
                'description': 'Соковита котлета з української фермерської яловичини Black Angus, солодкі томати, листя салату ромен, червона цибуля та сир чеддер.',
                'price': Decimal('270.00'),
                'weight_grams': 360,
                'calories': 810,
                'allergens': 'Глютен, лактоза, яйця',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1550547660-d9450f859349?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Смажене яйце з рідким жовтком', Decimal('30.00')),
                    ('Екстра сир чеддер', Decimal('35.00')),
                ],
            },
            {
                'title': 'Бургер Брі & Карамелізована Груша',
                'slug': 'burger-brie-caramelized-pear',
                'description': 'Витончений гастрономічний бургер: яловича котлета, ніжний сир Брі де Мо з білою пліснявою, томлена у вині груша, рукола та дижонська гірчиця.',
                'price': Decimal('325.00'),
                'weight_grams': 370,
                'calories': 830,
                'allergens': 'Глютен, лактоза, гірчиця',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1572802419224-296b0aeee0d9?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Подвійна порція сиру Брі', Decimal('55.00')),
                    ('Волоські горіхи крихта', Decimal('25.00')),
                ],
            },
            {
                'title': 'Crispy Chicken Supreme',
                'slug': 'crispy-chicken-supreme',
                'description': 'Хрустке куряче стегно у паніровці з японських сухарів панко, свіжий салат коулслоу з яблуком, маринований імбир та соус спайсі-майо на булочці бріош.',
                'price': Decimal('260.00'),
                'weight_grams': 370,
                'calories': 780,
                'allergens': 'Глютен, яйця, лактоза',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1551782450-a2132b4ba21d?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Соус солодкий корейський чилі', Decimal('20.00')),
                    ('Подвійне хрустке філе', Decimal('65.00')),
                ],
            },
            {
                'title': 'Бургер BBQ Bacon & Jalapeno',
                'slug': 'burger-bbq-bacon-jalapeno',
                'description': 'Димний яловичий бургер, копчений бекон подвійного обсмажування, гострі перчики халапеньйо, цибулеві кільця фрі та крафтовий BBQ соус.',
                'price': Decimal('290.00'),
                'weight_grams': 390,
                'calories': 960,
                'allergens': 'Глютен, лактоза, гірчиця',
                'is_vegetarian': False,
                'is_spicy': True,
                'image': 'https://images.unsplash.com/photo-1565299585323-38d6b0865b47?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра гострі халапеньйо', Decimal('25.00')),
                    ('Хрусткі цибулеві кільця всередину', Decimal('30.00')),
                ],
            },
            {
                'title': 'Бургер з Рваною Свининою Pulled Pork',
                'slug': 'burger-pulled-pork',
                'description': 'Ніжна свинина, томлена 12 годин у смокері, соус Hickory BBQ, маринована червона капуста, солодкі огірки реліш та гірчичний дресинг.',
                'price': Decimal('275.00'),
                'weight_grams': 410,
                'calories': 880,
                'allergens': 'Глютен, гірчиця, селера',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1521305916504-4a1121188589?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра порція рваного м\'яса', Decimal('60.00')),
                    ('Сирний соус чеддер', Decimal('30.00')),
                ],
            },
            {
                'title': 'Бургер Gorgonzola & Бекон Джем',
                'slug': 'burger-gorgonzola-bacon-jam',
                'description': 'Яловичина medium rare, інтенсивний блакитний сир Gorgonzola DOP, густий бекон-джем з кленовим сиропом та свіжий бейбі-шпинат.',
                'price': Decimal('335.00'),
                'weight_grams': 365,
                'calories': 910,
                'allergens': 'Глютен, лактоза',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1594212699903-ec8a3eca50f5?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Бекон-джем додаткова порція', Decimal('35.00')),
                    ('Блакитний сир екстра', Decimal('40.00')),
                ],
            },
            {
                'title': 'Фіш-бургер з Лососем та Тар-таром',
                'slug': 'fish-burger-salmon-tartar',
                'description': 'Стейк з філе норвезького лосося на грилі, домашній соус тар-тар з каперсами та кропом, хрусткий салат айсберг та свіжий огірок.',
                'price': Decimal('345.00'),
                'weight_grams': 350,
                'calories': 690,
                'allergens': 'Глютен, риба, яйця, лактоза',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1520072959219-c595dc870360?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Авокадо слайси всередину', Decimal('45.00')),
                    ('Лимонний соус айолі', Decimal('25.00')),
                ],
            },
            {
                'title': 'Бургер Beyond Meat 100% Vegan',
                'slug': 'burger-beyond-meat-vegan',
                'description': 'Соковита рослинна котлета Beyond Meat, безглютенова булочка або веган-бріош, рослинний сир чеддер, соус авокадо-майонез та свіжі овочі.',
                'price': Decimal('350.00'),
                'weight_grams': 360,
                'calories': 620,
                'allergens': 'Соя',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1585238342024-78d387f4a707?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Подвійна веганська котлета', Decimal('110.00')),
                    ('Веганський бекон', Decimal('40.00')),
                ],
            },
            {
                'title': 'Бургер з Креветками та Гуакамоле',
                'slug': 'burger-shrimp-guacamole',
                'description': 'Обсмажені на часниковому маслі тигрові креветки, свіже гуакамоле з кінзою та лаймом, соус манго-чилі на м\'якій чорній булочці з сезамом.',
                'price': Decimal('360.00'),
                'weight_grams': 340,
                'calories': 640,
                'allergens': 'Глютен, ракоподібні, лактоза, кунжут',
                'is_vegetarian': False,
                'is_spicy': True,
                'image': 'https://images.unsplash.com/photo-1525164286253-04e68b9d94c6?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Гуакамоле додаткова порція', Decimal('45.00')),
                    ('Екстра креветки (3 шт)', Decimal('70.00')),
                ],
            },
            {
                'title': 'Бургер Дор Блю з Трюфельною Пастою',
                'slug': 'burger-dor-blu-truffle',
                'description': 'Яловичина зернової відгодівлі, пікантний благородний сир Дор Блю, соус з білих трюфелів, рукола та свіжі хрусткі огірки.',
                'price': Decimal('320.00'),
                'weight_grams': 360,
                'calories': 870,
                'allergens': 'Глютен, лактоза',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1553979459-d2229ba7433b?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Трюфельна олія краплі', Decimal('30.00')),
                    ('Карамелізована цибуля', Decimal('25.00')),
                ],
            },
            {
                'title': 'Мехіко Бургер Чіпотле',
                'slug': 'mexico-burger-chipotle',
                'description': 'Яловича котлета, сир Pepper Jack, копчений соус чипотле, кукурудзяні начос для хрусту, свіже халапеньйо та сальса піко де гайо.',
                'price': Decimal('285.00'),
                'weight_grams': 380,
                'calories': 890,
                'allergens': 'Глютен, лактоза',
                'is_vegetarian': False,
                'is_spicy': True,
                'image': 'https://images.unsplash.com/photo-1561758033-d89a9ad46330?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Начос із сирним соусом гарнір', Decimal('40.00')),
                    ('Екстра соус чипотле', Decimal('25.00')),
                ],
            },
            {
                'title': 'Бургер Камамбер Фрі з Журавлиною',
                'slug': 'burger-camembert-cranberry',
                'description': 'Цілий круг сиру камамбер у хрусткій золотій паніровці замість котлети, кисло-солодкий журавлинний конфітюр, свіжа рукола та кедрові горішки.',
                'price': Decimal('295.00'),
                'weight_grams': 340,
                'calories': 790,
                'allergens': 'Глютен, лактоза, яйця, горіхи',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1547584370-2cc98b8b8dc8?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Журавлинний соус екстра', Decimal('25.00')),
                    ('Додати яловичу котлету', Decimal('80.00')),
                ],
            },
            {
                'title': 'Міні-бургери Слайдери Тріо',
                'slug': 'mini-sliders-trio',
                'description': 'Набір з трьох міні-бургерів: класичний чізбургер з яловичиною, хрусткий chicken слайдер та бургер з рваною свининою BBQ.',
                'price': Decimal('310.00'),
                'weight_grams': 390,
                'calories': 860,
                'allergens': 'Глютен, лактоза, яйця, гірчиця',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1568901346375-23c9450c58cd?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Порція картоплі діпперси', Decimal('35.00')),
                    ('Сирний соус', Decimal('25.00')),
                ],
            },
        ],
    },
    {
        'name': 'Боули та Салати',
        'slug': 'bowls-salads',
        'icon': 'salad',
        'display_order': 5,
        'dishes': [
            {
                'title': 'Боул з Лососем, Авокадо та Едамаме',
                'slug': 'bowl-salmon-avocado-edamame',
                'description': 'Охолоджений шотландський лосось, стиглий авокадо Hass, боби едамаме, чука, огірок, редис, бурий рис та фірмовий соус кунжутний понзу.',
                'price': Decimal('325.00'),
                'weight_grams': 380,
                'calories': 540,
                'allergens': 'Риба, соя, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Подвійна порція лосося', Decimal('70.00')),
                    ('Кіноа замість рису', Decimal('25.00')),
                ],
            },
            {
                'title': 'Салат Цезар з Куркою Су-від',
                'slug': 'salad-caesar-chicken-sous-vide',
                'description': 'Ніжне філе курки су-від, листя салату ромен, перепелині яйця, томати чері, слайси 24-місячного пармезану, часникові крутони та класичний анчоусний соус.',
                'price': Decimal('245.00'),
                'weight_grams': 320,
                'calories': 480,
                'allergens': 'Глютен, лактоза, яйця, риба',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1550304943-4f24f54ddde9?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Замінити курку на тигрові креветки', Decimal('65.00')),
                    ('Хрусткий бекон крихта', Decimal('30.00')),
                ],
            },
            {
                'title': 'Поке Боул з Жовтоперим Тунцем',
                'slug': 'poke-bowl-yellowfin-tuna',
                'description': 'Свіжий тунець Yellowfin, маринований у соусі понзу, свіже манго, вакаме, імбир, кіноа, зелена цибуля та обсмажений білий кунжут.',
                'price': Decimal('340.00'),
                'weight_grams': 370,
                'calories': 490,
                'allergens': 'Риба, соя, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1512621776951-a57141f2eefd?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Соус спайсі-майо', Decimal('20.00')),
                    ('Авокадо додатково', Decimal('40.00')),
                ],
            },
            {
                'title': 'Салат Грецький з Фермерською Фетою',
                'slug': 'salad-greek-farm-feta',
                'description': 'Справжній сир фета з овечого молока, бакинські томати, хрусткі огірки, солодкий перець, маслини Каламата, кримська цибуля та сушений орегано.',
                'price': Decimal('210.00'),
                'weight_grams': 340,
                'calories': 390,
                'allergens': 'Лактоза',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1540420773420-3366772f4999?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра фета 50г', Decimal('35.00')),
                    ('Каперси на гілочці', Decimal('25.00')),
                ],
            },
            {
                'title': 'Салат з Теплою Качиною Грудкою & Ягодами',
                'slug': 'salad-warm-duck-berries',
                'description': 'Ніжні слайси качиної грудки medium, мікс свіжого листя салату, карамелізована груша, свіжа малина та ожина під ожиново-бальзамічним дресингом.',
                'price': Decimal('295.00'),
                'weight_grams': 290,
                'calories': 460,
                'allergens': 'Горіхи',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1547496502-affa22d38842?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Дроблені волоські горіхи', Decimal('20.00')),
                    ('Сир Дор Блю крихта', Decimal('35.00')),
                ],
            },
            {
                'title': 'Боул з Тигровими Креветками та Манго',
                'slug': 'bowl-tiger-shrimp-mango',
                'description': 'Обсмажені на грилі тигрові креветки, стиглий манго, авокадо, огірок, паростки сої, рис жасмин та цитрусово-лаймовий дресинг.',
                'price': Decimal('330.00'),
                'weight_grams': 360,
                'calories': 470,
                'allergens': 'Ракоподібні, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1511690656952-34342bb7c2f2?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра креветки (3 шт)', Decimal('65.00')),
                    ('Соус солодкий чилі манго', Decimal('25.00')),
                ],
            },
            {
                'title': 'Салат з Буратою, Руколою та Песто',
                'slug': 'salad-burrata-arugula-pesto',
                'description': 'Ціла фермерська бурата з рідкою серединкою, солодкі чері confit, свіже листя руколи, соус песто з базиліку та підсмажені кедрові горішки.',
                'price': Decimal('285.00'),
                'weight_grams': 310,
                'calories': 520,
                'allergens': 'Лактоза, горіхи',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1607532941433-304659e8198a?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Прошуто ді Парма слайси', Decimal('55.00')),
                    ('Хрустка чіабата грінки', Decimal('20.00')),
                ],
            },
            {
                'title': 'Боул Веганський з Тофу та Кіноа',
                'slug': 'bowl-vegan-tofu-quinoa',
                'description': 'Органічний тофу в маринаді теріякі, триколірна кіноа, запечений батат, броколі на пару, гарбузове насіння та тахіні-дресинг.',
                'price': Decimal('240.00'),
                'weight_grams': 380,
                'calories': 430,
                'allergens': 'Соя, кунжут',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1543339308-43e59d6b73a6?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Запечений авокадо половинка', Decimal('45.00')),
                    ('Тахіні соус додатково', Decimal('25.00')),
                ],
            },
            {
                'title': 'Салат Капрезе з Моцарелою Буфало',
                'slug': 'salad-caprese-buffalo-mozzarella',
                'description': 'Оригінальна моцарела di Bufala Campana, добірні різнокольорові томати, свіжий зелений базилік, морська сіль пластівцями та оливкова олія DOP.',
                'price': Decimal('235.00'),
                'weight_grams': 280,
                'calories': 410,
                'allergens': 'Лактоза',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1529059997568-3d847b1154f0?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Бальзамічний крем Модена', Decimal('20.00')),
                    ('Песто соус порція', Decimal('25.00')),
                ],
            },
            {
                'title': 'Хрусткий Баклажан у Солодкому Чилі',
                'slug': 'salad-crispy-eggplant-sweet-chili',
                'description': 'Шматочки баклажана у надхрусткій паніровці, соковиті рожеві томати, листя кінзи, смажений кунжут та пікантний кисло-солодкий устричний соус.',
                'price': Decimal('225.00'),
                'weight_grams': 300,
                'calories': 380,
                'allergens': 'Соя, кунжут, молюски',
                'is_vegetarian': False,
                'is_spicy': True,
                'image': 'https://images.unsplash.com/photo-1505253758473-96b7015fcd40?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Сир страчателла шапка', Decimal('50.00')),
                    ('Екстра кінза свіжа', Decimal('15.00')),
                ],
            },
            {
                'title': 'Боул з Яловичиною Теріякі та Рис Жасмон',
                'slug': 'bowl-beef-teriyaki-jasmine-rice',
                'description': 'Тонкі слайси вирізки яловичини у глазурі теріякі, рис жасмин, стручкова квасоля, мариноване яйце рамьон, гриби шиїтаке та зелена цибуля.',
                'price': Decimal('310.00'),
                'weight_grams': 420,
                'calories': 620,
                'allergens': 'Соя, кунжут, яйця, глютен',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1623428187969-5da2dcea5ebf?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Яйце рамьон додатково', Decimal('25.00')),
                    ('Гострий соус кімчі', Decimal('20.00')),
                ],
            },
            {
                'title': 'Салат Нісуаз з Обпаленим Тунцем',
                'slug': 'salad-nicoise-seared-tuna',
                'description': 'Тунець rare з кунжутом, молода картопля бейбі, спаржева квасоля, анчоуси, перепелині яйця, оливки та французький гірчичний вінегрет.',
                'price': Decimal('315.00'),
                'weight_grams': 340,
                'calories': 470,
                'allergens': 'Риба, яйця, гірчиця',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1540420773420-3366772f4999?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Подвійна порція тунця', Decimal('75.00')),
                    ('Каперси гіганти', Decimal('25.00')),
                ],
            },
            {
                'title': 'Зелений Детокс Салат з Авокадо та Шпинатом',
                'slug': 'salad-green-detox-spinach-avocado',
                'description': 'Бейбі-шпинат, цукіні спагеті, авокадо, огірок, зелені яблука, гарбузове насіння, мікрогрін та освіжаючий лаймовий вінегрет.',
                'price': Decimal('220.00'),
                'weight_grams': 290,
                'calories': 310,
                'allergens': 'Кунжут',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1512621776951-a57141f2eefd?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Кіноа порція', Decimal('25.00')),
                    ('Сир фета крихта', Decimal('30.00')),
                ],
            },
            {
                'title': 'Поке Боул з Вугром та Кунжутом Унагі',
                'slug': 'poke-bowl-eel-unagi',
                'description': 'Копчений вугор у густому соусі унагі, свіжий огірок, чука, маринований імбир, авокадо, рис для суші та насіння кунжуту.',
                'price': Decimal('360.00'),
                'weight_grams': 370,
                'calories': 560,
                'allergens': 'Риба, соя, кунжут',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра вугор унагі', Decimal('70.00')),
                    ('Ікра тобіко', Decimal('35.00')),
                ],
            },
            {
                'title': 'Салат з Креветками, Грейпфрутом та Авокадо',
                'slug': 'salad-shrimp-grapefruit-avocado',
                'description': 'Обсмажені тигрові креветки, соковиті філе грейпфрута, авокадо, мікс салату лолло россо, кедрові горіхи та медово-гірчична заправка.',
                'price': Decimal('290.00'),
                'weight_grams': 300,
                'calories': 410,
                'allergens': 'Ракоподібні, горіхи, гірчиця',
                'is_vegetarian': False,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1551248429-40975aa4de74?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Додаткові креветки (3 шт)', Decimal('65.00')),
                    ('Соус медово-гірчичний', Decimal('20.00')),
                ],
            },
        ],
    },
    {
        'name': 'Десерти',
        'slug': 'desserts',
        'icon': 'cake',
        'display_order': 6,
        'dishes': [
            {
                'title': 'Баскський Чізкейк Сан-Себастьян',
                'slug': 'basque-burnt-cheesecake',
                'description': 'Знаменитий спалений баскський чізкейк з карамелізованою скоринкою зовні та кремовою ніжністю всередині, политий соусом із соленої карамелі.',
                'price': Decimal('195.00'),
                'weight_grams': 220,
                'calories': 580,
                'allergens': 'Лактоза, яйця',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Соус із соленої карамелі екстра', Decimal('25.00')),
                    ('Кулька ванільного джелато', Decimal('40.00')),
                ],
            },
            {
                'title': 'Тірамісу Класичний з Маскарпоне',
                'slug': 'tiramisu-classic-mascarpone',
                'description': 'Справжнє савоярді, просочене свіжозвареним еспресо та лікером Amaretto, крем на основі італійського маскарпоне та шар какао Brut.',
                'price': Decimal('185.00'),
                'weight_grams': 200,
                'calories': 490,
                'allergens': 'Глютен, лактоза, яйця, алкоголь',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1571877227200-a0d98ea607e9?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Додатковий шот еспресо', Decimal('20.00')),
                    ('Шоколадна стружка Belcolade', Decimal('20.00')),
                ],
            },
            {
                'title': 'Шоколадний Фондан з Ванільним Морозивом',
                'slug': 'chocolate-fondant-gelato',
                'description': 'Гарячий шоколадний кекс з бельгійського темного шоколаду 70% з рідким центром, подається з кулькою натурального мадагаскарського ванільного морозива.',
                'price': Decimal('210.00'),
                'weight_grams': 180,
                'calories': 610,
                'allergens': 'Глютен, лактоза, яйця',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1606313564200-e75d5e30476c?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Друга кулька морозива', Decimal('35.00')),
                    ('Ягідне пюре з малини', Decimal('25.00')),
                ],
            },
            {
                'title': 'Фісташковий Тарт зі Свіжою Малиною',
                'slug': 'pistachio-tart-raspberry',
                'description': 'Хрустке пісочне сабле, запечений фісташковий франжипан, легкий мус з натуральної сицилійської фісташки та свіжі ягоди малини.',
                'price': Decimal('225.00'),
                'weight_grams': 170,
                'calories': 480,
                'allergens': 'Глютен, лактоза, яйця, горіхи',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1587314168485-3236d6710814?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Свіжі ягоди малини екстра', Decimal('35.00')),
                    ('Фісташкова паста топпінг', Decimal('30.00')),
                ],
            },
            {
                'title': 'Японські Мочі Асорті (4 шт)',
                'slug': 'japanese-mochi-assorted',
                'description': 'Традиційний рисовий десерт з ніжним кремом: манго-маракуя, церемоніальна матча, бельгійський шоколад та полуниця-базилік.',
                'price': Decimal('240.00'),
                'weight_grams': 160,
                'calories': 360,
                'allergens': 'Лактоза',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1563729784474-d77dbb933a9e?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Додатковий мочі на вибір (1 шт)', Decimal('60.00')),
                ],
            },
            {
                'title': 'Десерт Анна Павлова з Манго та Маракуєю',
                'slug': 'pavlova-mango-passionfruit',
                'description': 'Хрустке зовні та тягуче як зефір всередині безе, вершковий крем маскарпоне та соус з тропічного манго та маракуї.',
                'price': Decimal('195.00'),
                'weight_grams': 190,
                'calories': 420,
                'allergens': 'Яйця, лактоза',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1551024709-8f23befc6f87?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра пюре маракуї', Decimal('25.00')),
                    ('Листя м\'яти', Decimal('10.00')),
                ],
            },
            {
                'title': 'Карамельний Медовик з Волоським Горіхом',
                'slug': 'caramel-honey-cake-walnut',
                'description': 'Тонкі запашні медові коржі на гречаному меду, крем з карамелізованого сметанкового крему та обсмажені волоські горіхи.',
                'price': Decimal('175.00'),
                'weight_grams': 210,
                'calories': 530,
                'allergens': 'Глютен, лактоза, яйця, горіхи',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1578985545062-69928b1d9587?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Медові стільники декор', Decimal('30.00')),
                ],
            },
            {
                'title': 'Торт Червоний Оксамит Red Velvet',
                'slug': 'cake-red-velvet',
                'description': 'Оксамитовий вологий бісквіт з тонкою ноткою какао, ніжний крем-чіз та прошарок із соковитого малинового конфі.',
                'price': Decimal('185.00'),
                'weight_grams': 200,
                'calories': 510,
                'allergens': 'Глютен, лактоза, яйця',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1586788680434-30d324b2d46f?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Малиновий соус', Decimal('20.00')),
                ],
            },
            {
                'title': 'Французький Круасан з Фісташковим Кремом',
                'slug': 'croissant-pistachio-cream',
                'description': 'Справжній листковий круасан на французькому маслі 84%, щедро наповнений шовковистим фісташковим кремом та посипаний фісташкою.',
                'price': Decimal('165.00'),
                'weight_grams': 180,
                'calories': 490,
                'allergens': 'Глютен, лактоза, яйця, горіхи',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1555507036-ab1f4038808a?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Підігріти перед відправкою', Decimal('0.00')),
                    ('Капучино у комбо', Decimal('55.00')),
                ],
            },
            {
                'title': 'Лавандовий Чізкейк з Чорницею',
                'slug': 'lavender-blueberry-cheesecake',
                'description': 'Холодний ніжний сирний мус з нотками натуральної французької лаванди, пісочна основа та соковите желе з дикої чорниці.',
                'price': Decimal('190.00'),
                'weight_grams': 200,
                'calories': 460,
                'allergens': 'Глютен, лактоза',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1533134242443-d4fd215305ad?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Чорничний джем додатково', Decimal('20.00')),
                ],
            },
            {
                'title': 'Еклер Трюфельний з Темним Шоколадом',
                'slug': 'eclair-truffle-dark-chocolate',
                'description': 'Заварне тісто шу, ніжний крем з темного шоколаду Callebaut, дзеркальна глазур та золоті пластівці.',
                'price': Decimal('145.00'),
                'weight_grams': 130,
                'calories': 380,
                'allergens': 'Глютен, лактоза, яйця',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1528975604071-b4dc52a2d18c?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Сет із 2-х еклерів', Decimal('120.00')),
                ],
            },
            {
                'title': 'Панна-кота з Лісовими Ягодами',
                'slug': 'panna-cotta-wild-berries',
                'description': 'Оксамитовий італійський десерт на фермерських вершках з натуральною бурбонською ваніллю та кулі з ожини, чорниці та малини.',
                'price': Decimal('160.00'),
                'weight_grams': 180,
                'calories': 340,
                'allergens': 'Лактоза',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1488477181946-6428a0291777?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Ягідний соус додатково', Decimal('20.00')),
                ],
            },
            {
                'title': 'Брауні з Солоною Карамеллю та Пеканом',
                'slug': 'brownie-salted-caramel-pecan',
                'description': 'Насичений вологий шоколадний брауні, шари тягучої домашньої соленої карамелі та обсмажений горіх пекан.',
                'price': Decimal('175.00'),
                'weight_grams': 170,
                'calories': 560,
                'allergens': 'Глютен, лактоза, яйця, горіхи',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1606313564200-e75d5e30476c?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Кулька пломбіру', Decimal('35.00')),
                ],
            },
            {
                'title': 'Чіа-пудинг на Кокосовому Молоці з Манго',
                'slug': 'chia-pudding-coconut-mango',
                'description': 'Легкий та корисний десерт без цукру: насіння чіа, замочене в органічному кокосовому молоці, та свіже пюре зі стиглого манго.',
                'price': Decimal('165.00'),
                'weight_grams': 210,
                'calories': 280,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1511690656952-34342bb7c2f2?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Кокосова стружка', Decimal('15.00')),
                    ('Ягоди годжі', Decimal('20.00')),
                ],
            },
            {
                'title': 'Лимонний Тарт з Меренгою Flambé',
                'slug': 'lemon-tart-meringue-flambe',
                'description': 'Французьке пісочне тісто, кисло-солодкий шовковий лимонний курд та висока шапка обпаленої італійської меренги.',
                'price': Decimal('180.00'),
                'weight_grams': 180,
                'calories': 440,
                'allergens': 'Глютен, лактоза, яйця',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1508737027454-e6454ef45afd?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('М\'ятний сироп', Decimal('15.00')),
                ],
            },
        ],
    },
    {
        'name': 'Напої',
        'slug': 'drinks',
        'icon': 'cup',
        'display_order': 7,
        'dishes': [
            {
                'title': 'Церемоніальна Матча Лате Uji',
                'slug': 'ceremonial-matcha-latte-uji',
                'description': 'Японська церемоніальна матча преміум-ґатунку з регіону Уджі, збита з ніжним вівсяним або кокосовим молоком.',
                'price': Decimal('135.00'),
                'weight_grams': 300,
                'calories': 140,
                'allergens': 'Вівсянка (за бажанням коров\'яче молоко)',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1536256263959-770b48d82b0a?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('На вівсяному молоці Oatly', Decimal('20.00')),
                    ('На мигдалевому молоці', Decimal('25.00')),
                    ('Шот ванільного сиропу', Decimal('15.00')),
                ],
            },
            {
                'title': 'Лимонад Юдзу & Імбир',
                'slug': 'lemonade-yuzu-ginger',
                'description': 'Освіжаючий авторський лимонад на основі японського цитрусу юдзу, свіжовичавленого соку імбиру, меду та газованої води.',
                'price': Decimal('120.00'),
                'weight_grams': 400,
                'calories': 95,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1513558161293-cdaf765ed2fd?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Менше льоду', Decimal('0.00')),
                    ('Без доданого цукру', Decimal('0.00')),
                ],
            },
            {
                'title': 'Холодна Кава Cold Brew Tonic',
                'slug': 'cold-brew-tonic',
                'description': 'Концентрат кави 18-годинної холодної екстракції на зернах Ethiopia Yirgacheffe, крафтовий індіан-тонік та часточка свіжого грейпфрута.',
                'price': Decimal('140.00'),
                'weight_grams': 350,
                'calories': 65,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1517701550927-30cf4ba1dba5?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Сироп червоний апельсин', Decimal('15.00')),
                ],
            },
            {
                'title': 'Лимонад Маракуя & Свіжий Базилік',
                'slug': 'lemonade-passionfruit-basil',
                'description': 'Яскравий тропічний напій з м\'якоттю маракуї, свіжим розтертим фіолетовим базиліком та свіжовичавленим соком лайма.',
                'price': Decimal('125.00'),
                'weight_grams': 400,
                'calories': 110,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1556881286-fc6915169721?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра порція маракуї', Decimal('25.00')),
                ],
            },
            {
                'title': 'Смузі Детокс Спіруліна, Ківі & Авокадо',
                'slug': 'smoothie-detox-spirulina-kiwi',
                'description': 'Густий енергетичний смузі: свіжий шпинат, зелене ківі, чверть авокадо, яблучний фреш та органічна блакитна спіруліна.',
                'price': Decimal('150.00'),
                'weight_grams': 350,
                'calories': 180,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1553530666-ba11a7da3888?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Протеїн рослинний порція', Decimal('35.00')),
                    ('Насіння чіа', Decimal('15.00')),
                ],
            },
            {
                'title': 'Свіжовичавлений Апельсиновий Фреш',
                'slug': 'fresh-orange-juice',
                'description': '100% натуральний сік з добірних солодких сицилійських апельсинів без додавання води та цукру.',
                'price': Decimal('130.00'),
                'weight_grams': 300,
                'calories': 120,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1613478223719-2ab802602423?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('З льодом', Decimal('0.00')),
                    ('Об\'єм 500 мл', Decimal('60.00')),
                ],
            },
            {
                'title': 'Крафтовий Чай Малина з Розмарином',
                'slug': 'craft-tea-raspberry-rosemary',
                'description': 'Зігріваючий ягідний чай з натуральної лісової малини, гілочки свіжого розмарину, меду та скибочки лимона.',
                'price': Decimal('115.00'),
                'weight_grams': 500,
                'calories': 80,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1576092768241-dec231879fc3?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Додати паличку кориці', Decimal('10.00')),
                    ('Імбир тертий додатково', Decimal('15.00')),
                ],
            },
            {
                'title': 'Фільтр-кава Ефіопія Спешелті',
                'slug': 'filter-coffee-ethiopia-specialty',
                'description': 'Кава світлого обсмажування, заварена методом фільтр Batch Brew. Дескриптори: бергамот, білий персик, жасмин та цитрусовий післясмак.',
                'price': Decimal('95.00'),
                'weight_grams': 280,
                'calories': 5,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1495474472287-4d71bcdd2085?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Рослинне молоко збоку 50мл', Decimal('15.00')),
                ],
            },
            {
                'title': 'Коктейль Virgin Mojito (Безалкогольний)',
                'slug': 'cocktail-virgin-mojito',
                'description': 'Класичний кубинський безалкогольний мікс: багато свіжої м\'яти, розтертої з соковитим лаймом, тростинний цукор та содова.',
                'price': Decimal('130.00'),
                'weight_grams': 380,
                'calories': 90,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1551024709-8f23befc6f87?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Полуничний Virgin Mojito', Decimal('20.00')),
                ],
            },
            {
                'title': 'Лимонад Лавандовий з Ожиною',
                'slug': 'lemonade-lavender-blackberry',
                'description': 'Авторський лимонад з пюре дикої ожини, квітковим настоєм прованської лаванди та свіжовичавленим соком лимона.',
                'price': Decimal('125.00'),
                'weight_grams': 400,
                'calories': 105,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1544145945-f90425340c7e?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Менше солодкого', Decimal('0.00')),
                ],
            },
            {
                'title': 'Чай Масала на Вівсяному Молоці',
                'slug': 'chai-masala-oat-milk',
                'description': 'Пряний індійський чай з кардамоном, імбиром, гвоздикою, корицею та чорним перцем, зварений на теплому вівсяному молоці.',
                'price': Decimal('135.00'),
                'weight_grams': 350,
                'calories': 160,
                'allergens': 'Вівсянка',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1544787219-7f47ccb76574?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Екстра імбир для гостроти', Decimal('15.00')),
                    ('Тростинний цукор', Decimal('0.00')),
                ],
            },
            {
                'title': 'Смузі Манго, Маракуя & Чіа',
                'slug': 'smoothie-mango-passionfruit-chia',
                'description': 'Соковитий тропічний смузі на основі манго Альфонсо, соку маракуї, банана, мигдалевого молока та насіння чіа.',
                'price': Decimal('155.00'),
                'weight_grams': 360,
                'calories': 210,
                'allergens': 'Горіхи',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1505252585461-04db1eb84625?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Без банана', Decimal('0.00')),
                    ('Кокосова вода основа', Decimal('20.00')),
                ],
            },
            {
                'title': 'Гранатовий Фреш Прямого Віджиму',
                'slug': 'fresh-pomegranate-juice',
                'description': 'Шляхетний терпко-солодкий сік з добірних азербайджанських гранатів. Джерело антиоксидантів та вітамінів.',
                'price': Decimal('180.00'),
                'weight_grams': 250,
                'calories': 130,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1556881286-fc6915169721?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Об\'єм 400 мл', Decimal('80.00')),
                ],
            },
            {
                'title': 'Каскара Тонік з Грейпфрутом',
                'slug': 'cascara-tonic-grapefruit',
                'description': 'Освіжаючий напій на основі висушеної м\'якоті кавових ягід (каскари), ігристого тоніка та свіжого соку рожевого грейпфрута.',
                'price': Decimal('130.00'),
                'weight_grams': 350,
                'calories': 75,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1514432324607-a09d9b4aefdd?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Листочки розмарину', Decimal('10.00')),
                ],
            },
            {
                'title': 'Коктейль Berry Spritz 0.0% (Безалкогольний)',
                'slug': 'berry-spritz-non-alcoholic',
                'description': 'Шляхетний італійський аперитив: безалкогольний червоний бітер, пюре з лісових ягід, тонік та ігристе безалкогольне вино.',
                'price': Decimal('160.00'),
                'weight_grams': 350,
                'calories': 95,
                'allergens': '',
                'is_vegetarian': True,
                'is_spicy': False,
                'image': 'https://images.unsplash.com/photo-1527661591475-527312dd65f5?auto=format&fit=crop&w=800&q=80',
                'options': [
                    ('Слайс апельсина та оливка', Decimal('15.00')),
                ],
            },
        ],
    },
]


class Command(BaseCommand):
    help = 'Повне очищення БД та заповнення меню з фотографіями, векторними ембедінгами, користувачами та FAQ'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clean-only',
            action='store_true',
            help='Тільки очистити базу даних без створення нових записів',
        )
        parser.add_argument(
            '--skip-images',
            action='store_true',
            help='Пропустити завантаження зовнішніх зображень',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING('Початок процесу очищення та заповнення бази даних...'))

        with transaction.atomic():
            self.stdout.write('Видалення старих тікетів, чатів та FAQ...')
            SupportTicket.objects.all().delete()
            ChatMessage.objects.all().delete()
            ChatSession.objects.all().delete()
            FAQKnowledge.objects.all().delete()

            self.stdout.write('Видалення старих замовлень, кошиків та адрес...')
            OrderItem.objects.all().delete()
            Order.objects.all().delete()
            CartItem.objects.all().delete()
            Cart.objects.all().delete()
            DeliveryAddress.objects.all().delete()

            self.stdout.write('Видалення старих опцій, страв та категорій...')
            DishOption.objects.all().delete()
            Dish.objects.all().delete()
            Category.objects.all().delete()

            admin_email = os.getenv('admin_mail', 'admin@doublebite.com')
            admin_password = os.getenv('admin_password', 'admin12345')
            if not User.objects.filter(email=admin_email).exists():
                User.objects.create_superuser(
                    email=admin_email,
                    password=admin_password,
                    first_name='Admin',
                    last_name='DoubleBite',
                )
                self.stdout.write(self.style.SUCCESS(f'Створено суперкористувача: {admin_email}'))
            else:
                self.stdout.write(f'Адміністратор {admin_email} вже існує.')

            if options['clean_only']:
                self.stdout.write(self.style.SUCCESS('Базу даних успішно очищено!'))
                return

            customer_email = 'customer@doublebite.com'
            customer_user = User.objects.filter(email=customer_email).first()
            if not customer_user:
                customer_user = User.objects.create_user(
                    email=customer_email,
                    password='customer12345',
                    first_name='Олена',
                    last_name='Коваленко',
                    phone='+380501234567',
                    role=Role.CUSTOMER,
                )
                self.stdout.write(self.style.SUCCESS(f'Створено клієнта: {customer_email}'))

            courier_email = 'courier@doublebite.com'
            if not User.objects.filter(email=courier_email).exists():
                User.objects.create_user(
                    email=courier_email,
                    password='courier12345',
                    first_name='Богдан',
                    last_name='Шевченко',
                    phone='+380671234567',
                    role=Role.COURIER,
                )
                self.stdout.write(self.style.SUCCESS(f'Створено кур\'єра: {courier_email}'))

            DeliveryAddress.objects.create(
                user=customer_user,
                title='Дім',
                city='Київ',
                street='вул. Хрещатик',
                building='24',
                apartment='15',
                floor='4',
                intercom='15',
                is_default=True,
            )
            DeliveryAddress.objects.create(
                user=customer_user,
                title='Робота',
                city='Київ',
                street='вул. Володимирська',
                building='42',
                apartment='Офіс 301',
                floor='3',
                intercom='301',
                is_default=False,
            )
            DeliveryAddress.objects.create(
                user=customer_user,
                title='Батьки',
                city='Київ',
                street='просп. Берестейський',
                building='18',
                apartment='50',
                floor='7',
                intercom='50',
                is_default=False,
            )

            faq_entries = [
                ('Які години роботи ресторану?', 'Ми приймаємо та доставляємо замовлення щодня з 10:00 до 22:00 без вихідних.', 'Графік роботи'),
                ('Яка мінімальна сума замовлення?', 'Мінімальна сума замовлення для оформлення доставки становить 200 грн.', 'Доставка'),
                ('Скільки коштує доставка?', 'Вартість доставки становить 50 грн. При замовленні на суму від 800 грн доставка безкоштовна.', 'Доставка'),
                ('Які способи оплати доступні?', 'Ви можете оплатити замовлення онлайн карткою через Stripe (Apple Pay, Google Pay) або готівкою / терміналом кур\'єру при отриманні.', 'Оплата'),
                ('Як дізнатися статус мого замовлення?', 'Ви можете переглянути актуальний статус замовлення на сторінці трекінгу або запитати нашого розумного асистента підтримки у чаті, вказавши номер замовлення (наприклад, DB-0001).', 'Замовлення'),
                ('Чи можна змінити або скасувати замовлення?', 'Скасувати замовлення можна, поки воно знаходиться у статусі «Очікує оплати» або «Оплачено», звернувшись до нашого асистента або за телефоном гарячої лінії ресторану.', 'Повернення та скасування'),
            ]
            for q, a, c in faq_entries:
                FAQKnowledge.objects.create(question=q, answer=a, category=c, is_active=True)

            self.stdout.write(self.style.MIGRATE_HEADING('Створення нових категорій та страв із векторними ембедінгами...'))

            total_categories = 0
            total_dishes = 0
            total_options = 0

            media_root_dir = Path(settings.MEDIA_ROOT)
            media_dishes_dir = media_root_dir / 'dishes'
            media_dishes_dir.mkdir(parents=True, exist_ok=True)
            parent_media_dir = Path(settings.BASE_DIR).parent / 'media'
            parent_media_dishes_dir = parent_media_dir / 'dishes'
            candidate_exts = ('.jpg', '.jpeg', '.png', '.webp')
            color_palette = [
                (230, 81, 0),
                (194, 24, 91),
                (0, 121, 107),
                (211, 47, 47),
                (81, 45, 168),
                (48, 63, 159),
                (2, 136, 209),
                (56, 142, 60),
            ]

            for cat_data in CATEGORIES_DATA:
                dishes_data = cat_data.get('dishes', [])
                category = Category.objects.create(
                    name=cat_data['name'],
                    slug=cat_data['slug'],
                    icon=cat_data.get('icon', ''),
                    display_order=cat_data.get('display_order', 0),
                    is_active=True,
                )
                total_categories += 1
                self.stdout.write(f' -> Категорія [{category.name}] ({len(dishes_data)} позицій)')

                for d_data in dishes_data:
                    options_list = d_data.get('options', [])
                    slug = d_data['slug']
                    img_src = d_data.get('image', '')

                    local_file = None
                    for ext in candidate_exts:
                        candidate = media_dishes_dir / f"{slug}{ext}"
                        if candidate.exists() and candidate.stat().st_size > 0:
                            local_file = candidate
                            break

                    if not local_file:
                        for ext in candidate_exts:
                            candidate = media_root_dir / f"{slug}{ext}"
                            if candidate.exists() and candidate.stat().st_size > 0:
                                target = media_dishes_dir / candidate.name
                                shutil.copyfile(candidate, target)
                                local_file = target
                                break

                    if not local_file and parent_media_dishes_dir.exists():
                        for ext in candidate_exts:
                            candidate = parent_media_dishes_dir / f"{slug}{ext}"
                            if candidate.exists() and candidate.stat().st_size > 0:
                                target = media_dishes_dir / candidate.name
                                shutil.copyfile(candidate, target)
                                local_file = target
                                break

                    if not local_file and parent_media_dir.exists():
                        for ext in candidate_exts:
                            candidate = parent_media_dir / f"{slug}{ext}"
                            if candidate.exists() and candidate.stat().st_size > 0:
                                target = media_dishes_dir / candidate.name
                                shutil.copyfile(candidate, target)
                                local_file = target
                                break

                    if not local_file and img_src and not str(img_src).startswith(('http://', 'https://')):
                        src_name = Path(img_src).name
                        candidate1 = media_dishes_dir / src_name
                        candidate2 = media_root_dir / src_name
                        if candidate1.exists() and candidate1.stat().st_size > 0:
                            local_file = candidate1
                        elif candidate2.exists() and candidate2.stat().st_size > 0:
                            target = media_dishes_dir / candidate2.name
                            shutil.copyfile(candidate2, target)
                            local_file = target

                    if not local_file:
                        target_file = media_dishes_dir / f"{slug}.jpg"
                        if not options.get('skip_images') and img_src and str(img_src).startswith(('http://', 'https://')):
                            try:
                                req = urllib.request.Request(
                                    img_src,
                                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
                                )
                                with urllib.request.urlopen(req, timeout=10) as resp:
                                    data = resp.read()
                                    if data:
                                        img_check = Image.open(io.BytesIO(data))
                                        img_check.verify()
                                        with open(target_file, 'wb') as f:
                                            f.write(data)
                                        local_file = target_file
                            except Exception:
                                if target_file.exists():
                                    target_file.unlink(missing_ok=True)

                        if not local_file or not target_file.exists() or target_file.stat().st_size == 0:
                            existing_samples = [
                                f for f in media_dishes_dir.iterdir()
                                if f.is_file() and f.suffix.lower() in candidate_exts and f.stat().st_size > 0 and f != target_file and not f.name.startswith('.')
                            ]
                            if existing_samples:
                                sample_idx = abs(hash(slug)) % len(existing_samples)
                                shutil.copyfile(existing_samples[sample_idx], target_file)
                                local_file = target_file
                            else:
                                bg_color = color_palette[abs(hash(slug)) % len(color_palette)]
                                img = Image.new('RGB', (800, 600), color=bg_color)
                                draw = ImageDraw.Draw(img)
                                draw.ellipse([200, 100, 600, 500], fill=(255, 255, 255), outline=(220, 220, 220), width=4)
                                draw.ellipse([250, 150, 550, 450], fill=(245, 245, 240))
                                img.save(target_file, format='JPEG', quality=85)
                                local_file = target_file

                    final_image = f"dishes/{local_file.name}"

                    embed_text = f"{d_data['title']} {category.name} {d_data['description']} {d_data.get('allergens', '')}"
                    embedding = generate_mock_embedding(embed_text)

                    dish = Dish.objects.create(
                        category=category,
                        title=d_data['title'],
                        slug=d_data['slug'],
                        description=d_data['description'],
                        price=d_data['price'],
                        weight_grams=d_data['weight_grams'],
                        calories=d_data['calories'],
                        allergens=d_data.get('allergens', ''),
                        is_vegetarian=d_data.get('is_vegetarian', False),
                        is_spicy=d_data.get('is_spicy', False),
                        is_available=True,
                        image=final_image,
                        embedding=embedding,
                    )
                    total_dishes += 1

                    for opt_name, price_delta in options_list:
                        DishOption.objects.create(
                            dish=dish,
                            name=opt_name,
                            price_delta=price_delta,
                        )
                        total_options += 1

            dish_margherita = Dish.objects.filter(slug__contains='margherita').first() or Dish.objects.first()
            dish_pepperoni = Dish.objects.filter(slug__contains='pepperoni').first() or Dish.objects.last()

            if dish_margherita:
                order1 = Order.objects.create(
                    order_number='DB-0001',
                    user=customer_user,
                    customer_name='Олена Коваленко',
                    customer_phone='+380501234567',
                    delivery_address='м. Київ, вул. Хрещатик, 24, кв. 15',
                    status=OrderStatus.DELIVERED,
                    payment_method=PaymentMethod.CARD,
                    payment_status=PaymentStatus.PAID,
                    total_amount=Decimal(str(dish_margherita.price * 2)),
                    eta_minutes=0,
                )
                OrderItem.objects.create(
                    order=order1,
                    dish=dish_margherita,
                    dish_title=dish_margherita.title,
                    price=dish_margherita.price,
                    quantity=2,
                )

            if dish_pepperoni:
                order2 = Order.objects.create(
                    order_number='DB-0002',
                    user=customer_user,
                    customer_name='Олена Коваленко',
                    customer_phone='+380501234567',
                    delivery_address='м. Київ, вул. Володимирська, 42, офіс 301',
                    status=OrderStatus.ON_WAY,
                    payment_method=PaymentMethod.CARD,
                    payment_status=PaymentStatus.PAID,
                    total_amount=Decimal(str(dish_pepperoni.price)),
                    eta_minutes=25,
                )
                OrderItem.objects.create(
                    order=order2,
                    dish=dish_pepperoni,
                    dish_title=dish_pepperoni.title,
                    price=dish_pepperoni.price,
                    quantity=1,
                )

        self.stdout.write(
            self.style.SUCCESS(
                f'\nУспішно заповнено базу даних!\n'
                f'  • Категорій створено: {total_categories}\n'
                f'  • Страв з ембедінгами створено: {total_dishes}\n'
                f'  • Опцій/модифікаторів створено: {total_options}\n'
                f'  • FAQ статей створено: {len(faq_entries)}\n'
                f'  • Демо-замовлень створено: 2\n'
                f'  • Демо-адрес створено: 3\n'
            )
        )
