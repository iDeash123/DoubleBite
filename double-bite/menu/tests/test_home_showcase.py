import json
import os
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import AsyncClient, Client, TestCase, override_settings
from django.urls import resolve, reverse
from orders.context_processors import LazyValue

from menu.models import Category, Dish
from menu.selectors import (
    DEFAULT_COLLECTIONS_DATA,
    DEFAULT_HERO_DISHES,
    get_home_page_context,
)
from menu.views import HomeView

User = get_user_model()


class HomeShowcaseTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.category = Category.objects.create(
            name='Бургери',
            slug='burgers',
            display_order=1,
            is_active=True,
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Truffle Wagyu Burger',
            slug='truffle-wagyu-burger',
            description='Фірмовий мармуровий бургер',
            price=Decimal('485.00'),
            weight_grams=380,
            calories=680,
            is_available=True,
        )

    def test_home_url_routes_to_menu_home_view(self):
        match = resolve(reverse('home'))
        self.assertIs(match.func.view_class, HomeView)

    def test_home_page_template_and_hero_render(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'home/index.html')
        self.assertTemplateUsed(response, 'base.html')
        self.assertTemplateUsed(response, 'home/partials/hero_showcase.html')
        self.assertTemplateUsed(response, 'home/partials/live_ticker.html')

    def test_hero_dishes_context_and_json(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('hero_dishes', response.context)
        self.assertIn('hero_dishes_json', response.context)
        hero_dishes = response.context['hero_dishes']
        self.assertEqual(len(hero_dishes), 4)
        titles = [d['title'] for d in hero_dishes]
        self.assertIn('Truffle Wagyu Burger', titles)
        self.assertIn('Quattro Formaggi al Tartufo', titles)
        self.assertIn('Wild Salmon & Avocado Bowl', titles)
        self.assertIn('Basque Burnt Cheesecake', titles)
        data = json.loads(response.context['hero_dishes_json'])
        self.assertEqual(len(data), 4)

    def test_hero_dishes_match_database_dish_without_modulo_fallback(self):
        response = self.client.get(reverse('home'))
        hero_dishes = response.context['hero_dishes']
        wagyu = next(d for d in hero_dishes if d['title'] == 'Truffle Wagyu Burger')
        self.assertEqual(wagyu['id'], self.dish.id)
        self.assertEqual(wagyu['slug'], self.dish.slug)

        unmatched_hero = [d for d in hero_dishes if d['title'] != 'Truffle Wagyu Burger']
        self.assertEqual(len(unmatched_hero), 3)
        for item in unmatched_hero:
            self.assertIsNone(item['id'])

        collections_data = response.context['collections_data']
        burgers_col = next(c for c in collections_data if c['slug'] == 'burgers')
        self.assertEqual(burgers_col['dishes'][0]['id'], self.dish.id)
        for extra_item in burgers_col['dishes'][1:]:
            self.assertIsNone(extra_item['id'])

        for other_col in collections_data:
            if other_col['slug'] != 'burgers':
                for item in other_col['dishes']:
                    self.assertIsNone(item['id'])

    def test_no_unsplash_urls_in_showcase_or_csp(self):
        self.assertNotIn('unsplash.com', json.dumps(DEFAULT_HERO_DISHES).lower())
        self.assertNotIn('unsplash.com', json.dumps(DEFAULT_COLLECTIONS_DATA).lower())
        img_src = settings.SECURE_CSP.get('img-src', [])
        self.assertFalse(any('unsplash.com' in str(src).lower() for src in img_src))

        ctx = get_home_page_context()
        self.assertNotIn('unsplash.com', ctx['hero_dishes_json'].lower())
        self.assertNotIn('unsplash.com', ctx['collections_json'].lower())

        response = self.client.get(reverse('home'))
        self.assertNotIn('unsplash.com', response.content.decode('utf-8').lower())

    def test_collections_use_bowls_salads_slug_and_existing_static_images(self):
        slugs = [c['slug'] for c in DEFAULT_COLLECTIONS_DATA]
        self.assertIn('bowls-salads', slugs)
        self.assertNotIn('bowls', slugs)

        image_urls = [d['image'] for d in DEFAULT_HERO_DISHES]
        for col in DEFAULT_COLLECTIONS_DATA:
            image_urls.extend(item['image'] for item in col['dishes'])

        static_prefix = getattr(settings, 'STATIC_URL', '/static/') or '/static/'
        for img_url in image_urls:
            self.assertIn('/images/showcase/', img_url)
            relative = img_url.removeprefix(static_prefix).lstrip('/')
            resolved = finders.find(relative)
            self.assertIsNotNone(resolved, f'Static showcase asset {relative} not found')
            self.assertTrue(os.path.exists(resolved), f'File {resolved} does not exist')

    def test_inactive_category_dishes_excluded_from_home_showcase(self):
        inactive_cat = Category.objects.create(
            name='Архівна піца',
            slug='pizza',
            display_order=2,
            is_active=False,
        )
        inactive_dish = Dish.objects.create(
            category=inactive_cat,
            title='Quattro Formaggi al Tartufo',
            slug='quattro-formaggi-inactive',
            price=Decimal('420.00'),
            weight_grams=460,
            is_available=True,
        )
        ctx = get_home_page_context()
        self.assertNotIn(inactive_dish, ctx['featured_dishes'])
        pizza_hero = next(
            d for d in ctx['hero_dishes'] if d['title'] == 'Quattro Formaggi al Tartufo'
        )
        self.assertIsNone(pizza_hero['id'])

    def test_live_gastro_ticker_content(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'ПРИЙМАЄМО ЗАМОВЛЕННЯ')
        self.assertContains(response, 'ТЕРМОБОКСИ З СЕНСОРАМИ')
        self.assertContains(response, 'БЕЗКОШТОВНА ДОСТАВКА ВІД 500 ₴')
        self.assertContains(response, 'MISTRAL AI CONCIERGE АКТИВНИЙ 24/7')

    def test_quick_cart_action_via_htmx(self):
        url = reverse('orders:cart_add', kwargs={'dish_id': self.dish.id})
        response = self.client.post(url, {'quantity': 1}, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('HX-Trigger'), 'open-cart-drawer')
        self.assertContains(response, '1')

    def test_ai_concierge_button_rendered(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'hero-ai-consult-btn')
        self.assertContains(response, 'Запитати AI-консьєржа про страву')
        self.assertContains(response, 'open-ai-chat')

    async def test_home_page_async_client_safety(self):
        async_client = AsyncClient()
        response = await async_client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'double bite')
        self.assertContains(response, 'hero-showcase')

    def test_about_section_rendered_with_pillar_and_transparency_images(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'home/partials/about_section.html')
        self.assertContains(response, 'pillar-forms.jpg')
        self.assertContains(response, 'pillar-ingredients.jpg')
        self.assertContains(response, 'pillar-proportions.jpg')
        self.assertContains(response, 'origin-beef.jpg')
        self.assertContains(response, 'origin-flour.jpg')
        self.assertContains(response, 'origin-salmon.jpg')
        self.assertContains(response, 'origin-cheese.jpg')
        self.assertContains(response, 'group-hover:scale-105')
        self.assertContains(response, 'ТРИ ФУНДАМЕНТАЛЬНИХ')
        self.assertContains(response, 'INGREDIENT TRANSPARENCY MAP')
        self.assertContains(response, 'ECO-PASTURE CERTIFIED')
        self.assertContains(response, 'ITALIAN D.O.P. 100%')
        self.assertContains(response, 'MSC WILD CHILLED')
        self.assertContains(response, 'ARTISANAL WHOLE MILK')

    def test_about_section_static_assets_exist_on_disk(self):
        required_images = [
            'images/about/pillar-forms.jpg',
            'images/about/pillar-ingredients.jpg',
            'images/about/pillar-proportions.jpg',
            'images/about/origin-beef.jpg',
            'images/about/origin-flour.jpg',
            'images/about/origin-salmon.jpg',
            'images/about/origin-cheese.jpg',
        ]
        for img_path in required_images:
            resolved = finders.find(img_path)
            self.assertIsNotNone(resolved, f'Static asset {img_path} not found by finders')
            self.assertTrue(os.path.exists(resolved), f'File {resolved} does not exist on disk')
            self.assertGreater(os.path.getsize(resolved), 10000, f'File {resolved} is too small')

    def test_get_home_page_context_selector_and_partial_delivery_math(self):
        ctx_full = get_home_page_context(total_amount=Decimal('600.00'))
        self.assertTrue(ctx_full['meets_free_delivery'])
        self.assertEqual(ctx_full['free_delivery_remaining'], Decimal('0.00'))
        self.assertEqual(ctx_full['free_delivery_percent'], 100)
        self.assertEqual(len(ctx_full['hero_dishes']), 4)
        self.assertEqual(len(ctx_full['collections_data']), 5)

        with override_settings(FREE_DELIVERY_THRESHOLD=Decimal('500.00')):
            ctx_half = get_home_page_context(total_amount=Decimal('250.00'))
            self.assertFalse(ctx_half['meets_free_delivery'])
            self.assertEqual(ctx_half['free_delivery_remaining'], Decimal('250.00'))
            self.assertEqual(ctx_half['free_delivery_percent'], 50)

            ctx_empty = get_home_page_context(total_amount=Decimal('0.00'))
            self.assertFalse(ctx_empty['meets_free_delivery'])
            self.assertEqual(ctx_empty['free_delivery_remaining'], Decimal('500.00'))
            self.assertEqual(ctx_empty['free_delivery_percent'], 0)

        with override_settings(FREE_DELIVERY_THRESHOLD=Decimal('0.00')):
            ctx_zero_thresh = get_home_page_context(total_amount=Decimal('0.00'))
            self.assertTrue(ctx_zero_thresh['meets_free_delivery'])
            self.assertEqual(ctx_zero_thresh['free_delivery_remaining'], Decimal('0.00'))
            self.assertEqual(ctx_zero_thresh['free_delivery_percent'], 100)

    @override_settings(
        FREE_DELIVERY_THRESHOLD=Decimal('750.00'),
        DELIVERY_FEE=Decimal('90.00'),
        RESTAURANT_ADDRESS='вул. Сагайдачного, 15, Київ',
        RESTAURANT_HOTLINE='+380 44 999 00 11',
    )
    def test_home_templates_reflect_unified_delivery_and_contact_settings(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'БЕЗКОШТОВНА ДОСТАВКА ВІД 750 ₴')
        self.assertContains(response, 'Від 750 ₴')
        self.assertContains(response, 'min_amount: 750')
        self.assertContains(response, '90 ₴')
        self.assertContains(response, 'вул. Сагайдачного, 15, Київ')
        self.assertContains(response, '+380 44 999 00 11')
        self.assertNotContains(response, '500 ₴')
        self.assertNotContains(response, '65 ₴')

    def test_lazy_value_supports_ordering_comparisons(self):
        low = LazyValue(lambda: Decimal('350.00'))
        high = LazyValue(lambda: Decimal('700.00'))
        threshold = Decimal('500.00')

        self.assertTrue(high >= threshold)
        self.assertTrue(high >= 500)
        self.assertFalse(low >= threshold)
        self.assertTrue(low <= threshold)
        self.assertTrue(high >= low)
        self.assertTrue(low <= high)
        self.assertTrue(low < high)
        self.assertTrue(high > low)

    @override_settings(FREE_DELIVERY_THRESHOLD=Decimal('500.00'))
    def test_home_free_delivery_bar_partial_cart_is_dynamic(self):
        half_dish = Dish.objects.create(
            category=self.category,
            title='Half Threshold Burger',
            slug='half-threshold-burger',
            price=Decimal('250.00'),
            weight_grams=250,
            is_available=True,
        )
        add_response = self.client.post(
            reverse('orders:cart_add', args=[half_dish.id]),
            {'quantity': 1},
        )
        self.assertEqual(add_response.status_code, 302)

        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(bool(response.context['meets_free_delivery']))
        self.assertEqual(Decimal(str(response.context['free_delivery_remaining'])), Decimal('250.00'))
        self.assertEqual(int(response.context['free_delivery_percent']), 50)
        self.assertContains(response, 'style="width: 50%;"')
        self.assertContains(response, '250.00 ₴')

    def test_home_free_delivery_bar_caps_at_100_when_threshold_reached(self):
        add_response = self.client.post(
            reverse('orders:cart_add', args=[self.dish.id]),
            {'quantity': 2},
        )
        self.assertEqual(add_response.status_code, 302)

        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'style="width: 100%;"')
        self.assertNotContains(response, 'style="width: 194%;"')
        self.assertContains(response, 'Безкоштовна доставка активна для вашого замовлення')
