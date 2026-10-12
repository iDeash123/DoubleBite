from decimal import Decimal

from django.conf import settings
from django.test import RequestFactory, TestCase
from django.utils.functional import SimpleLazyObject
from menu.models import Category, Dish

from orders.context_processors import cart_context
from orders.models import Cart, CartItem, Order, PaymentMethod
from orders.services import CartService, CartSummary


class RefactoringAndCodeQualityTest(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='Категорія', slug='category-test')
        self.dish = Dish.objects.create(
            category=self.category,
            title='Бургер',
            slug='burger-test',
            price=Decimal('250.00'),
            weight_grams=350,
            is_available=True,
        )

    def test_settings_min_order_amount_defined(self):
        self.assertTrue(hasattr(settings, 'MIN_ORDER_AMOUNT'))
        self.assertIsInstance(settings.MIN_ORDER_AMOUNT, Decimal)

    def test_payment_method_is_online_helper(self):
        self.assertTrue(PaymentMethod.is_online(PaymentMethod.ONLINE))
        self.assertTrue(PaymentMethod.is_online(PaymentMethod.STRIPE))
        self.assertTrue(PaymentMethod.is_online('ONLINE'))
        self.assertTrue(PaymentMethod.is_online('STRIPE'))
        self.assertFalse(PaymentMethod.is_online(PaymentMethod.CARD))
        self.assertFalse(PaymentMethod.is_online(PaymentMethod.CASH))
        self.assertFalse(PaymentMethod.is_online(None))

    def test_order_is_online_payment_property(self):
        order_online = Order(payment_method=PaymentMethod.ONLINE)
        self.assertTrue(order_online.is_online_payment)
        order_stripe = Order(payment_method=PaymentMethod.STRIPE)
        self.assertTrue(order_stripe.is_online_payment)
        order_card = Order(payment_method=PaymentMethod.CARD)
        self.assertFalse(order_card.is_online_payment)

    def test_cart_service_summary_empty(self):
        summary = CartService.summary(None)
        self.assertIsInstance(summary, CartSummary)
        self.assertEqual(summary.total_amount, Decimal('0.00'))
        self.assertEqual(summary.total_quantity, 0)
        self.assertFalse(summary.meets_min_amount)

    def test_cart_service_summary_with_items(self):
        cart = Cart.objects.create()
        CartItem.objects.create(cart=cart, dish=self.dish, quantity=2)
        summary = CartService.summary(cart)
        self.assertEqual(summary.total_amount, Decimal('500.00'))
        self.assertEqual(summary.total_quantity, 2)
        self.assertTrue(summary.meets_min_amount)

    def test_lazy_cart_context_processor(self):
        factory = RequestFactory()
        request = factory.get('/')
        ctx = cart_context(request)
        self.assertIn('cart', ctx)
        self.assertIsInstance(ctx['cart'], SimpleLazyObject)
        self.assertIsInstance(ctx['items'], SimpleLazyObject)
        self.assertIsInstance(ctx['total_amount'], SimpleLazyObject)
        self.assertEqual(ctx['total_quantity'], 0)
