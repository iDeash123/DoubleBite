from decimal import Decimal

from accounts.models import DeliveryAddress, Role
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from menu.models import Category, Dish, DishOption

from orders.exceptions import CartEmptyError, OrderMinimumAmountError
from orders.models import Cart, CartItem, Order, OrderStatus
from orders.services import CartService, OrderService

User = get_user_model()


class CheckoutAndTrackingTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.factory = RequestFactory()

        self.user = User.objects.create_user(
            email='checkout.user@doublebite.ua',
            password='StrongPassword123!',
            first_name='Іван',
            last_name='Франко',
            phone='+380501234567',
        )
        self.address = DeliveryAddress.objects.create(
            user=self.user,
            title='Дім',
            city='Київ',
            street='вул. Хрещатик',
            building='10',
            apartment='25',
            floor='3',
            is_default=True,
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
            title='Кватро Формаджі',
            slug='quattro-formaggi',
            price=Decimal('340.00'),
            weight_grams=480,
            is_available=True,
        )
        self.dish_unavailable = Dish.objects.create(
            category=self.category,
            title='Тимчасово відсутня',
            slug='unavailable',
            price=Decimal('210.00'),
            weight_grams=400,
            is_available=False,
        )

        self.opt_cheese = DishOption.objects.create(
            dish=self.dish1,
            name='Подвійний сир',
            price_delta=Decimal('40.00'),
        )

    def _get_request(self, user=None, session_key='checkout_session_123'):
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

    def test_create_order_from_cart_success(self):
        req = self._get_request(user=self.user)
        CartService.add_dish(req, dish_id=self.dish1.id, quantity=1, option_id=self.opt_cheese.id)

        order = OrderService.create_order_from_cart(
            request=req,
            customer_name='Іван Франко',
            customer_phone='+380501234567',
            delivery_address='Київ, вул. Хрещатик, 10, кв. 25',
        )

        self.assertIsNotNone(order)
        self.assertTrue(order.order_number.startswith('DB-'))
        self.assertEqual(order.user, self.user)
        self.assertEqual(order.status, OrderStatus.PENDING)
        self.assertEqual(order.total_amount, Decimal('290.00'))
        self.assertEqual(order.items.count(), 1)

        item = order.items.first()
        self.assertEqual(item.dish_title, 'Маргарита')
        self.assertEqual(item.price, Decimal('290.00'))
        self.assertEqual(item.quantity, 1)

        cart = CartService.get_cart(req)
        self.assertEqual(cart.items.count(), 0)

    def test_create_order_price_snapshot_preserved(self):
        req = self._get_request(user=self.user)
        CartService.add_dish(req, dish_id=self.dish1.id, quantity=1)

        order = OrderService.create_order_from_cart(
            request=req,
            customer_name='Іван',
            customer_phone='+380501234567',
            delivery_address='Київ',
        )

        self.dish1.price = Decimal('999.00')
        self.dish1.title = 'Нова ціна страви'
        self.dish1.save()

        order_item = order.items.first()
        order_item.refresh_from_db()
        self.assertEqual(order_item.dish_title, 'Маргарита')
        self.assertEqual(order_item.price, Decimal('250.00'))

    def test_create_order_empty_cart_raises_error(self):
        req = self._get_request(user=self.user)
        with self.assertRaises(CartEmptyError):
            OrderService.create_order_from_cart(
                request=req,
                customer_name='Іван',
                customer_phone='+380501234567',
                delivery_address='Київ',
            )

    def test_create_order_below_minimum_amount_raises_error(self):
        req = self._get_request(user=self.user)
        cheap = Dish.objects.create(
            category=self.category,
            title='Соус',
            slug='sauce',
            price=Decimal('50.00'),
            weight_grams=50,
            is_available=True,
        )
        CartService.add_dish(req, dish_id=cheap.id, quantity=1)

        with self.assertRaises(OrderMinimumAmountError):
            OrderService.create_order_from_cart(
                request=req,
                customer_name='Іван',
                customer_phone='+380501234567',
                delivery_address='Київ',
            )

    def test_checkout_view_get_with_items_renders_form(self):
        self.client.force_login(self.user)
        self.client.post(reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}))

        response = self.client.get(reverse('orders:checkout'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'orders/checkout.html')
        content = response.content.decode('utf-8')
        self.assertIn('Іван Франко', content)
        self.assertIn('+380501234567', content)
        self.assertIn('вул. Хрещатик', content)

    def test_checkout_view_post_creates_order_and_redirects(self):
        self.client.force_login(self.user)
        self.client.post(reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}))

        response = self.client.post(
            reverse('orders:checkout'),
            {
                'customer_name': 'Іван Франко',
                'customer_phone': '+380501234567',
                'delivery_address': 'Київ, вул. Хрещатик, 10, кв. 25',
                'payment_method': 'CARD',
                'notes': 'Код домофону 42K',
            },
        )

        order = Order.objects.filter(user=self.user).first()
        self.assertIsNotNone(order)
        self.assertRedirects(response, reverse('orders:tracking', kwargs={'order_number': order.order_number}))

    def test_tracking_view_and_htmx_status(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Іван Франко',
            customer_phone='+380501234567',
            delivery_address='Київ, вул. Хрещатик, 10',
            total_amount=Decimal('250.00'),
        )

        tracking_url = reverse('orders:tracking', kwargs={'order_number': order.order_number})
        response = self.client.get(tracking_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'orders/tracking.html')
        self.assertIn(order.order_number, response.content.decode('utf-8'))

        status_url = reverse('orders:tracking_status', kwargs={'order_number': order.order_number})
        status_resp = self.client.get(status_url, HTTP_HX_REQUEST='true')
        self.assertEqual(status_resp.status_code, 200)
        content = status_resp.content.decode('utf-8')
        self.assertIn('id="order-status"', content)
        self.assertIn(order.get_status_display(), content)
        self.assertNotIn('<!DOCTYPE html>', content)

    def test_order_cancel_flow(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Іван',
            customer_phone='+380501234567',
            delivery_address='Київ',
            status=OrderStatus.PENDING,
        )

        cancel_url = reverse('orders:order_cancel', kwargs={'order_number': order.order_number})

        attacker_resp = self.client.post(cancel_url)
        self.assertEqual(attacker_resp.status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING)

        self.client.force_login(self.user)
        response = self.client.post(cancel_url)
        self.assertRedirects(response, reverse('orders:tracking', kwargs={'order_number': order.order_number}))

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)

        order.status = OrderStatus.DELIVERED
        order.save()
        self.client.post(cancel_url)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DELIVERED)

    def test_staff_cannot_checkout(self):
        admin_user = User.objects.create_user(
            email='admin@doublebite.ua',
            password='AdminPassword123!',
            role=Role.RESTAURANT_ADMIN,
        )
        self.client.force_login(admin_user)
        response = self.client.get(reverse('orders:checkout'))
        self.assertEqual(response.status_code, 403)

    def test_superuser_can_checkout(self):
        superuser = User.objects.create_superuser(
            email='super_checkout@doublebite.ua',
            password='SuperPassword123!',
        )
        self.client.force_login(superuser)
        self.client.post(reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}))
        cart = Cart.objects.get(user=superuser)
        item = cart.items.first()
        item.quantity = 5
        item.save()
        response = self.client.get(reverse('orders:checkout'))
        self.assertEqual(response.status_code, 200)

    def test_checkout_view_displays_unavailable_item_warning(self):
        self.client.force_login(self.user)
        self.client.post(reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}))
        cart = Cart.objects.get(user=self.user)
        CartItem.objects.create(cart=cart, dish=self.dish_unavailable, quantity=1)

        response = self.client.get(reverse('orders:checkout'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Ця страва тимчасово недоступна', content)
        self.assertIn('не враховано', content)
