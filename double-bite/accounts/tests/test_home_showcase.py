import json
import os
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import AsyncClient, Client, TestCase
from django.urls import reverse
from menu.models import Category, Dish

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

    def test_hero_dishes_match_database_dish(self):
        response = self.client.get(reverse('home'))
        hero_dishes = response.context['hero_dishes']
        wagyu = next(d for d in hero_dishes if d['title'] == 'Truffle Wagyu Burger')
        self.assertEqual(wagyu['id'], self.dish.id)
        self.assertEqual(wagyu['slug'], self.dish.slug)

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

    def test_get_home_page_context_selector(self):
        from accounts.selectors import get_home_page_context

        ctx = get_home_page_context(total_amount=Decimal('600.00'))
        self.assertTrue(ctx['meets_free_delivery'])
        self.assertEqual(ctx['free_delivery_remaining'], Decimal('0.00'))
        self.assertEqual(len(ctx['hero_dishes']), 4)
        self.assertEqual(len(ctx['collections_data']), 5)


