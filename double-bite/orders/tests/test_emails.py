from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import RequestFactory, TestCase
from menu.models import Category, Dish

from orders.emails import send_order_confirmation_email
from orders.models import Cart, CartItem, Order, OrderStatus, PaymentMethod
from orders.services import OrderService

User = get_user_model()


class OrderEmailNotificationTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            email='orderclient@example.com',
            password='TestPassword123!',
            first_name='Тарас',
            last_name='Шевченко',
            phone='+380501234567',
        )
        self.category = Category.objects.create(
            name='Піца',
            slug='pizza',
            display_order=1,
            is_active=True,
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Піца Маргарита Екстра',
            slug='pizza-margherita-extra',
            price=Decimal('250.00'),
            weight_grams=500,
            calories=800,
            is_available=True,
        )
        mail.outbox.clear()

    def _setup_cart_with_items(self, user=None):
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart,
            dish=self.dish,
            quantity=2,
            selected_options=[{'name': 'Подвійний сир', 'price_delta': 30}],
        )
        return cart

    def test_order_confirmation_email_sent_on_checkout(self):
        cart = self._setup_cart_with_items(user=self.user)
        request = self.factory.post('/orders/checkout/')
        request.user = self.user
        request.session = {'session_key': 'test-session-key'}
        request._cached_cart = cart

        order = OrderService.create_order_from_cart(
            request=request,
            customer_name='Тарас Шевченко',
            customer_phone='+380501234567',
            delivery_address='Київ, вул. Франка, 10, кв. 5',
            payment_method=PaymentMethod.CASH,
            notes='Не турбувати',
        )

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, [self.user.email])
        self.assertIn(f'#{order.order_number}', sent.subject)
        self.assertIn('підтверджено', sent.subject)
        self.assertIn(order.order_number, sent.body)
        self.assertIn('Маргарита', sent.body)
        self.assertIn('560', sent.body)
        self.assertIn('~30 хв', sent.body)
        self.assertIn(f'/orders/tracking/{order.order_number}/', sent.body)

        self.assertEqual(len(sent.alternatives), 1)
        self.assertEqual(sent.alternatives[0][1], 'text/html')
        html = sent.alternatives[0][0]
        self.assertIn('#F6F6F6', html)
        self.assertIn('#262626', html)
        self.assertIn('JetBrains Mono', html)
        self.assertIn(order.order_number, html)

    def test_order_status_preparing_email_notification(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Тарас Шевченко',
            customer_phone='+380501234567',
            delivery_address='Київ, вул. Франка, 10',
            status=OrderStatus.PAID,
            total_amount=Decimal('500.00'),
        )
        mail.outbox.clear()

        order.transition_to(OrderStatus.PREPARING)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, [self.user.email])
        self.assertIn(order.order_number, sent.subject)
        self.assertIn('Готується', sent.subject)
        self.assertIn(f'/orders/tracking/{order.order_number}/', sent.body)

    def test_order_status_on_way_email_notification(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Тарас Шевченко',
            customer_phone='+380501234567',
            delivery_address='Київ, вул. Франка, 10',
            status=OrderStatus.PREPARING,
            total_amount=Decimal('500.00'),
        )
        mail.outbox.clear()

        order.transition_to(OrderStatus.ON_WAY)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, [self.user.email])
        self.assertIn('Кур\'єр', sent.subject)

    def test_order_status_delivered_email_notification(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Тарас Шевченко',
            customer_phone='+380501234567',
            delivery_address='Київ, вул. Франка, 10',
            status=OrderStatus.ON_WAY,
            total_amount=Decimal('500.00'),
        )
        mail.outbox.clear()

        order.transition_to(OrderStatus.DELIVERED)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, [self.user.email])
        self.assertIn('Доставлено', sent.subject)

    def test_order_status_cancelled_email_notification(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Тарас Шевченко',
            customer_phone='+380501234567',
            delivery_address='Київ, вул. Франка, 10',
            status=OrderStatus.PENDING,
            total_amount=Decimal('500.00'),
        )
        mail.outbox.clear()

        order.transition_to(OrderStatus.CANCELLED)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, [self.user.email])
        self.assertIn('Скасовано', sent.subject)

    def test_guest_order_without_email_fails_silently(self):
        order = Order.objects.create(
            user=None,
            customer_name='Анонімний гість',
            customer_phone='+380501234567',
            delivery_address='Київ, вул. Франка, 10',
            status=OrderStatus.PENDING,
            total_amount=Decimal('500.00'),
        )
        mail.outbox.clear()

        count = send_order_confirmation_email(order)
        self.assertEqual(count, 0)
        self.assertEqual(len(mail.outbox), 0)

        order.transition_to(OrderStatus.CANCELLED)
        self.assertEqual(len(mail.outbox), 0)
