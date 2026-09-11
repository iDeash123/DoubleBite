from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse

from menu.models import Category, Dish, DishOption


class MenuViewsTest(TestCase):
    def setUp(self):
        self.client = Client()

        self.category_pizza = Category.objects.create(
            name='Піца',
            slug='pizza',
            display_order=1,
            is_active=True,
        )
        self.category_burgers = Category.objects.create(
            name='Бургери',
            slug='burgers',
            display_order=2,
            is_active=True,
        )
        self.category_inactive = Category.objects.create(
            name='Сезонне',
            slug='seasonal',
            display_order=3,
            is_active=False,
        )

        self.dish_margarita = Dish.objects.create(
            category=self.category_pizza,
            title='Маргарита',
            slug='margarita',
            description='Класична італійська піца',
            price=Decimal('250.00'),
            weight_grams=450,
            calories=780,
            allergens='лактоза, глютен',
            is_vegetarian=True,
            is_spicy=False,
            is_available=True,
        )
        self.dish_diavola = Dish.objects.create(
            category=self.category_pizza,
            title='Діавола',
            slug='diavola',
            description='Гостра піца з пепероні',
            price=Decimal('310.00'),
            weight_grams=490,
            calories=920,
            allergens='глютен, свинина',
            is_vegetarian=False,
            is_spicy=True,
            is_available=True,
        )
        self.dish_burger = Dish.objects.create(
            category=self.category_burgers,
            title='Трюфельний бургер',
            slug='truffle-burger',
            description='Мармурова яловичина та трюфельний соус',
            price=Decimal('350.00'),
            weight_grams=380,
            calories=850,
            allergens='глютен',
            is_vegetarian=False,
            is_spicy=False,
            is_available=True,
        )
        self.dish_unavailable = Dish.objects.create(
            category=self.category_burgers,
            title='Чорний бургер',
            slug='black-burger',
            price=Decimal('360.00'),
            weight_grams=390,
            is_available=False,
        )
        self.dish_inactive_cat = Dish.objects.create(
            category=self.category_inactive,
            title='Сезонний пунш',
            slug='seasonal-punch',
            price=Decimal('150.00'),
            weight_grams=300,
            is_available=True,
        )

        self.option_size = DishOption.objects.create(
            dish=self.dish_margarita,
            name='Велика (40 см)',
            price_delta=Decimal('60.00'),
        )

    def test_catalog_view_full_page(self):
        url = reverse('menu:catalog')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'menu/catalog.html')
        self.assertIn('dishes', response.context)
        self.assertIn('categories', response.context)
        self.assertEqual(len(response.context['categories']), 2)
        dishes = list(response.context['dishes'])
        self.assertEqual(len(dishes), 3)
        self.assertIn(self.dish_margarita, dishes)
        self.assertIn(self.dish_diavola, dishes)
        self.assertIn(self.dish_burger, dishes)
        self.assertNotIn(self.dish_unavailable, dishes)
        self.assertNotIn(self.dish_inactive_cat, dishes)

    def test_catalog_view_filter_by_category(self):
        url = reverse('menu:catalog')
        response = self.client.get(url, {'category': 'pizza'})
        self.assertEqual(response.status_code, 200)
        dishes = list(response.context['dishes'])
        self.assertEqual(len(dishes), 2)
        self.assertIn(self.dish_margarita, dishes)
        self.assertIn(self.dish_diavola, dishes)
        self.assertNotIn(self.dish_burger, dishes)

    def test_catalog_view_filter_by_query(self):
        url = reverse('menu:catalog')
        response = self.client.get(url, {'q': 'трюфельний'})
        self.assertEqual(response.status_code, 200)
        dishes = list(response.context['dishes'])
        self.assertEqual(len(dishes), 1)
        self.assertEqual(dishes[0], self.dish_burger)

    def test_catalog_view_filter_by_vegetarian(self):
        url = reverse('menu:catalog')
        response = self.client.get(url, {'is_vegetarian': 'true'})
        self.assertEqual(response.status_code, 200)
        dishes = list(response.context['dishes'])
        self.assertEqual(len(dishes), 1)
        self.assertEqual(dishes[0], self.dish_margarita)

    def test_catalog_view_filter_by_spicy(self):
        url = reverse('menu:catalog')
        response = self.client.get(url, {'is_spicy': 'true'})
        self.assertEqual(response.status_code, 200)
        dishes = list(response.context['dishes'])
        self.assertEqual(len(dishes), 1)
        self.assertEqual(dishes[0], self.dish_diavola)

    def test_catalog_view_filter_by_max_price(self):
        url = reverse('menu:catalog')
        response = self.client.get(url, {'max_price': '260'})
        self.assertEqual(response.status_code, 200)
        dishes = list(response.context['dishes'])
        self.assertEqual(len(dishes), 1)
        self.assertEqual(dishes[0], self.dish_margarita)

    def test_catalog_view_htmx_partial_response(self):
        url = reverse('menu:catalog')
        response = self.client.get(url, {'category': 'pizza'}, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="dish-grid"', content)
        self.assertIn('Маргарита', content)
        self.assertIn('Діавола', content)
        self.assertNotIn('<!DOCTYPE html>', content)
        self.assertNotIn('<header', content)

    def test_dish_detail_view_success(self):
        url = reverse('menu:dish_detail', kwargs={'dish_slug': self.dish_margarita.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'menu/dish_detail.html')
        self.assertEqual(response.context['dish'], self.dish_margarita)
        self.assertEqual(len(response.context['options']), 1)
        self.assertEqual(response.context['options'][0], self.option_size)
        self.assertIn('breadcrumbs', response.context)
        content = response.content.decode('utf-8')
        self.assertIn('Маргарита', content)
        self.assertIn('250', content)
        self.assertIn('грн', content)
        self.assertIn('Велика (40 см)', content)

    def test_dish_detail_category_slug_success(self):
        url = reverse(
            'menu:category_dish_detail',
            kwargs={'category_slug': 'pizza', 'dish_slug': self.dish_margarita.slug},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_dish_detail_view_404_nonexistent(self):
        url = reverse('menu:dish_detail', kwargs={'dish_slug': 'non-existent-dish'})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_dish_detail_view_404_unavailable(self):
        url = reverse('menu:dish_detail', kwargs={'dish_slug': self.dish_unavailable.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_dish_detail_view_404_inactive_category(self):
        url = reverse('menu:dish_detail', kwargs={'dish_slug': self.dish_inactive_cat.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)
