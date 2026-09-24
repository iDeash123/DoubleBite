import xml.etree.ElementTree as ET
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse

from menu.models import Category, Dish


class SeoEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.active_category = Category.objects.create(
            name='Піца',
            slug='pizza',
            display_order=1,
            is_active=True,
        )
        self.inactive_category = Category.objects.create(
            name='Архів',
            slug='archive-cat',
            display_order=99,
            is_active=False,
        )

        self.available_dish = Dish.objects.create(
            category=self.active_category,
            title='Маргарита Преміум',
            slug='margarita-premium',
            description='Класична піца з моцарелою',
            price=Decimal('280.00'),
            weight_grams=450,
            calories=750,
            is_available=True,
        )
        self.unavailable_dish = Dish.objects.create(
            category=self.active_category,
            title='Сезонна страва',
            slug='seasonal-unavailable',
            description='Тимчасово недоступна',
            price=Decimal('350.00'),
            weight_grams=300,
            calories=500,
            is_available=False,
        )
        self.dish_in_inactive_category = Dish.objects.create(
            category=self.inactive_category,
            title='Стара страва',
            slug='inactive-cat-dish',
            description='Страва з закритої категорії',
            price=Decimal('200.00'),
            weight_grams=250,
            calories=400,
            is_available=True,
        )

    def test_robots_txt_status_and_content(self):
        response = self.client.get('/robots.txt')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('text/plain'))
        content = response.content.decode('utf-8')
        self.assertIn('User-agent: *', content)
        self.assertIn('Allow: /', content)
        self.assertIn('Disallow: /admin/', content)
        self.assertIn('Disallow: /cart/', content)
        self.assertIn('Disallow: /orders/', content)
        self.assertIn('Disallow: /support/chat/', content)
        self.assertIn('Sitemap: http://testserver/sitemap.xml', content)

    def test_sitemap_xml_status_and_format(self):
        response = self.client.get('/sitemap.xml')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response['Content-Type'].startswith('application/xml')
            or response['Content-Type'].startswith('text/xml')
        )
        root = ET.fromstring(response.content)
        self.assertTrue(root.tag.endswith('urlset'))
        namespaces = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        loc_elements = root.findall('ns:url/ns:loc', namespaces)
        locations = [elem.text.strip() for elem in loc_elements if elem.text]

        home_url = 'http://testserver/'
        catalog_url = f'http://testserver{reverse("menu:catalog")}'
        category_url = f'http://testserver{self.active_category.get_absolute_url()}'
        dish_url = f'http://testserver{self.available_dish.get_absolute_url()}'

        self.assertIn(home_url, locations)
        self.assertIn(catalog_url, locations)
        self.assertIn(category_url, locations)
        self.assertIn(dish_url, locations)

    def test_sitemap_xml_excludes_inactive_and_unavailable(self):
        response = self.client.get('/sitemap.xml')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertNotIn(self.unavailable_dish.slug, content)
        self.assertNotIn(self.inactive_category.slug, content)
        self.assertNotIn(self.dish_in_inactive_category.slug, content)

    def test_canonical_links_in_pages(self):
        home_resp = self.client.get('/')
        self.assertEqual(home_resp.status_code, 200)
        self.assertIn(
            '<link rel="canonical" href="http://testserver/">',
            home_resp.content.decode('utf-8'),
        )

        catalog_resp = self.client.get('/menu/')
        self.assertEqual(catalog_resp.status_code, 200)
        self.assertIn(
            '<link rel="canonical" href="http://testserver/menu/">',
            catalog_resp.content.decode('utf-8'),
        )

        filtered_catalog_resp = self.client.get('/menu/?category=pizza&sort=price_asc&page=1')
        self.assertEqual(filtered_catalog_resp.status_code, 200)
        self.assertIn(
            '<link rel="canonical" href="http://testserver/menu/">',
            filtered_catalog_resp.content.decode('utf-8'),
        )

        dish_url = self.available_dish.get_absolute_url()
        dish_resp = self.client.get(dish_url)
        self.assertEqual(dish_resp.status_code, 200)
        self.assertIn(
            f'<link rel="canonical" href="http://testserver{dish_url}">',
            dish_resp.content.decode('utf-8'),
        )
