import json
import re
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

from django.conf import settings
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

        short_dish_url = f'/menu/{self.available_dish.slug}/'
        short_resp = self.client.get(short_dish_url)
        self.assertEqual(short_resp.status_code, 200)
        self.assertIn(
            f'<link rel="canonical" href="http://testserver{dish_url}">',
            short_resp.content.decode('utf-8'),
        )
        self.assertIn(
            f'<meta property="og:url" content="http://testserver{dish_url}">',
            short_resp.content.decode('utf-8'),
        )

    def test_homepage_open_graph_tags(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('<meta property="og:title"', content)
        self.assertIn('<meta property="og:description"', content)
        self.assertIn('<meta property="og:image"', content)
        self.assertIn('<meta property="og:type" content="website">', content)
        self.assertIn('<meta property="og:url" content="http://testserver/">', content)
        self.assertIn('<meta property="og:locale" content="uk_UA">', content)

    def test_dish_detail_open_graph_tags(self):
        dish_url = self.available_dish.get_absolute_url()
        response = self.client.get(dish_url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn(
            f'<meta property="og:title" content="{self.available_dish.title} — Double Bite">',
            content,
        )
        self.assertIn('<meta property="og:description"', content)
        self.assertIn('<meta property="og:image"', content)
        self.assertIn('<meta property="og:type" content="website">', content)
        self.assertIn(
            f'<meta property="og:url" content="http://testserver{dish_url}">',
            content,
        )
        self.assertIn('<meta property="og:locale" content="uk_UA">', content)

    def test_homepage_schema_org_json_ld(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        match = re.search(r'<script type="application/ld\+json">(.*?)</script>', content, re.DOTALL)
        self.assertIsNotNone(match)
        data = json.loads(match.group(1).strip())
        self.assertEqual(data.get('@context'), 'https://schema.org')
        self.assertEqual(data.get('@type'), 'Restaurant')
        self.assertEqual(data.get('name'), 'Double Bite')
        self.assertIn('Піца', data.get('servesCuisine', []))
        self.assertIn('Бургери', data.get('servesCuisine', []))
        address = data.get('address', {})
        self.assertEqual(address.get('@type'), 'PostalAddress')
        self.assertEqual(address.get('addressLocality'), 'Київ')
        self.assertEqual(address.get('addressCountry'), 'UA')
        self.assertEqual(data.get('url'), 'http://testserver')

    def test_dish_detail_schema_org_json_ld(self):
        dish_url = self.available_dish.get_absolute_url()
        response = self.client.get(dish_url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        match = re.search(r'<script type="application/ld\+json">(.*?)</script>', content, re.DOTALL)
        self.assertIsNotNone(match)
        data = json.loads(match.group(1).strip())
        self.assertEqual(data.get('@context'), 'https://schema.org')
        self.assertEqual(data.get('@type'), 'Product')
        self.assertEqual(data.get('name'), self.available_dish.title)
        self.assertEqual(data.get('description'), self.available_dish.description)
        self.assertEqual(data.get('url'), f'http://testserver{dish_url}')
        offers = data.get('offers', {})
        self.assertEqual(offers.get('@type'), 'Offer')
        self.assertEqual(offers.get('price'), f'{self.available_dish.price:.2f}')
        self.assertEqual(offers.get('priceCurrency'), 'UAH')
        self.assertEqual(offers.get('availability'), 'https://schema.org/InStock')
        nutrition = data.get('nutrition', {})
        self.assertEqual(nutrition.get('@type'), 'NutritionInformation')
        self.assertEqual(nutrition.get('calories'), f'{self.available_dish.calories} cal')

    def test_meta_title_and_description_hierarchy(self):
        home_resp = self.client.get('/')
        self.assertEqual(home_resp.status_code, 200)
        home_content = home_resp.content.decode('utf-8')
        self.assertIn(
            '<title>Double Bite — Онлайн-замовлення їжі з доставкою | Піца, суші, бургери, салати</title>',
            home_content,
        )
        self.assertIn('<meta name="description" content="', home_content)

        catalog_resp = self.client.get('/menu/')
        self.assertEqual(catalog_resp.status_code, 200)
        catalog_content = catalog_resp.content.decode('utf-8')
        self.assertIn(
            'Меню страв — Double Bite™ | Авторська ресторанна доставка в Києві',
            catalog_content,
        )

        dish_url = self.available_dish.get_absolute_url()
        dish_resp = self.client.get(dish_url)
        self.assertEqual(dish_resp.status_code, 200)
        dish_content = dish_resp.content.decode('utf-8')
        self.assertIn(
            f'<title>{self.available_dish.title}',
            dish_content,
        )
        self.assertIn('Double Bite™</title>', dish_content)
        self.assertIn('грн', dish_content)

    def test_catalog_and_category_open_graph_tags(self):
        catalog_resp = self.client.get('/menu/')
        self.assertEqual(catalog_resp.status_code, 200)
        catalog_content = catalog_resp.content.decode('utf-8')
        self.assertIn(
            '<meta property="og:title" content="Меню страв — Double Bite™ | Авторська ресторанна доставка в Києві">',
            catalog_content,
        )
        self.assertIn('<meta property="og:description"', catalog_content)

        cat_resp = self.client.get(f'/menu/{self.active_category.slug}/')
        self.assertEqual(cat_resp.status_code, 200)
        cat_content = cat_resp.content.decode('utf-8')
        self.assertIn(
            f'<meta property="og:title" content="{self.active_category.name} з доставкою — Double Bite | Меню ресторану">',
            cat_content,
        )
        self.assertIn('<meta property="og:description"', cat_content)

    def test_accessibility_and_semantic_html(self):
        dish_url = self.available_dish.get_absolute_url()
        dish_resp = self.client.get(dish_url)
        self.assertEqual(dish_resp.status_code, 200)
        dish_content = dish_resp.content.decode('utf-8')
        self.assertIn('<header', dish_content)
        self.assertIn('<main', dish_content)
        self.assertIn('<footer', dish_content)
        self.assertIn('aria-label="Кошик"', dish_content)
        self.assertIn('aria-label="Зменшити кількість"', dish_content)
        self.assertIn('aria-label="Збільшити кількість"', dish_content)

        catalog_resp = self.client.get('/menu/')
        self.assertEqual(catalog_resp.status_code, 200)
        catalog_content = catalog_resp.content.decode('utf-8')
        self.assertIn('aria-label="Закрити"', catalog_content)

        self.client.post(f'/cart/add/{self.available_dish.id}/')
        cart_resp = self.client.get('/cart/')
        self.assertEqual(cart_resp.status_code, 200)
        cart_content = cart_resp.content.decode('utf-8')
        self.assertIn('aria-label="Зменшити кількість"', cart_content)
        self.assertIn('aria-label="Збільшити кількість"', cart_content)
        self.assertIn('aria-label="Видалити страву"', cart_content)

    def test_zero_comments_in_seo_files(self):
        files_to_check = [
            settings.BASE_DIR / 'config' / 'settings.py',
            settings.BASE_DIR / 'config' / 'urls.py',
            settings.BASE_DIR / 'menu' / 'models.py',
            settings.BASE_DIR / 'menu' / 'sitemaps.py',
            settings.BASE_DIR / 'menu' / 'views.py',
            settings.BASE_DIR / 'menu' / 'tests' / 'test_seo.py',
            settings.BASE_DIR / 'templates' / 'base.html',
            settings.BASE_DIR / 'templates' / 'home' / 'index.html',
            settings.BASE_DIR / 'templates' / 'menu' / 'catalog.html',
            settings.BASE_DIR / 'templates' / 'menu' / 'dish_detail.html',
            settings.BASE_DIR / 'templates' / 'partials' / 'cart_drawer.html',
            settings.BASE_DIR / 'templates' / 'orders' / 'cart.html',
        ]
        for fpath in files_to_check:
            content = Path(fpath).read_text(encoding='utf-8')
            for line_idx, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if fpath.suffix == '.py':
                    self.assertFalse(
                        stripped.startswith('#'),
                        f'Comment found in {fpath.name}:{line_idx}: {stripped}',
                    )
                elif fpath.suffix == '.html':
                    self.assertNotIn(
                        '{#',
                        stripped,
                        f'Django comment found in {fpath.name}:{line_idx}: {stripped}',
                    )
                    self.assertNotIn(
                        '<!--',
                        stripped,
                        f'HTML comment found in {fpath.name}:{line_idx}: {stripped}',
                    )
