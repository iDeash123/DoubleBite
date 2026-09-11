from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from menu.models import Category, Dish, DishOption

from orders.exceptions import DishUnavailableError
from orders.models import Cart, CartItem
from orders.services import CartService

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

    def _get_request(self, user=None, session_key='test_session_123'):
        from django.contrib.auth.models import AnonymousUser

        req = self.factory.get('/')
        req.user = user if user else AnonymousUser()

        class FakeSession(dict):
            def __init__(self, key):
                super().__init__()
                self.session_key = key

            def create(self):
                pass

        req.session = FakeSession(session_key)
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
        req = self._get_request(session_key='guest_session_999')
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

        self.assertFalse(Cart.objects.filter(session_key='guest_session_999').exists())

    def test_merge_guest_cart_via_real_login_view(self):
        client = self.client_class()
        client.post(f'/cart/add/{self.dish1.id}/', {'quantity': '2'})

        guest_session_key = client.session.session_key
        self.assertTrue(bool(guest_session_key))
        guest_cart = Cart.objects.filter(session_key=guest_session_key).first()
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
