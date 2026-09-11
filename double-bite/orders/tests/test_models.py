from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from menu.models import Category, Dish
from orders.exceptions import InvalidStatusTransitionError
from orders.models import (
    Cart,
    CartItem,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)

User = get_user_model()


class OrdersModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='client@doublebite.ua',
            password='StrongPassword123!',
            first_name='Олексій',
            last_name='Коваленко',
        )

        self.category = Category.objects.create(
            name='Піца',
            slug='pizza',
            display_order=1,
            is_active=True,
        )
        self.category_inactive = Category.objects.create(
            name='Напої сезонні',
            slug='seasonal-drinks',
            is_active=False,
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
            title='Чотири Сири',
            slug='four-cheeses',
            price=Decimal('320.00'),
            weight_grams=480,
            is_available=True,
        )
        self.dish_unavailable = Dish.objects.create(
            category=self.category,
            title='Кальцоне',
            slug='calzone',
            price=Decimal('280.00'),
            weight_grams=400,
            is_available=False,
        )
        self.dish_inactive_category = Dish.objects.create(
            category=self.category_inactive,
            title='Морс',
            slug='mors',
            price=Decimal('60.00'),
            weight_grams=250,
            is_available=True,
        )

    def test_cart_creation_for_user_and_guest(self):
        user_cart = Cart.objects.create(user=self.user)
        self.assertIn('client@doublebite.ua', str(user_cart))
        self.assertIsNone(user_cart.session_key or None)

        guest_cart = Cart.objects.create(session_key='guest_session_1234567890')
        self.assertIn('Гість', str(guest_cart))
        self.assertIsNone(guest_cart.user)

    def test_cart_item_unit_and_total_price_with_options(self):
        cart = Cart.objects.create(user=self.user)
        item = CartItem.objects.create(
            cart=cart,
            dish=self.dish1,
            quantity=2,
            selected_options=[
                {'name': 'Подвійний сир', 'price_delta': '40.00'},
                {'name': 'Бортик з філадельфією', 'price_delta': '30.00'},
            ],
        )

        self.assertEqual(item.unit_price, Decimal('320.00'))
        self.assertEqual(item.total_price, Decimal('640.00'))
        self.assertTrue(item.is_available)
        self.assertIn('Маргарита x 2', str(item))

    def test_cart_total_quantity_and_amount_excludes_unavailable(self):
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, dish=self.dish1, quantity=2)
        CartItem.objects.create(cart=cart, dish=self.dish2, quantity=1)
        CartItem.objects.create(cart=cart, dish=self.dish_unavailable, quantity=3)
        CartItem.objects.create(cart=cart, dish=self.dish_inactive_category, quantity=1)

        self.assertEqual(cart.total_quantity, 3)
        self.assertEqual(cart.total_amount, Decimal('820.00'))

    def test_order_number_auto_generation(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Олексій',
            customer_phone='+380501112233',
            delivery_address='вул. Хрещатик, 1, кв. 10',
            total_amount=Decimal('500.00'),
        )
        self.assertTrue(order.order_number.startswith('DB-'))
        self.assertEqual(order.order_number, f"DB-{order.pk:04d}")
        self.assertEqual(order.status, OrderStatus.PENDING)
        self.assertEqual(order.payment_status, PaymentStatus.PENDING)
        self.assertEqual(order.payment_method, PaymentMethod.CARD)
        self.assertIn(order.order_number, str(order))

    def test_state_machine_valid_linear_transitions(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Олексій',
            customer_phone='+380501112233',
            delivery_address='Київ, вул. Франка, 12',
            total_amount=Decimal('400.00'),
        )

        self.assertTrue(order.can_transition_to(OrderStatus.PAID))
        order.transition_to(OrderStatus.PAID)
        self.assertEqual(order.status, OrderStatus.PAID)
        self.assertEqual(order.payment_status, PaymentStatus.COMPLETED)

        self.assertTrue(order.can_transition_to(OrderStatus.PREPARING))
        order.transition_to(OrderStatus.PREPARING)
        self.assertEqual(order.status, OrderStatus.PREPARING)

        self.assertTrue(order.can_transition_to(OrderStatus.ON_WAY))
        order.transition_to(OrderStatus.ON_WAY)
        self.assertEqual(order.status, OrderStatus.ON_WAY)

        self.assertTrue(order.can_transition_to(OrderStatus.DELIVERED))
        order.transition_to(OrderStatus.DELIVERED)
        self.assertEqual(order.status, OrderStatus.DELIVERED)

    def test_state_machine_valid_cancellations(self):
        order1 = Order.objects.create(
            user=self.user,
            customer_name='Тест',
            customer_phone='+380501112233',
            delivery_address='Київ',
        )
        self.assertTrue(order1.can_transition_to(OrderStatus.CANCELLED))
        order1.transition_to(OrderStatus.CANCELLED)
        self.assertEqual(order1.status, OrderStatus.CANCELLED)

        order2 = Order.objects.create(
            user=self.user,
            customer_name='Тест 2',
            customer_phone='+380501112233',
            delivery_address='Київ',
            status=OrderStatus.PAID,
        )
        self.assertTrue(order2.can_transition_to(OrderStatus.CANCELLED))
        order2.transition_to(OrderStatus.CANCELLED)
        self.assertEqual(order2.status, OrderStatus.CANCELLED)

    def test_state_machine_invalid_transitions_raise_error(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Тест',
            customer_phone='+380501112233',
            delivery_address='Київ',
            status=OrderStatus.PENDING,
        )
        with self.assertRaises(InvalidStatusTransitionError):
            order.transition_to(OrderStatus.DELIVERED)

        order.status = OrderStatus.PREPARING
        order.save()
        with self.assertRaises(InvalidStatusTransitionError):
            order.transition_to(OrderStatus.CANCELLED)

        order.status = OrderStatus.DELIVERED
        order.save()
        with self.assertRaises(InvalidStatusTransitionError):
            order.transition_to(OrderStatus.PAID)

        order.status = OrderStatus.CANCELLED
        order.save()
        with self.assertRaises(InvalidStatusTransitionError):
            order.transition_to(OrderStatus.ON_WAY)

    def test_order_item_snapshot(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Олексій',
            customer_phone='+380501112233',
            delivery_address='Київ',
        )
        order_item = OrderItem.objects.create(
            order=order,
            dish=self.dish1,
            dish_title='Маргарита Класична Спеціальна',
            price=Decimal('299.00'),
            quantity=2,
            selected_options=[{'name': '40 см', 'price_delta': '50.00'}],
        )

        self.dish1.price = Decimal('999.00')
        self.dish1.title = 'Змінена назва'
        self.dish1.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.dish_title, 'Маргарита Класична Спеціальна')
        self.assertEqual(order_item.price, Decimal('299.00'))
        self.assertEqual(order_item.total_price, Decimal('598.00'))
        self.assertIn('299.00 грн', str(order_item))
