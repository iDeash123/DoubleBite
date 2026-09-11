from decimal import Decimal

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from menu.models import Category, Dish

from orders.admin import CartAdmin, CartItemInline, OrderAdmin, OrderItemInline
from orders.models import Cart, CartItem, Order, OrderItem

User = get_user_model()


class OrdersAdminTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.superuser = User.objects.create_superuser(
            email='admin@doublebite.ua',
            password='AdminPassword123!',
        )
        self.category = Category.objects.create(
            name='Піца',
            slug='pizza-admin',
            is_active=True,
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Пепероні',
            slug='pepperoni-admin',
            price=Decimal('270.00'),
            weight_grams=450,
            is_available=True,
        )
        self.order = Order.objects.create(
            user=self.superuser,
            customer_name='Адміністратор',
            customer_phone='+380501112233',
            delivery_address='Київ, вул. Велика Васильківська, 1',
            total_amount=Decimal('540.00'),
        )
        self.order_item = OrderItem.objects.create(
            order=self.order,
            dish=self.dish,
            dish_title='Пепероні',
            price=Decimal('270.00'),
            quantity=2,
        )
        self.cart = Cart.objects.create(user=self.superuser)
        self.cart_item = CartItem.objects.create(
            cart=self.cart,
            dish=self.dish,
            quantity=1,
        )

    def test_admin_registration(self):
        self.assertIn(Order, site._registry)
        self.assertIn(Cart, site._registry)
        self.assertIsInstance(site._registry[Order], OrderAdmin)
        self.assertIsInstance(site._registry[Cart], CartAdmin)

    def test_order_change_view_renders_successfully(self):
        self.client.force_login(self.superuser)
        url = reverse('admin:orders_order_change', args=[self.order.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.order.order_number)
        self.assertContains(response, '540.00')

    def test_order_change_view_with_extra_inline_row(self):
        self.client.force_login(self.superuser)
        original_extra = OrderItemInline.extra
        OrderItemInline.extra = 1
        try:
            url = reverse('admin:orders_order_change', args=[self.order.pk])
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, '0.00')
        finally:
            OrderItemInline.extra = original_extra

    def test_cart_change_view_renders_successfully(self):
        self.client.force_login(self.superuser)
        url = reverse('admin:orders_cart_change', args=[self.cart.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_cart_change_view_with_extra_inline_row(self):
        self.client.force_login(self.superuser)
        original_extra = CartItemInline.extra
        CartItemInline.extra = 1
        try:
            url = reverse('admin:orders_cart_change', args=[self.cart.pk])
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
        finally:
            CartItemInline.extra = original_extra

    def test_order_add_view_renders_successfully(self):
        self.client.force_login(self.superuser)
        url = reverse('admin:orders_order_add')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_cart_add_view_renders_successfully(self):
        self.client.force_login(self.superuser)
        url = reverse('admin:orders_cart_add')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
