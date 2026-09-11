from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Role
from menu.models import Category, Dish
from orders.models import Cart, CartItem, Order

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

    def test_cart_clear_direct(self):
        response = self.client.post(reverse('cart_clear_direct'), HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)

    def test_cart_drawer_view(self):
        response = self.client.get(reverse('orders:cart_drawer'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'partials/cart_drawer.html')

        direct_resp = self.client.get(reverse('cart_drawer_direct'))
        self.assertEqual(direct_resp.status_code, 200)

        # Drawer view with HTMX returns partial container
        htmx_resp = self.client.get(reverse('orders:cart_drawer'), HTTP_HX_REQUEST='true')
        self.assertEqual(htmx_resp.status_code, 200)
        self.assertIn('id="cart-drawer-container"', htmx_resp.content.decode('utf-8'))

    def test_cart_update_and_remove_from_drawer(self):
        # Add item
        self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}),
            {'quantity': '1'},
            HTTP_HX_REQUEST='true',
        )
        session_key = self.client.session.session_key
        cart = Cart.objects.get(session_key=session_key)
        item = cart.items.first()

        # Update from drawer
        update_url = reverse('orders:cart_update', kwargs={'item_id': item.id})
        response = self.client.post(
            update_url,
            {'quantity': '4'},
            HTTP_HX_REQUEST='true',
            HTTP_HX_TARGET='cart-drawer-container',
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="cart-drawer-container"', content)
        self.assertIn('4', content)
        self.assertIn('id="cart-badge"', content)

        # Remove from drawer
        remove_url = reverse('orders:cart_remove', kwargs={'item_id': item.id})
        response = self.client.post(
            remove_url,
            HTTP_HX_REQUEST='true',
            HTTP_HX_TARGET='cart-drawer-container',
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="cart-drawer-container"', content)
        self.assertIn('Кошик порожній', content)


    def test_cart_add_unavailable_dish_returns_400(self):
        unavailable_dish = Dish.objects.create(
            category=self.category,
            title='Недоступна піца',
            slug='unavailable-pizza',
            price=Decimal('280.00'),
            weight_grams=400,
            is_available=False,
        )
        url = reverse('orders:cart_add', kwargs={'dish_id': unavailable_dish.id})
        response = self.client.post(url, {'quantity': '1'}, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 400)
        self.assertIn('недоступна', response.content.decode('utf-8'))

    def test_cart_add_staff_forbidden(self):
        admin_user = User.objects.create_user(
            email='admin@test.com',
            password='adminpassword123',
            role=Role.RESTAURANT_ADMIN,
        )
        self.client.force_login(admin_user)
        url = reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id})
        response = self.client.post(url, {'quantity': '1'}, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 403)

    def test_order_list_view_permissions(self):
        url = reverse('orders:order_list')

        # Anonymous user gets redirected to login
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

        # Customer gets 200
        customer = User.objects.create_user(
            email='customer@test.com',
            password='customerpassword123',
            role=Role.CUSTOMER,
        )
        self.client.force_login(customer)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'orders/order_list.html')

        # Admin gets 403
        admin_user = User.objects.create_user(
            email='admin_staff@test.com',
            password='adminpassword123',
            role=Role.RESTAURANT_ADMIN,
        )
        self.client.force_login(admin_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

