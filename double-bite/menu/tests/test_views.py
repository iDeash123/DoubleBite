import json
import re
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from menu import views
from menu.models import Category, Dish, DishOption


class _CatalogSelectHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.selects: list[dict[str, str | None]] = []
        self.sort_options: list[str] = []
        self.number_inputs: list[dict[str, str | None]] = []
        self._in_sort_select = False

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        if tag == 'select':
            self.selects.append(attr_map)
            if attr_map.get('name') == 'sort':
                self._in_sort_select = True
        elif tag == 'option' and self._in_sort_select:
            if 'value' in attr_map and attr_map['value'] is not None:
                self.sort_options.append(attr_map['value'])
        elif tag == 'input':
            if attr_map.get('type') == 'number':
                self.number_inputs.append(attr_map)

    def handle_endtag(self, tag):
        if tag == 'select':
            self._in_sort_select = False


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

    def test_category_slug_url_renders_catalog(self):
        url = reverse('menu:dish_detail', kwargs={'dish_slug': self.category_pizza.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'menu/catalog.html')
        dishes = list(response.context['dishes'])
        self.assertEqual(len(dishes), 2)
        self.assertIn(self.dish_margarita, dishes)
        self.assertIn(self.dish_diavola, dishes)
        self.assertNotIn(self.dish_burger, dishes)

    def test_menu_slug_dispatch_reuses_fetched_dish(self):
        with patch('menu.views.dish_detail_view', wraps=views.dish_detail_view) as mock_detail:
            url = reverse('menu:dish_detail', kwargs={'dish_slug': self.dish_margarita.slug})
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            mock_detail.assert_called_once()
            _, kwargs = mock_detail.call_args
            self.assertEqual(kwargs.get('dish'), self.dish_margarita)

    def test_menu_slug_dispatch_prioritizes_active_category_over_dish(self):
        shared_cat = Category.objects.create(
            name='Спільна категорія',
            slug='shared-slug',
            display_order=4,
            is_active=True,
        )
        cat_dish = Dish.objects.create(
            category=shared_cat,
            title='Страва з категорії',
            slug='category-owned-dish',
            price=Decimal('210.00'),
            weight_grams=300,
            is_available=True,
        )
        shadowed_dish = Dish.objects.create(
            category=self.category_burgers,
            title='Бургер з тим самим слагом',
            slug='shared-slug',
            price=Decimal('320.00'),
            weight_grams=350,
            is_available=True,
        )

        resp = self.client.get(reverse('menu:dish_detail', kwargs={'dish_slug': 'shared-slug'}))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'menu/catalog.html')
        dishes = list(resp.context['dishes'])
        self.assertIn(cat_dish, dishes)
        self.assertNotIn(shadowed_dish, dishes)

        shared_cat.is_active = False
        shared_cat.save(update_fields=['is_active'])
        fallback_resp = self.client.get(
            reverse('menu:dish_detail', kwargs={'dish_slug': 'shared-slug'})
        )
        self.assertEqual(fallback_resp.status_code, 200)
        self.assertTemplateUsed(fallback_resp, 'menu/dish_detail.html')
        self.assertEqual(fallback_resp.context['dish'], shadowed_dish)

    def test_catalog_html_parses_cleanly_with_valid_sort_select_and_step(self):
        response = self.client.get(reverse('menu:catalog'))
        self.assertEqual(response.status_code, 200)
        vary_tokens = [v.strip() for v in response.headers.get('Vary', '').split(',')]
        self.assertIn('HX-Request', vary_tokens)
        content = response.content.decode('utf-8')

        parser = _CatalogSelectHTMLParser()
        parser.feed(content)
        parser.close()

        sort_select = next((s for s in parser.selects if s.get('name') == 'sort'), None)
        self.assertIsNotNone(sort_select)
        self.assertIn('db-select-arrow', (sort_select.get('class') or '').split())
        self.assertIsNone(sort_select.get('style'))
        self.assertEqual(
            parser.sort_options,
            [
                'default',
                'price_asc',
                'price_desc',
                'name_asc',
                'name_desc',
                'calories_asc',
                'calories_desc',
            ],
        )

        max_price_input = next(
            (inp for inp in parser.number_inputs if inp.get('name') == 'max_price'),
            None,
        )
        self.assertIsNotNone(max_price_input)
        self.assertEqual(max_price_input.get('step'), '1')
        self.assertIn('htmx:configRequest', content)
        self.assertNotIn('style="background-image:', content)

    def test_catalog_htmx_oob_total_vary_and_history_restore(self):
        url = reverse('menu:catalog')
        htmx_resp = self.client.get(url, {'category': 'pizza'}, HTTP_HX_REQUEST='true')
        self.assertEqual(htmx_resp.status_code, 200)
        htmx_vary = [v.strip() for v in htmx_resp.headers.get('Vary', '').split(',')]
        self.assertIn('HX-Request', htmx_vary)
        htmx_html = htmx_resp.content.decode('utf-8')
        self.assertIn('id="total-dishes"', htmx_html)
        self.assertIn('hx-swap-oob="true"', htmx_html)
        self.assertIn('<strong class="text-fv-noir">2</strong>', htmx_html)
        self.assertNotIn('<!DOCTYPE html>', htmx_html)

        restore_resp = self.client.get(
            url,
            {'category': 'pizza'},
            HTTP_HX_REQUEST='true',
            HTTP_HX_HISTORY_RESTORE_REQUEST='true',
        )
        self.assertEqual(restore_resp.status_code, 200)
        restore_vary = [v.strip() for v in restore_resp.headers.get('Vary', '').split(',')]
        self.assertIn('HX-Request', restore_vary)
        restore_html = restore_resp.content.decode('utf-8')
        self.assertIn('<!DOCTYPE html>', restore_html)
        self.assertNotIn('id="total-dishes" hx-swap-oob="true"', restore_html)

    def test_catalog_pagination_htmx_and_quick_view_modal_controls(self):
        for idx in range(13):
            Dish.objects.create(
                category=self.category_pizza,
                title=f'Пагінація Піца {idx:02d}',
                slug=f'pagination-pizza-{idx:02d}',
                price=Decimal('210.00'),
                weight_grams=400,
                is_available=True,
            )
        response = self.client.get(reverse('menu:catalog'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('hx-get="?page=2"', content)
        self.assertIn('hx-target="#dish-grid"', content)
        self.assertIn('hx-push-url="true"', content)

        self.assertIn('modalSelectedOptionId', content)
        self.assertIn('modalQuantity', content)
        self.assertIn('decrementModalQty()', content)
        self.assertIn('incrementModalQty()', content)
        self.assertIn("document.body.classList.add('overflow-hidden')", content)
        self.assertIn('window.__lenis', content)
        self.assertIn('trapFocus($event)', content)
        self.assertIn('htmx:afterRequest', content)

    def test_dish_card_canonical_url_and_single_image_url_check(self):
        Dish.objects.filter(pk=self.dish_margarita.pk).update(image='dishes/card-cache-test.jpg')
        storage = Dish._meta.get_field('image').storage
        with (
            patch.object(storage, 'exists', return_value=True) as mock_exists,
            patch.object(storage, 'url', return_value='/media/dishes/card-cache-test.webp'),
        ):
            response = self.client.get(reverse('menu:catalog'), {'q': 'Маргарита'})

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertEqual(mock_exists.call_count, 1)
        self.assertIn(f'href="{self.dish_margarita.get_absolute_url()}"', content)
        self.assertIn('/media/dishes/card-cache-test.webp', content)
        self.assertIn("name: 'Велика (40 см)'", content)
        self.assertIn('opener: $el', content)

    def test_base_html_cart_button_grid_overlay_footer_and_newsletter(self):
        with override_settings(DEBUG=False):
            prod_resp = self.client.get(reverse('menu:catalog'))
        self.assertEqual(prod_resp.status_code, 200)
        prod_html = prod_resp.content.decode('utf-8')

        self.assertEqual(prod_html.count('@click="$dispatch(\'open-cart-drawer\')"'), 2)
        self.assertNotIn("onclick=\"window.dispatchEvent(new CustomEvent('open-cart-drawer'))\"", prod_html)
        self.assertNotIn('id="grid-overlay"', prod_html)
        self.assertNotIn('data-toggle-grid', prod_html)
        self.assertNotIn('function toggleGrid()', prod_html)

        self.assertIn('href="/menu/?category=burgers"', prod_html)
        self.assertIn('href="/menu/?category=pizza"', prod_html)
        self.assertIn('href="/menu/?category=bowls-salads"', prod_html)
        self.assertIn('href="/menu/?category=desserts"', prod_html)
        self.assertNotIn('alert(', prod_html)
        self.assertIn('window.showToast', prod_html)

        with override_settings(DEBUG=True):
            debug_resp = self.client.get(reverse('menu:catalog'))
        debug_html = debug_resp.content.decode('utf-8')
        self.assertIn('id="grid-overlay"', debug_html)
        self.assertIn('data-toggle-grid', debug_html)
        self.assertIn('function toggleGrid()', debug_html)

    def test_no_invalid_tailwind_v3_classes_in_templates_and_css_present(self):
        templates_dir = Path(settings.BASE_DIR) / 'templates'
        for html_file in templates_dir.rglob('*.html'):
            text = html_file.read_text(encoding='utf-8')
            self.assertNotIn('h-4.5', text, f'Found h-4.5 in {html_file}')
            self.assertNotIn('backdrop-blur-xs', text, f'Found backdrop-blur-xs in {html_file}')

        tailwind_css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'tailwind.css').read_text(
            encoding='utf-8'
        )
        for token in ('.db-select-arrow', '.h-\\[18px\\]', '.backdrop-blur-sm', '.overflow-hidden'):
            self.assertIn(token, tailwind_css)

    def test_dish_detail_json_ld_special_chars_and_negative_and_base_options(self):
        special_dish = Dish.objects.create(
            category=self.category_pizza,
            title='Піца "Маргарита" & Сир <50%>',
            slug='special-chars-pizza',
            description='Опис з "лапками", \'апострофом\', & амперсандом та </script> тегом',
            price=Decimal('280.00'),
            weight_grams=440,
            calories=790,
            is_available=True,
        )
        DishOption.objects.create(
            dish=special_dish,
            name='Без соусу (легка)',
            price_delta=Decimal('-25.00'),
        )
        DishOption.objects.create(
            dish=special_dish,
            name='Подвійний пармезан',
            price_delta=Decimal('60.00'),
        )

        response = self.client.get(special_dish.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')

        match = re.search(
            r'<script id="dish-ld-json" type="application/json">(.*?)</script>',
            content,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        parsed = json.loads(match.group(1).strip())
        self.assertEqual(parsed['name'], special_dish.title)
        self.assertEqual(parsed['description'], special_dish.description)
        self.assertEqual(parsed['offers']['price'], '280.00')

        self.assertIn("selectedOptionId: ''", content)
        self.assertIn("@click=\"setOption('', '0')\"", content)
        self.assertIn('Стандарт', content)
        self.assertIn('Базова', content)
        self.assertIn('-25.00 грн', content)
        self.assertNotIn('+-25.00 грн', content)
        self.assertIn('+60.00 грн', content)

    @override_settings(
        FREE_DELIVERY_THRESHOLD=Decimal('500.00'),
        DELIVERY_FEE=Decimal('65.00'),
    )
    def test_cart_drawer_and_cart_page_render_delivery_and_canonical_dish_url(self):
        self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish_margarita.id}),
            {'quantity': 1},
        )
        drawer_resp = self.client.get(reverse('orders:cart_drawer'), HTTP_HX_REQUEST='true')
        self.assertEqual(drawer_resp.status_code, 200)
        drawer_html = drawer_resp.content.decode('utf-8')
        self.assertIn(f'href="{self.dish_margarita.get_absolute_url()}"', drawer_html)
        self.assertIn('65.00 грн', drawer_html)

        cart_resp = self.client.get(reverse('orders:cart'))
        self.assertEqual(cart_resp.status_code, 200)
        cart_html = cart_resp.content.decode('utf-8')
        self.assertIn(f'href="{self.dish_margarita.get_absolute_url()}"', cart_html)
        self.assertIn('65.00 грн', cart_html)

        self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish_margarita.id}),
            {'quantity': 1},
        )
        free_drawer = self.client.get(reverse('orders:cart_drawer'), HTTP_HX_REQUEST='true')
        self.assertIn('Безкоштовно', free_drawer.content.decode('utf-8'))
        free_cart = self.client.get(reverse('orders:cart'))
        self.assertIn('Безкоштовно', free_cart.content.decode('utf-8'))

