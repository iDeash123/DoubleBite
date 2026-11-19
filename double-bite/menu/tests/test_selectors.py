from decimal import Decimal

from django.test import TestCase
from support.vector_search import generate_mock_embedding

from menu.models import Category, Dish, DishOption
from menu.selectors import (
    filter_dishes,
    get_active_categories,
    get_available_dishes,
    get_dish_by_id,
    get_dish_by_slug,
    search_dishes_for_agent,
)


class MenuSelectorsTest(TestCase):
    def setUp(self):
        self.cat_pizza = Category.objects.create(
            name='Піца',
            slug='pizza',
            display_order=1,
            is_active=True,
        )
        self.cat_burgers = Category.objects.create(
            name='Бургери',
            slug='burgers',
            display_order=2,
            is_active=True,
        )
        self.cat_inactive = Category.objects.create(
            name='Сезонне',
            slug='seasonal',
            display_order=3,
            is_active=False,
        )

        self.dish1 = Dish.objects.create(
            category=self.cat_pizza,
            title='Маргарита Класична',
            slug='margarita-classic',
            description='Свіжі томати, моцарела та базилік',
            price=Decimal('220.00'),
            weight_grams=450,
            calories=750,
            allergens='лактоза, глютен',
            is_vegetarian=True,
            is_spicy=False,
            is_available=True,
        )
        self.dish2 = Dish.objects.create(
            category=self.cat_pizza,
            title='Діавола Гостра',
            slug='diavola',
            description='Салямі піканте, халапеньйо, перець чилі',
            price=Decimal('280.00'),
            weight_grams=480,
            calories=950,
            allergens='глютен, свинина',
            is_vegetarian=False,
            is_spicy=True,
            is_available=True,
        )
        self.dish3 = Dish.objects.create(
            category=self.cat_burgers,
            title='Чизбургер Преміум',
            slug='cheeseburger-premium',
            description='Яловичина, чедер, солоний огірок',
            price=Decimal('190.00'),
            weight_grams=350,
            calories=600,
            allergens='лактоза, глютен',
            is_vegetarian=False,
            is_spicy=False,
            is_available=False,
        )
        self.dish_inactive_cat = Dish.objects.create(
            category=self.cat_inactive,
            title='Глінтвейн',
            slug='glintwein',
            price=Decimal('120.00'),
            weight_grams=250,
            is_available=True,
        )

        DishOption.objects.create(
            dish=self.dish1,
            name='Подвійний сир',
            price_delta=Decimal('40.00'),
        )

    def test_get_active_categories(self):
        cats = list(get_active_categories())
        self.assertEqual(len(cats), 2)
        self.assertIn(self.cat_pizza, cats)
        self.assertIn(self.cat_burgers, cats)
        self.assertNotIn(self.cat_inactive, cats)

    def test_get_available_dishes(self):
        dishes = list(get_available_dishes())
        self.assertEqual(len(dishes), 2)
        self.assertIn(self.dish1, dishes)
        self.assertIn(self.dish2, dishes)
        self.assertNotIn(self.dish3, dishes)
        self.assertNotIn(self.dish_inactive_cat, dishes)

    def test_get_dish_by_slug(self):
        dish = get_dish_by_slug('pizza', 'margarita-classic')
        self.assertIsNotNone(dish)
        self.assertEqual(dish.id, self.dish1.id)

        dish_none = get_dish_by_slug('pizza', 'non-existing')
        self.assertIsNone(dish_none)

    def test_get_dish_by_id(self):
        dish = get_dish_by_id(self.dish1.id)
        self.assertIsNotNone(dish)
        self.assertEqual(dish.slug, 'margarita-classic')

        dish_none = get_dish_by_id(999999)
        self.assertIsNone(dish_none)

    def test_filter_dishes_by_category(self):
        dishes = list(filter_dishes(category_slug='pizza'))
        self.assertEqual(len(dishes), 2)
        self.assertIn(self.dish1, dishes)
        self.assertIn(self.dish2, dishes)

    def test_filter_dishes_by_query_title(self):
        dishes = list(filter_dishes(query='Маргарита'))
        self.assertEqual(len(dishes), 1)
        self.assertEqual(dishes[0], self.dish1)

    def test_filter_dishes_by_query_description(self):
        dishes = list(filter_dishes(query='халапеньйо'))
        self.assertEqual(len(dishes), 1)
        self.assertEqual(dishes[0], self.dish2)

    def test_filter_dishes_by_max_price(self):
        dishes = list(filter_dishes(max_price=Decimal('250.00')))
        self.assertEqual(len(dishes), 1)
        self.assertEqual(dishes[0], self.dish1)

    def test_filter_dishes_by_vegetarian(self):
        dishes = list(filter_dishes(is_vegetarian=True))
        self.assertEqual(len(dishes), 1)
        self.assertEqual(dishes[0], self.dish1)

    def test_filter_dishes_by_spicy(self):
        dishes = list(filter_dishes(is_spicy=True))
        self.assertEqual(len(dishes), 1)
        self.assertEqual(dishes[0], self.dish2)

    def test_filter_dishes_by_max_calories(self):
        dishes = list(filter_dishes(max_calories=800))
        self.assertEqual(len(dishes), 1)
        self.assertEqual(dishes[0], self.dish1)

    def test_filter_dishes_exclude_allergens(self):
        dishes = list(filter_dishes(exclude_allergens=['свинина']))
        self.assertEqual(len(dishes), 1)
        self.assertEqual(dishes[0], self.dish1)

    def test_filter_dishes_include_unavailable(self):
        dishes = list(filter_dishes(only_available=False, category_slug='burgers'))
        self.assertEqual(len(dishes), 1)
        self.assertEqual(dishes[0], self.dish3)

    def test_search_dishes_for_agent(self):
        results = search_dishes_for_agent(query='піца', max_price=300)
        self.assertEqual(len(results), 2)
        first = results[0]
        self.assertIn('title', first)
        self.assertIn('category', first)
        self.assertIn('price', first)
        self.assertIn('options', first)
        self.assertTrue(len(first['options']) >= 1)

    def test_search_dishes_for_agent_sql_filter_before_limit_and_cutoff(self):
        high_sim_vec = generate_mock_embedding('трюфельна неаполітанська паста')
        for idx in range(5):
            Dish.objects.create(
                category=self.cat_pizza,
                title=f'Трюфельна Стейк Піца {idx}',
                slug=f'truffle-steak-{idx}',
                description='трюфельна неаполітанська паста',
                price=Decimal('490.00'),
                weight_grams=480,
                calories=920,
                is_vegetarian=False,
                is_available=True,
                embedding=high_sim_vec,
            )

        veg_match = Dish.objects.create(
            category=self.cat_pizza,
            title='Легка Веганська Паста',
            slug='light-veg-pasta',
            description='ніжна паста з зеленню',
            price=Decimal('195.00'),
            weight_grams=320,
            calories=420,
            is_vegetarian=True,
            is_available=True,
            embedding=generate_mock_embedding('паста зелень томати'),
        )

        results = search_dishes_for_agent(
            query='трюфельна неаполітанська паста',
            max_price=Decimal('250.00'),
            is_vegetarian=True,
            limit=2,
        )
        self.assertEqual([item['id'] for item in results], [veg_match.id])

        unrelated = search_dishes_for_agent(
            query='абсолютно неіснуючий трактор космос',
            limit=5,
        )
        self.assertEqual(unrelated, [])

    def test_filter_dishes_exclude_allergens_cyrillic_case_insensitive(self):
        dish_nuts_titlecase = Dish.objects.create(
            category=self.cat_pizza,
            title='Горіхова Піца',
            slug='nut-pizza-titlecase',
            price=Decimal('290.00'),
            weight_grams=440,
            allergens='Горіхи, лактоза',
            is_available=True,
        )
        dish_nuts_lowercase = Dish.objects.create(
            category=self.cat_pizza,
            title='Тарт з фундуком',
            slug='hazelnut-tart',
            price=Decimal('210.00'),
            weight_grams=200,
            allergens='горіхи',
            is_available=True,
        )

        excluded_from_lower = list(filter_dishes(exclude_allergens=['горіхи']))
        self.assertNotIn(dish_nuts_titlecase, excluded_from_lower)
        self.assertNotIn(dish_nuts_lowercase, excluded_from_lower)
        self.assertIn(self.dish1, excluded_from_lower)

        excluded_from_title = list(filter_dishes(exclude_allergens=['Горіхи']))
        self.assertNotIn(dish_nuts_titlecase, excluded_from_title)
        self.assertNotIn(dish_nuts_lowercase, excluded_from_title)
        self.assertIn(self.dish1, excluded_from_title)

    def test_filter_dishes_prefetch_options_query_behavior(self):
        qs_no_prefetch = filter_dishes(category_slug='pizza', prefetch_options=False)
        self.assertEqual(qs_no_prefetch._prefetch_related_lookups, ())
        with self.assertNumQueries(1):
            dishes_no_prefetch = list(qs_no_prefetch)
        self.assertEqual(len(dishes_no_prefetch), 2)

        qs_prefetch = filter_dishes(category_slug='pizza', prefetch_options=True)
        self.assertEqual(qs_prefetch._prefetch_related_lookups, ('options',))
        with self.assertNumQueries(2):
            dishes_prefetch = list(qs_prefetch)
            all_options = [list(d.options.all()) for d in dishes_prefetch]
        self.assertEqual(len(dishes_prefetch), 2)
        self.assertTrue(any(len(opts) == 1 for opts in all_options))
