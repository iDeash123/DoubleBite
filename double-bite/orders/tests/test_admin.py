from decimal import Decimal
from unittest.mock import patch

import stripe
from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import connection
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from menu.models import Category, Dish

from orders.admin import CartAdmin, CartItemInline, OrderAdmin, OrderItemInline
from orders.models import Cart, CartItem, Order, OrderItem, OrderStatus, PaymentMethod, PaymentStatus

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
        cart_admin = site._registry[Cart]
        order_admin = site._registry[Order]
        self.assertNotIn('guest_token', cart_admin.list_display)
        self.assertNotIn('guest_token', cart_admin.search_fields)
        self.assertIn('guest_token', cart_admin.exclude)
        self.assertNotIn('guest_token', order_admin.list_display)
        self.assertNotIn('guest_token', order_admin.search_fields)
        self.assertIn('guest_token', order_admin.exclude)
        self.assertIn('customer_email', order_admin.search_fields)
        self.assertIn('access_token', order_admin.exclude)
        self.assertIn('status', order_admin.readonly_fields)
        self.assertIn('payment_status', order_admin.readonly_fields)

        inline = OrderItemInline(Order, site)
        self.assertFalse(inline.has_add_permission(None))
        self.assertFalse(inline.has_add_permission(None, obj=self.order))

    def test_order_admin_transition_actions(self):
        order_admin = site._registry[Order]
        from orders.models import OrderStatus

        queryset = Order.objects.filter(pk=self.order.pk)
        order_admin.action_mark_paid(None, queryset)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAID)

        order_admin.action_mark_preparing(None, queryset)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PREPARING)

        order_admin.action_mark_on_way(None, queryset)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.ON_WAY)

        order_admin.action_mark_delivered(None, queryset)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.DELIVERED)

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

    def _build_admin_request(self):
        request = RequestFactory().post('/admin/')
        request.user = self.superuser
        SessionMiddleware(lambda req: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda req: None).process_request(request)
        return request

    @override_settings(STRIPE_SECRET_KEY='sk_test_admin_refund')
    @patch('stripe.Refund.create')
    def test_order_admin_action_mark_cancelled_triggers_stripe_refund(self, mock_refund_create):
        paid_order = Order.objects.create(
            user=self.superuser,
            customer_name='Оплата Онлайн',
            customer_phone='+380501112233',
            delivery_address='Київ, вул. Тестова, 1',
            payment_method=PaymentMethod.ONLINE,
            status=OrderStatus.PAID,
            payment_status=PaymentStatus.PAID,
            stripe_payment_intent_id='pi_admin_refund_123',
            total_amount=Decimal('540.00'),
        )
        order_admin = site._registry[Order]
        request = self._build_admin_request()

        order_admin.action_mark_cancelled(request, Order.objects.filter(pk=paid_order.pk))

        mock_refund_create.assert_called_once_with(
            payment_intent='pi_admin_refund_123',
            idempotency_key=f'refund-order-{paid_order.pk}',
            api_key='sk_test_admin_refund',
        )
        paid_order.refresh_from_db()
        self.assertEqual(paid_order.status, OrderStatus.CANCELLED)
        self.assertEqual(paid_order.payment_status, PaymentStatus.REFUNDED)
        self.assertFalse(paid_order.requires_manual_refund)

    @override_settings(STRIPE_SECRET_KEY='sk_test_admin_refund')
    @patch('stripe.Refund.create')
    def test_order_admin_action_mark_cancelled_handles_manual_refund_and_invalid_transition(
        self,
        mock_refund_create,
    ):
        mock_refund_create.side_effect = stripe.StripeError('API timeout')
        paid_order = Order.objects.create(
            user=self.superuser,
            customer_name='Помилка повернення',
            customer_phone='+380501112233',
            delivery_address='Київ, вул. Тестова, 2',
            payment_method=PaymentMethod.ONLINE,
            status=OrderStatus.PAID,
            payment_status=PaymentStatus.PAID,
            stripe_payment_intent_id='pi_admin_fail_456',
            total_amount=Decimal('300.00'),
        )
        delivered_order = Order.objects.create(
            user=self.superuser,
            customer_name='Вже доставлено',
            customer_phone='+380501112234',
            delivery_address='Київ, вул. Тестова, 3',
            payment_method=PaymentMethod.CASH,
            status=OrderStatus.DELIVERED,
            payment_status=PaymentStatus.PAID,
            total_amount=Decimal('300.00'),
        )
        order_admin = site._registry[Order]
        request = self._build_admin_request()

        order_admin.action_mark_cancelled(
            request,
            Order.objects.filter(pk__in=[paid_order.pk, delivered_order.pk]),
        )

        paid_order.refresh_from_db()
        delivered_order.refresh_from_db()
        self.assertEqual(paid_order.status, OrderStatus.CANCELLED)
        self.assertTrue(paid_order.requires_manual_refund)
        self.assertEqual(delivered_order.status, OrderStatus.DELIVERED)

        msgs = [m.message for m in request._messages]
        self.assertTrue(any('Успішно скасовано 1 замовлень' in m for m in msgs))
        self.assertTrue(any('Не вдалося скасувати 1 замовлень' in m for m in msgs))
        self.assertTrue(any('Потрібне ручне повернення коштів' in m for m in msgs))

    def test_cart_admin_changelist_no_2n_plus_1_queries(self):
        self.client.force_login(self.superuser)
        changelist_url = reverse('admin:orders_cart_changelist')

        with CaptureQueriesContext(connection) as ctx_one:
            resp_one = self.client.get(changelist_url)
        self.assertEqual(resp_one.status_code, 200)

        extra_cat = Category.objects.create(name='Напої', slug='drinks-admin', is_active=True)
        extra_dish = Dish.objects.create(
            category=extra_cat,
            title='Лимонад',
            slug='lemonade-admin',
            price=Decimal('90.00'),
            weight_grams=300,
            is_available=True,
        )
        other_user = User.objects.create_user(
            email='second.cart@doublebite.ua',
            password='StrongPassword123!',
        )
        user_cart = Cart.objects.create(user=other_user)
        CartItem.objects.create(cart=user_cart, dish=self.dish, quantity=2)
        CartItem.objects.create(cart=user_cart, dish=extra_dish, quantity=1)
        for idx in range(4):
            guest_cart = Cart.objects.create(guest_token=f'admin-guest-token-{idx}')
            CartItem.objects.create(cart=guest_cart, dish=self.dish, quantity=idx + 1)
            CartItem.objects.create(cart=guest_cart, dish=extra_dish, quantity=2)

        with CaptureQueriesContext(connection) as ctx_many:
            resp_many = self.client.get(changelist_url)
        self.assertEqual(resp_many.status_code, 200)
        self.assertEqual(len(ctx_many), len(ctx_one))
