from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
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
        self.assertEqual(user_cart.guest_token, '')

        guest_cart = Cart.objects.create(guest_token='guest_token_1234567890')
        self.assertIn('Гість', str(guest_cart))
        self.assertIn('guest_to', str(guest_cart))
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
        self.assertEqual(order.payment_method, PaymentMethod.ONLINE)
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
        self.assertEqual(order.payment_status, PaymentStatus.PAID)

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

    def test_order_item_total_price_handles_none(self):
        item_empty = OrderItem()
        self.assertEqual(item_empty.total_price, Decimal('0.00'))

        item_none_price = OrderItem(price=None, quantity=2)
        self.assertEqual(item_none_price.total_price, Decimal('0.00'))

        item_none_quantity = OrderItem(price=Decimal('100.00'), quantity=None)
        self.assertEqual(item_none_quantity.total_price, Decimal('0.00'))

        item_both_none = OrderItem(price=None, quantity=None)
        self.assertEqual(item_both_none.total_price, Decimal('0.00'))

        self.assertIn('0.00 грн', str(item_empty))

    def test_cart_item_total_price_and_unit_price_handles_none(self):
        item_empty = CartItem()
        self.assertEqual(item_empty.unit_price, Decimal('0.00'))
        self.assertEqual(item_empty.total_price, Decimal('0.00'))
        self.assertFalse(item_empty.is_available)
        self.assertIn('Позиція кошика', str(item_empty))

        item_none_quantity = CartItem(dish=self.dish1, quantity=None)
        self.assertEqual(item_none_quantity.total_price, Decimal('0.00'))

    def test_unsaved_cart_properties_return_defaults(self):
        cart = Cart()
        self.assertEqual(cart.total_quantity, 0)
        self.assertEqual(cart.total_amount, Decimal('0.00'))

    def test_order_item_unit_price(self):
        item_empty = OrderItem()
        self.assertEqual(item_empty.unit_price, Decimal('0.00'))

        item_with_price = OrderItem(price=Decimal('150.00'))
        self.assertEqual(item_with_price.unit_price, Decimal('150.00'))

    def test_cart_item_unit_price_malformed_and_negative_options(self):
        item_malformed = CartItem(
            dish=self.dish1,
            selected_options=[
                {'price_delta': 'invalid_delta'},
                {'price_delta': None},
                {'name': 'no_delta'},
                'not_a_dict',
            ],
        )
        self.assertEqual(item_malformed.unit_price, Decimal('250.00'))

        item_negative_clamped = CartItem(
            dish=self.dish1,
            selected_options=[
                {'price_delta': '-500.00'},
            ],
        )
        self.assertEqual(item_negative_clamped.unit_price, Decimal('0.00'))

    def test_cart_item_unit_price_skips_non_finite_without_poisoning_valid_deltas(self):
        item = CartItem(
            dish=self.dish1,
            selected_options=[
                {'name': 'Сир', 'price_delta': '40.00'},
                {'name': 'NaN', 'price_delta': 'NaN'},
                {'name': 'sNaN', 'price_delta': 'sNaN'},
                {'name': 'Inf', 'price_delta': 'Infinity'},
                {'name': 'NegInf', 'price_delta': '-Infinity'},
                {'name': 'FloatNaN', 'price_delta': float('nan')},
                {'name': 'FloatInf', 'price_delta': float('inf')},
                {'name': 'BoolTrue', 'price_delta': True},
                {'name': 'BoolFalse', 'price_delta': False},
                {'name': 'NoneVal', 'price_delta': None},
                {'name': 'Соус', 'price_delta': '15.00'},
            ],
        )
        self.assertEqual(item.unit_price, Decimal('305.00'))

    def test_cash_and_card_transitions_allow_pending_to_preparing_and_mark_paid_on_delivered(self):
        for method in (PaymentMethod.CASH, PaymentMethod.CARD):
            order = Order.objects.create(
                user=self.user,
                customer_name='Олексій',
                customer_phone='+380501112233',
                delivery_address='Київ, вул. Франка, 12',
                payment_method=method,
                total_amount=Decimal('400.00'),
            )
            self.assertTrue(order.can_transition_to(OrderStatus.PREPARING))
            order.transition_to(OrderStatus.PREPARING)
            self.assertEqual(order.status, OrderStatus.PREPARING)
            self.assertEqual(order.payment_status, PaymentStatus.PENDING)

            order.transition_to(OrderStatus.ON_WAY)
            self.assertEqual(order.status, OrderStatus.ON_WAY)
            self.assertEqual(order.payment_status, PaymentStatus.PENDING)

            order.transition_to(OrderStatus.DELIVERED)
            self.assertEqual(order.status, OrderStatus.DELIVERED)
            self.assertEqual(order.payment_status, PaymentStatus.PAID)

    def test_online_order_rejects_pending_to_preparing(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Олексій',
            customer_phone='+380501112233',
            delivery_address='Київ, вул. Франка, 12',
            payment_method=PaymentMethod.ONLINE,
            total_amount=Decimal('400.00'),
        )
        self.assertFalse(order.can_transition_to(OrderStatus.PREPARING))
        with self.assertRaises(InvalidStatusTransitionError):
            order.transition_to(OrderStatus.PREPARING)

    def test_cart_unique_constraints_enforce_single_user_and_guest_token_cart(self):
        Cart.objects.create(user=self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Cart.objects.create(user=self.user)

        Cart.objects.create(user=None, guest_token='unique-guest-token-1')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Cart.objects.create(user=None, guest_token='unique-guest-token-1')

        empty_a = Cart.objects.create(user=None, guest_token='')
        empty_b = Cart.objects.create(user=None, guest_token='')
        self.assertNotEqual(empty_a.pk, empty_b.pk)

    def test_order_transition_to_schedules_email_only_on_commit(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Олексій',
            customer_phone='+380501112233',
            delivery_address='Київ',
            payment_method=PaymentMethod.CASH,
            status=OrderStatus.PENDING,
            total_amount=Decimal('300.00'),
        )
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    order.transition_to(OrderStatus.PREPARING)
                    raise RuntimeError('Simulated rollback')
            except RuntimeError:
                pass

        self.assertEqual(len(mail.outbox), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING)


class OrdersMigration0005Test(TransactionTestCase):
    def test_migration_0005_migrates_enums_tokens_and_deduplicates_carts(self):
        target_0004 = [('orders', '0004_order_requires_manual_refund_and_more')]
        target_0005 = [('orders', '0005_guest_token_payment_enums_and_constraints')]

        executor = MigrationExecutor(connection)
        executor.migrate(target_0004)
        try:
            old_apps = executor.loader.project_state(target_0004).apps
            OldUser = old_apps.get_model('accounts', 'User')
            OldCategory = old_apps.get_model('menu', 'Category')
            OldDish = old_apps.get_model('menu', 'Dish')
            OldCart = old_apps.get_model('orders', 'Cart')
            OldCartItem = old_apps.get_model('orders', 'CartItem')
            OldOrder = old_apps.get_model('orders', 'Order')

            user = OldUser.objects.create(email='mig0005@doublebite.ua', password='x')
            category = OldCategory.objects.create(name='Піца', slug='mig-pizza', is_active=True)
            dish_a = OldDish.objects.create(
                category=category,
                title='Маргарита',
                slug='mig-margarita',
                price=Decimal('250.00'),
                weight_grams=400,
            )
            dish_b = OldDish.objects.create(
                category=category,
                title='Пепероні',
                slug='mig-pepperoni',
                price=Decimal('280.00'),
                weight_grams=420,
            )

            user_cart_old = OldCart.objects.create(user=user, session_key='stale-user-sess')
            user_cart_new = OldCart.objects.create(user=user, session_key='newer-user-sess')
            OldCartItem.objects.create(cart=user_cart_old, dish=dish_a, quantity=60)
            OldCartItem.objects.create(cart=user_cart_old, dish=dish_b, quantity=2)
            OldCartItem.objects.create(cart=user_cart_new, dish=dish_a, quantity=50)

            guest_cart_1 = OldCart.objects.create(user=None, session_key='raw_guest_sess_abc')
            guest_cart_2 = OldCart.objects.create(user=None, session_key='raw_guest_sess_abc')
            OldCartItem.objects.create(cart=guest_cart_1, dish=dish_a, quantity=2)
            OldCartItem.objects.create(cart=guest_cart_2, dish=dish_a, quantity=3)

            legacy_order = OldOrder.objects.create(
                user=None,
                session_key='raw_order_sess_xyz',
                customer_name='Міграція',
                customer_phone='+380501112233',
                delivery_address='Київ',
                payment_method='STRIPE',
                payment_status='COMPLETED',
                total_amount=Decimal('500.00'),
            )

            executor_fwd = MigrationExecutor(connection)
            executor_fwd.migrate(target_0005)
            new_apps = executor_fwd.loader.project_state(target_0005).apps

            NewCart = new_apps.get_model('orders', 'Cart')
            NewOrder = new_apps.get_model('orders', 'Order')

            user_carts = list(NewCart.objects.filter(user_id=user.pk))
            self.assertEqual(len(user_carts), 1)
            self.assertEqual(user_carts[0].guest_token, '')
            items_by_dish = {ci.dish_id: ci.quantity for ci in user_carts[0].items.all()}
            self.assertEqual(items_by_dish[dish_a.pk], 99)
            self.assertEqual(items_by_dish[dish_b.pk], 2)

            guest_carts = list(NewCart.objects.filter(user_id__isnull=True))
            self.assertEqual(len(guest_carts), 1)
            self.assertTrue(bool(guest_carts[0].guest_token))
            self.assertNotEqual(guest_carts[0].guest_token, 'raw_guest_sess_abc')
            self.assertEqual(guest_carts[0].items.get(dish_id=dish_a.pk).quantity, 5)

            migrated_order = NewOrder.objects.get(pk=legacy_order.pk)
            self.assertEqual(migrated_order.payment_method, 'ONLINE')
            self.assertEqual(migrated_order.payment_status, 'PAID')
            self.assertTrue(bool(migrated_order.guest_token))
            self.assertNotEqual(migrated_order.guest_token, 'raw_order_sess_xyz')
        finally:
            MigrationExecutor(connection).migrate(target_0005)
