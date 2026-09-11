from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from menu.models import Category, Dish
from orders.models import Cart, CartItem

User = get_user_model()


class CartViewsTest(TestCase):
    def setUp(self):
        self.client = Client()

        self.category = Category.objects.create(
            name='Піца',
            slug='pizza',
            is_active=True,
        )
        self.dish1 = Dish.objects.create(
            category=self.category,
            title='Маргарита',
            slug='margarita',
            price=Decimal('250.00'),
            weight_grams=450,
            is_available=True,
        )
        self.dish2 = Dish.objects.create(
            category=self.category,
            title='Діавола',
            slug='diavola',
            price=Decimal('310.00'),
            weight_grams=480,
            is_available=True,
        )

    def test_cart_view_empty_cart(self):
        response = self.client.get(reverse('orders:cart'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'orders/cart.html')
        self.assertIn('Ваш кошик порожній', response.content.decode('utf-8'))

    def test_cart_add_htmx_returns_badge_partial(self):
        url = reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id})
        response = self.client.post(
            url,
            {'quantity': '2'},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="cart-badge"', content)
        self.assertIn('2', content)
        self.assertNotIn('<!DOCTYPE html>', content)

    def test_cart_direct_add_endpoint(self):
        url = reverse('cart_add_direct', kwargs={'dish_id': self.dish2.id})
        response = self.client.post(
            url,
            {'quantity': '1'},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="cart-badge"', content)

    def test_cart_update_and_remove_htmx_returns_cart_partial(self):
        # Add item first
        self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}),
            {'quantity': '2'},
            HTTP_HX_REQUEST='true',
        )

        session_key = self.client.session.session_key
        cart = Cart.objects.get(session_key=session_key)
        item = cart.items.first()

        # Update quantity
        update_url = reverse('orders:cart_update', kwargs={'item_id': item.id})
        response = self.client.post(
            update_url,
            {'quantity': '3'},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="cart-content"', content)
        self.assertIn('Маргарита', content)
        self.assertIn('3', content)

        # Remove item
        remove_url = reverse('orders:cart_remove', kwargs={'item_id': item.id})
        response = self.client.post(
            remove_url,
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="cart-content"', content)
        self.assertIn('Ваш кошик порожній', content)

    def test_cart_clear(self):
        self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}),
            {'quantity': '2'},
        )
        self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish2.id}),
            {'quantity': '1'},
        )

        response = self.client.post(reverse('orders:cart_clear'), HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Ваш кошик порожній', content)
