import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from accounts.models import Role
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connections
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from menu.models import Category, Dish, DishOption

from orders.exceptions import (
    CartForbiddenError,
    DishNotFoundError,
    DishUnavailableError,
    InvalidOptionError,
)
from orders.middleware import PRE_LOGIN_GUEST_TOKEN_SESSION_KEY
from orders.models import Cart, CartItem, Order, PaymentMethod
from orders.services import CartService, OrderService

User = get_user_model()


class CartServiceTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            email='shopper@doublebite.ua',
            password='StrongPassword123!',
        )

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
            title='Пепероні',
            slug='pepperoni',
            price=Decimal('290.00'),
            weight_grams=460,
            is_available=True,
        )
        self.dish_unavailable = Dish.objects.create(
            category=self.category,
            title='Недоступна піца',
            slug='unavailable-pizza',
            price=Decimal('200.00'),
            weight_grams=400,
            is_available=False,
        )

        self.opt_cheese = DishOption.objects.create(
            dish=self.dish1,
            name='Подвійний сир',
            price_delta=Decimal('40.00'),
        )

    def _get_request(self, user=None, guest_token='test_guest_token_123'):
        from django.contrib.auth.models import AnonymousUser

        req = self.factory.get('/')
        req.user = user if user else AnonymousUser()

        class FakeSession(dict):
            def __init__(self, key, token):
                super().__init__()
                self.session_key = key
                if token:
                    self['guest_token'] = token

            def create(self):
                pass

        req.session = FakeSession('test_session_123', guest_token)
        return req

    def test_add_dish_creates_cart_and_item(self):
        req = self._get_request(user=self.user)
        item = CartService.add_dish(req, dish_id=self.dish1.id, quantity=2)

        self.assertEqual(item.dish, self.dish1)
        self.assertEqual(item.quantity, 2)
        cart = CartService.get_cart(req)
        self.assertIsNotNone(cart)
        self.assertEqual(cart.user, self.user)
        self.assertEqual(CartService.get_items_count(req), 2)

    def test_add_same_dish_same_options_increments_quantity(self):
        req = self._get_request(user=self.user)
        CartService.add_dish(req, dish_id=self.dish1.id, quantity=2)
        CartService.add_dish(req, dish_id=self.dish1.id, quantity=3)

        cart = CartService.get_cart(req)
        self.assertEqual(cart.items.count(), 1)
        self.assertEqual(cart.items.first().quantity, 5)

    def test_add_same_dish_different_options_creates_distinct_items(self):
        req = self._get_request(user=self.user)
        CartService.add_dish(req, dish_id=self.dish1.id, quantity=1)
        CartService.add_dish(req, dish_id=self.dish1.id, quantity=1, option_id=self.opt_cheese.id)

        cart = CartService.get_cart(req)
        self.assertEqual(cart.items.count(), 2)
        self.assertEqual(cart.total_quantity, 2)

    def test_quantity_cap_at_99(self):
        req = self._get_request(user=self.user)
        CartService.add_dish(req, dish_id=self.dish1.id, quantity=150)
        cart = CartService.get_cart(req)
        self.assertEqual(cart.items.first().quantity, 99)

        CartService.add_dish(req, dish_id=self.dish1.id, quantity=5)
        cart.items.first().refresh_from_db()
        self.assertEqual(cart.items.first().quantity, 99)

    def test_add_unavailable_dish_raises_error(self):
        req = self._get_request(user=self.user)
        with self.assertRaises(DishUnavailableError):
            CartService.add_dish(req, dish_id=self.dish_unavailable.id, quantity=1)

    def test_update_item_quantity(self):
        req = self._get_request(user=self.user)
        item = CartService.add_dish(req, dish_id=self.dish1.id, quantity=2)

        updated = CartService.update_item_quantity(req, item_id=item.id, quantity=5)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.quantity, 5)

        deleted = CartService.update_item_quantity(req, item_id=item.id, quantity=0)
        self.assertIsNone(deleted)
        self.assertEqual(Cart.objects.get(user=self.user).items.count(), 0)

    def test_remove_and_clear_cart(self):
        req = self._get_request(user=self.user)
        item1 = CartService.add_dish(req, dish_id=self.dish1.id, quantity=1)
        CartService.add_dish(req, dish_id=self.dish2.id, quantity=2)

        CartService.remove_item(req, item_id=item1.id)
        cart = CartService.get_cart(req)
        self.assertEqual(cart.items.count(), 1)

        CartService.clear_cart(req)
        self.assertEqual(cart.items.count(), 0)

    def test_merge_guest_cart_on_login(self):
        req = self._get_request(guest_token='guest_token_999')
        CartService.add_dish(req, dish_id=self.dish1.id, quantity=2)
        CartService.add_dish(req, dish_id=self.dish2.id, quantity=1)

        user_cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=user_cart, dish=self.dish1, quantity=1)

        CartService.merge_guest_cart(req, user=self.user)

        user_cart.refresh_from_db()
        self.assertEqual(user_cart.items.count(), 2)
        item_d1 = user_cart.items.get(dish=self.dish1)
        self.assertEqual(item_d1.quantity, 3)
        item_d2 = user_cart.items.get(dish=self.dish2)
        self.assertEqual(item_d2.quantity, 1)

        self.assertFalse(Cart.objects.filter(guest_token='guest_token_999').exists())
        self.assertEqual(
            req.session.get(PRE_LOGIN_GUEST_TOKEN_SESSION_KEY),
            'guest_token_999',
        )

    def test_merge_guest_cart_via_real_login_view(self):
        client = self.client_class()
        client.post(f'/orders/cart/add/{self.dish1.id}/', {'quantity': '2'})

        guest_token = client.session.get('guest_token')
        self.assertTrue(bool(guest_token))
        self.assertNotEqual(guest_token, client.session.session_key)
        guest_cart = Cart.objects.filter(guest_token=guest_token, user__isnull=True).first()
        self.assertIsNotNone(guest_cart)
        self.assertEqual(guest_cart.items.count(), 1)

        resp = client.post('/accounts/login/', {
            'email': 'shopper@doublebite.ua',
            'password': 'StrongPassword123!',
        })
        self.assertEqual(resp.status_code, 302)

        user_cart = Cart.objects.filter(user=self.user).first()
        self.assertIsNotNone(user_cart)
        self.assertEqual(user_cart.items.count(), 1)
        self.assertEqual(user_cart.items.first().quantity, 2)
        self.assertFalse(Cart.objects.filter(id=guest_cart.id).exists())
        self.assertEqual(
            client.session.get(PRE_LOGIN_GUEST_TOKEN_SESSION_KEY),
            guest_token,
        )

    def test_unit_price_handles_malformed_delta(self):
        cart = Cart.objects.create(user=self.user)
        item = CartItem.objects.create(
            cart=cart,
            dish=self.dish1,
            quantity=1,
            selected_options=[
                {'name': 'Тест', 'price_delta': None},
                {'name': 'Невірне', 'price_delta': 'invalid'},
                {'name': 'Валідне', 'price_delta': '25.50'},
            ],
        )
        self.assertEqual(item.unit_price, Decimal('275.50'))

    def test_add_dish_same_dish_multiple_calls(self):
        req = self._get_request(user=self.user)
        CartService.add_dish(req, dish_id=self.dish1.id, quantity=2)
        CartService.add_dish(req, dish_id=self.dish1.id, quantity=3)

        cart = CartService.get_cart(req)
        self.assertEqual(cart.items.count(), 1)
        self.assertEqual(cart.items.first().quantity, 5)

    def test_cart_service_add_dish_forbidden_for_staff(self):
        courier_user = User.objects.create_user(
            email='courier@doublebite.ua',
            password='StrongPassword123!',
            role=Role.COURIER,
        )
        req = self._get_request(user=courier_user)
        with self.assertRaises(CartForbiddenError):
            CartService.add_dish(req, dish_id=self.dish1.id, quantity=1)

    def test_add_dish_rejects_missing_dish_and_invalid_or_foreign_options(self):
        req = self._get_request(user=self.user)
        foreign_opt = DishOption.objects.create(
            dish=self.dish2,
            name='Соус гострий',
            price_delta=Decimal('20.00'),
        )

        with self.assertRaises(DishNotFoundError):
            CartService.add_dish(req, dish_id=999999, quantity=1)

        with self.assertRaises(InvalidOptionError):
            CartService.add_dish(req, dish_id=self.dish1.id, quantity=1, option_id=999999)

        with self.assertRaises(InvalidOptionError):
            CartService.add_dish(
                req,
                dish_id=self.dish1.id,
                quantity=1,
                option_id=foreign_opt.id,
            )

        with self.assertRaises(InvalidOptionError):
            CartService.add_dish(
                req,
                dish_id=self.dish1.id,
                quantity=1,
                selected_options=[{'option_id': self.opt_cheese.id, 'price_delta': 'NaN'}],
            )

    def test_create_order_from_cart_uses_db_dish_option_price_delta(self):
        req = self._get_request(user=self.user)
        item = CartService.add_dish(
            req,
            dish_id=self.dish1.id,
            quantity=1,
            option_id=self.opt_cheese.id,
        )

        CartItem.objects.filter(pk=item.pk).update(
            selected_options=[
                {
                    'option_id': self.opt_cheese.id,
                    'name': 'Зламана назва',
                    'price_delta': '-9999.00',
                }
            ]
        )
        DishOption.objects.filter(pk=self.opt_cheese.pk).update(price_delta=Decimal('75.00'))

        order = OrderService.create_order_from_cart(
            request=req,
            customer_name='Покупець',
            customer_phone='+380501234567',
            delivery_address='Київ, вул. Хрещатик, 1',
            payment_method=PaymentMethod.CASH,
        )

        self.assertEqual(order.total_amount, Decimal('325.00'))
        order_item = order.items.first()
        self.assertEqual(order_item.price, Decimal('325.00'))
        self.assertEqual(
            order_item.selected_options,
            [
                {
                    'option_id': self.opt_cheese.id,
                    'name': 'Подвійний сир',
                    'price_delta': '75.00',
                }
            ],
        )

    def test_create_order_from_cart_rejects_foreign_deleted_or_invalid_options(self):
        req = self._get_request(user=self.user)
        cart = CartService.get_or_create_cart(req)
        foreign_opt = DishOption.objects.create(
            dish=self.dish2,
            name='Чужа опція',
            price_delta=Decimal('15.00'),
        )

        bad_item = CartItem.objects.create(
            cart=cart,
            dish=self.dish1,
            quantity=1,
            selected_options=[{'option_id': foreign_opt.id, 'name': 'Чужа', 'price_delta': '15.00'}],
        )
        with self.assertRaises(InvalidOptionError):
            OrderService.create_order_from_cart(
                request=req,
                customer_name='Покупець',
                customer_phone='+380501234567',
                delivery_address='Київ',
            )

        deleted_opt = DishOption.objects.create(
            dish=self.dish1,
            name='Видалена опція',
            price_delta=Decimal('25.00'),
        )
        deleted_opt_id = deleted_opt.id
        deleted_opt.delete()
        bad_item.selected_options = [
            {'option_id': deleted_opt_id, 'name': 'Видалена опція', 'price_delta': '25.00'}
        ]
        bad_item.save()

        with self.assertRaises(InvalidOptionError):
            OrderService.create_order_from_cart(
                request=req,
                customer_name='Покупець',
                customer_phone='+380501234567',
                delivery_address='Київ',
            )

        bad_item.selected_options = [{'name': 'Без ID', 'price_delta': '10.00'}]
        bad_item.save()
        with self.assertRaises(InvalidOptionError):
            OrderService.create_order_from_cart(
                request=req,
                customer_name='Покупець',
                customer_phone='+380501234567',
                delivery_address='Київ',
            )

    def test_create_order_from_cart_clears_unavailable_only_cart(self):
        req = self._get_request(user=self.user)
        cart = CartService.get_or_create_cart(req)
        CartItem.objects.create(cart=cart, dish=self.dish_unavailable, quantity=2)

        with self.assertRaises(DishUnavailableError):
            OrderService.create_order_from_cart(
                request=req,
                customer_name='Покупець',
                customer_phone='+380501234567',
                delivery_address='Київ',
            )

        self.assertEqual(cart.items.count(), 0)

    def test_single_login_signal_triggers_merge_guest_cart_once(self):
        client = self.client_class()
        client.post(f'/orders/cart/add/{self.dish1.id}/', {'quantity': '1'})

        with patch(
            'orders.services.CartService.merge_guest_cart_to_user',
            wraps=CartService.merge_guest_cart_to_user,
        ) as mock_merge:
            resp = client.post(
                '/accounts/login/',
                {
                    'email': 'shopper@doublebite.ua',
                    'password': 'StrongPassword123!',
                },
            )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(mock_merge.call_count, 1)

    def test_pre_login_guest_token_survives_login_without_cart_and_grants_order_access(self):
        client = self.client_class()
        client.post(f'/orders/cart/add/{self.dish1.id}/', {'quantity': '1'})
        guest_token = client.session.get('guest_token')
        self.assertTrue(bool(guest_token))

        guest_order = Order.objects.create(
            user=None,
            guest_token=guest_token,
            customer_name='Гість',
            customer_phone='+380501234567',
            delivery_address='Київ',
            payment_method=PaymentMethod.CASH,
            total_amount=Decimal('250.00'),
        )
        Cart.objects.filter(guest_token=guest_token).delete()

        login_resp = client.post(
            '/accounts/login/',
            {
                'email': 'shopper@doublebite.ua',
                'password': 'StrongPassword123!',
            },
        )
        self.assertEqual(login_resp.status_code, 302)
        self.assertEqual(
            client.session.get(PRE_LOGIN_GUEST_TOKEN_SESSION_KEY),
            guest_token,
        )

        tracking_url = reverse('orders:tracking', kwargs={'order_number': guest_order.order_number})
        track_resp = client.get(tracking_url)
        self.assertEqual(track_resp.status_code, 200)

    def test_get_or_create_user_cart_recovers_from_integrity_error(self):
        existing = Cart.objects.create(user=self.user)
        with patch('orders.services.Cart.objects.filter') as mock_filter:
            mock_filter.return_value.first.return_value = None
            recovered = CartService._get_or_create_user_cart(self.user)
        self.assertEqual(recovered.pk, existing.pk)


class CartConcurrencyTest(TransactionTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            email='concurrent.cart@doublebite.ua',
            password='StrongPassword123!',
        )
        self.category = Category.objects.create(
            name='Піца',
            slug='pizza-conc',
            is_active=True,
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Маргарита',
            slug='margarita-conc',
            price=Decimal('250.00'),
            weight_grams=450,
            is_available=True,
        )

    def test_concurrent_get_or_create_user_cart_creates_single_cart(self):
        workers = 6
        barrier = threading.Barrier(workers)

        def worker(_idx: int) -> int:
            try:
                req = self.factory.get('/')
                req.user = self.user
                req.session = {}
                barrier.wait(timeout=10)
                cart = CartService.get_or_create_cart(req)
                return cart.pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            cart_ids = list(pool.map(worker, range(workers)))

        self.assertEqual(len(set(cart_ids)), 1)
        self.assertEqual(Cart.objects.filter(user=self.user).count(), 1)

    def test_concurrent_merge_guest_cart_handles_race_cleanly(self):
        guest_token = 'concurrent-guest-token-xyz'
        guest_cart = Cart.objects.create(user=None, guest_token=guest_token)
        CartItem.objects.create(cart=guest_cart, dish=self.dish, quantity=3)

        workers = 4
        barrier = threading.Barrier(workers)

        def worker(_idx: int) -> bool:
            try:
                req = self.factory.post('/accounts/login/')
                req.user = self.user
                req.session = {'guest_token': guest_token, 'guest_cart_id': guest_cart.id}
                barrier.wait(timeout=10)
                CartService.merge_guest_cart(req, self.user)
                return True
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(worker, range(workers)))

        self.assertTrue(all(results))
        self.assertFalse(Cart.objects.filter(guest_token=guest_token).exists())
        user_cart = Cart.objects.get(user=self.user)
        self.assertEqual(user_cart.items.get(dish=self.dish).quantity, 3)


class CleanupAbandonedCartsCommandTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='cleanup@doublebite.ua',
            password='StrongPassword123!',
        )
        self.user_empty = User.objects.create_user(
            email='cleanup.empty@doublebite.ua',
            password='StrongPassword123!',
        )
        self.category = Category.objects.create(
            name='Піца',
            slug='pizza-cleanup',
            is_active=True,
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Маргарита',
            slug='margarita-cleanup',
            price=Decimal('250.00'),
            weight_grams=450,
            is_available=True,
        )

        old_dt = timezone.now() - timedelta(days=10)
        fresh_dt = timezone.now() - timedelta(days=2)

        self.old_anon = Cart.objects.create(user=None, guest_token='old-anon-tok')
        CartItem.objects.create(cart=self.old_anon, dish=self.dish, quantity=2)

        self.old_empty_auth = Cart.objects.create(user=self.user_empty)

        self.old_non_empty_auth = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=self.old_non_empty_auth, dish=self.dish, quantity=1)

        self.fresh_anon = Cart.objects.create(user=None, guest_token='fresh-anon-tok')
        CartItem.objects.create(cart=self.fresh_anon, dish=self.dish, quantity=1)

        Cart.objects.filter(
            pk__in=[self.old_anon.pk, self.old_empty_auth.pk, self.old_non_empty_auth.pk]
        ).update(updated_at=old_dt)
        Cart.objects.filter(pk=self.fresh_anon.pk).update(updated_at=fresh_dt)

    def test_cleanup_abandoned_carts_dry_run_and_delete(self):
        dry_out = StringIO()
        call_command('cleanup_abandoned_carts', days=7, dry_run=True, stdout=dry_out)
        self.assertIn('DRY-RUN', dry_out.getvalue())
        self.assertIn('2', dry_out.getvalue())
        self.assertEqual(Cart.objects.count(), 4)

        real_out = StringIO()
        call_command('cleanup_abandoned_carts', days=7, batch_size=1, stdout=real_out)
        self.assertIn('2', real_out.getvalue())

        self.assertFalse(Cart.objects.filter(pk=self.old_anon.pk).exists())
        self.assertFalse(Cart.objects.filter(pk=self.old_empty_auth.pk).exists())
        self.assertTrue(Cart.objects.filter(pk=self.old_non_empty_auth.pk).exists())
        self.assertTrue(Cart.objects.filter(pk=self.fresh_anon.pk).exists())

    def test_cleanup_abandoned_carts_validates_arguments(self):
        with self.assertRaises(CommandError):
            call_command('cleanup_abandoned_carts', days=0)
        with self.assertRaises(CommandError):
            call_command('cleanup_abandoned_carts', batch_size=0)
