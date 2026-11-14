from decimal import Decimal

from django.template import TemplateDoesNotExist
from django.test import RequestFactory, TestCase
from django.urls import reverse
from menu.models import Category, Dish
from orders.models import Order

from config.partials import render_partial_or_full


class NativePartialsTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.category = Category.objects.create(name='Бургери', slug='burgers')
        self.dish = Dish.objects.create(
            category=self.category,
            title='Truffle Burger',
            slug='truffle-burger',
            description='Опис страви',
            price=Decimal('320.00'),
            weight_grams=380,
            is_available=True,
        )

    def test_render_partial_or_full_renders_native_django6_partial(self):
        request = self.factory.get('/menu/')
        response = render_partial_or_full(
            request,
            'menu/catalog.html#dish-grid',
            {'dishes': [self.dish], 'page_obj': None},
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Truffle Burger', content)
        self.assertNotIn('<!DOCTYPE html>', content)

    def test_render_partial_or_full_raises_on_unknown_partial(self):
        request = self.factory.get('/orders/tracking/')
        with self.assertRaises(TemplateDoesNotExist):
            render_partial_or_full(
                request,
                'orders/tracking.html#nonexistent-partial-name',
                {},
            )

    def test_tracking_status_view_without_hx_request_returns_partial_only(self):
        order = Order.objects.create(
            customer_name='Тест Клієнт',
            customer_phone='+380501234567',
            delivery_address='Київ, вул. Тестова, 1',
            total_amount=Decimal('320.00'),
        )
        url = reverse('orders:tracking_status', kwargs={'order_number': order.order_number})
        response = self.client.get(f'{url}?t={order.access_token}')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertNotIn('<!DOCTYPE html>', content)
        self.assertIn(order.order_number, content)

    def test_catalog_history_restore_renders_full_page(self):
        response = self.client.get(
            reverse('menu:catalog'),
            HTTP_HX_REQUEST='true',
            HTTP_HX_HISTORY_RESTORE_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('<!DOCTYPE html>', response.content.decode('utf-8'))
