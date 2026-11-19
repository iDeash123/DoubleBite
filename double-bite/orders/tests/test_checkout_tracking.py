from decimal import Decimal
from unittest.mock import MagicMock, patch

from accounts.models import DeliveryAddress, Role
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from menu.models import Category, Dish, DishOption

from orders.exceptions import CartEmptyError, OrderMinimumAmountError
from orders.models import Cart, CartItem, Order, OrderStatus, PaymentMethod, PaymentStatus
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

    def _get_request(self, user=None, guest_token='checkout_guest_123'):
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

        req.session = FakeSession('checkout_session_123', guest_token)
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

    @override_settings(STRIPE_SECRET_KEY='sk_test_guest_tracking')
    @patch('orders.services.StripeService.create_checkout_session')
    def test_guest_tracking_token_flow_without_force_login(self, mock_create_session):
        mock_session = MagicMock()
        mock_session.url = 'https://checkout.stripe.com/pay/cs_guest_flow'
        mock_create_session.return_value = mock_session

        guest_order = Order.objects.create(
            user=None,
            guest_token='guest-flow-token-1',
            customer_name='Гість Без Логіну',
            customer_phone='+380501234567',
            delivery_address='Київ, вул. Хрещатик, 10',
            payment_method=PaymentMethod.ONLINE,
            status=OrderStatus.PENDING,
            total_amount=Decimal('250.00'),
        )

        guest_client = Client()
        tracking_url = reverse('orders:tracking', kwargs={'order_number': guest_order.order_number})
        status_url = reverse('orders:tracking_status', kwargs={'order_number': guest_order.order_number})
        checkout_url = reverse('orders:stripe_checkout', kwargs={'order_number': guest_order.order_number})
        cancel_url = reverse('orders:order_cancel', kwargs={'order_number': guest_order.order_number})

        unauth_response = guest_client.get(tracking_url)
        self.assertEqual(unauth_response.status_code, 404)

        token_response = guest_client.get(f'{tracking_url}?t={guest_order.access_token}')
        self.assertRedirects(token_response, tracking_url)

        session_page = guest_client.get(tracking_url)
        self.assertEqual(session_page.status_code, 200)
        self.assertTemplateUsed(session_page, 'orders/tracking.html')
        page_html = session_page.content.decode('utf-8')
        self.assertIn(guest_order.order_number, page_html)
        self.assertIn(guest_order.masked_customer_phone, page_html)
        self.assertIn('Оплатити онлайн', page_html)
        self.assertIn(f'action="{checkout_url}"', page_html)

        status_resp = guest_client.get(status_url, HTTP_HX_REQUEST='true')
        self.assertEqual(status_resp.status_code, 200)
        status_html = status_resp.content.decode('utf-8')
        self.assertIn('id="order-status"', status_html)
        self.assertIn(guest_order.get_status_display(), status_html)
        self.assertIn('Оплатити онлайн', status_html)
        self.assertNotIn('<!DOCTYPE html>', status_html)

        pay_resp = guest_client.post(checkout_url)
        self.assertEqual(pay_resp.status_code, 302)
        self.assertEqual(pay_resp.url, 'https://checkout.stripe.com/pay/cs_guest_flow')

        cancel_resp = guest_client.post(cancel_url)
        self.assertRedirects(cancel_resp, tracking_url)
        guest_order.refresh_from_db()
        self.assertEqual(guest_order.status, OrderStatus.CANCELLED)

    def test_tracking_view_and_htmx_status_for_authenticated_owner(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Іван Франко',
            customer_phone='+380501234567',
            delivery_address='Київ, вул. Хрещатик, 10',
            total_amount=Decimal('250.00'),
        )
        tracking_url = reverse('orders:tracking', kwargs={'order_number': order.order_number})
        status_url = reverse('orders:tracking_status', kwargs={'order_number': order.order_number})

        self.client.force_login(self.user)
        response = self.client.get(tracking_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'orders/tracking.html')
        self.assertIn(order.order_number, response.content.decode('utf-8'))

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
        self.assertEqual(attacker_resp.status_code, 404)
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

    def test_tracking_pay_online_button_renders_only_for_pending_online_orders(self):
        self.client.force_login(self.user)
        cases = [
            (PaymentMethod.ONLINE, OrderStatus.PENDING, PaymentStatus.PENDING, True),
            (PaymentMethod.ONLINE, OrderStatus.PAID, PaymentStatus.PAID, False),
            (PaymentMethod.ONLINE, OrderStatus.CANCELLED, PaymentStatus.PENDING, False),
            (PaymentMethod.CASH, OrderStatus.PENDING, PaymentStatus.PENDING, False),
            (PaymentMethod.CARD, OrderStatus.PENDING, PaymentStatus.PENDING, False),
        ]
        for method, status_val, pay_status, should_render in cases:
            order = Order.objects.create(
                user=self.user,
                customer_name='Іван',
                customer_phone='+380501234567',
                delivery_address='Київ',
                payment_method=method,
                status=status_val,
                payment_status=pay_status,
                total_amount=Decimal('250.00'),
            )
            resp = self.client.get(
                reverse('orders:tracking', kwargs={'order_number': order.order_number})
            )
            self.assertEqual(resp.status_code, 200)
            html = resp.content.decode('utf-8')
            if should_render:
                self.assertIn('Оплатити онлайн', html)
            else:
                self.assertNotIn('Оплатити онлайн', html)

    def test_checkout_view_prefills_format_full_address_without_trailing_kv_and_shows_saved(self):
        self.address.apartment = ''
        self.address.intercom = '25K'
        self.address.save()
        DeliveryAddress.objects.create(
            user=self.user,
            title='Офіс',
            city='Київ',
            street='вул. Саксаганського',
            building='44',
            is_default=False,
        )

        self.client.force_login(self.user)
        self.client.post(reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}))
        resp = self.client.get(reverse('orders:checkout'))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode('utf-8')

        self.assertIn('Київ, вул. Хрещатик 10, поверх 3, домофон 25K', html)
        initial_addr = resp.context['form'].initial['delivery_address']
        self.assertEqual(initial_addr, 'Київ, вул. Хрещатик 10, поверх 3, домофон 25K')
        self.assertNotIn(', кв. ', initial_addr)
        self.assertIn(self.user.email, html)
        self.assertIn('data-saved-address="Київ, вул. Хрещатик 10, поверх 3, домофон 25K"', html)
        self.assertIn('Офіс', html)
        self.assertIn('Київ, вул. Саксаганського 44', html)

    def test_checkout_view_saves_customer_email_and_renders_notes_errors(self):
        guest_client = Client()
        guest_client.post(reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}))

        invalid_resp = guest_client.post(
            reverse('orders:checkout'),
            {
                'customer_name': 'Гість Тест',
                'customer_phone': '+380501234567',
                'customer_email': 'guest.receipt@example.com',
                'delivery_address': 'Київ, вул. Франка, 5',
                'payment_method': PaymentMethod.CASH,
                'notes': 'А' * 501,
            },
        )
        self.assertEqual(invalid_resp.status_code, 200)
        self.assertIn('А' * 20, invalid_resp.content.decode('utf-8'))
        notes_errors = invalid_resp.context['form']['notes'].errors
        self.assertTrue(bool(notes_errors))
        self.assertIn(notes_errors[0], invalid_resp.content.decode('utf-8'))

        valid_resp = guest_client.post(
            reverse('orders:checkout'),
            {
                'customer_name': 'Гість Тест',
                'customer_phone': '+380501234567',
                'customer_email': 'Guest.Receipt@Example.com',
                'delivery_address': 'Київ, вул. Франка, 5',
                'payment_method': PaymentMethod.CASH,
                'notes': 'Біля дверей',
            },
        )
        order = Order.objects.get(customer_name='Гість Тест')
        self.assertEqual(order.customer_email, 'guest.receipt@example.com')
        expected_tracking = reverse('orders:tracking', kwargs={'order_number': order.order_number})
        self.assertEqual(valid_resp.status_code, 302)
        self.assertEqual(valid_resp.url, expected_tracking)
        self.assertNotIn('?t=', valid_resp.url)
        self.assertEqual(guest_client.get(valid_resp.url).status_code, 200)

    def test_checkout_view_double_submit_redirects_to_recent_order(self):
        guest_client = Client()
        guest_client.post(reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}))
        payload = {
            'customer_name': 'Подвійний Сабміт',
            'customer_phone': '+380501234567',
            'delivery_address': 'Київ, вул. Франка, 1',
            'payment_method': PaymentMethod.CASH,
        }
        first_resp = guest_client.post(reverse('orders:checkout'), payload)
        order = Order.objects.get(customer_name='Подвійний Сабміт')
        tracking_url = reverse('orders:tracking', kwargs={'order_number': order.order_number})
        self.assertRedirects(first_resp, tracking_url)

        second_resp = guest_client.post(reverse('orders:checkout'), payload)
        self.assertRedirects(second_resp, tracking_url)

        cart = CartService.get_or_create_cart(self._get_request(user=self.user))
        CartItem.objects.create(cart=cart, dish=self.dish1, quantity=1)
        self.client.force_login(self.user)
        user_order = Order.objects.create(
            user=self.user,
            customer_name='Іван Франко',
            customer_phone='+380501234567',
            delivery_address='Київ',
            status=OrderStatus.PENDING,
            total_amount=Decimal('250.00'),
        )
        with patch(
            'orders.services.OrderService.create_order_from_cart',
            side_effect=CartEmptyError('empty race'),
        ):
            race_resp = self.client.post(reverse('orders:checkout'), payload)
        self.assertRedirects(
            race_resp,
            reverse('orders:tracking', kwargs={'order_number': user_order.order_number}),
        )

    def test_guest_order_accessible_after_login_via_pre_login_guest_token(self):
        guest_client = Client()
        guest_client.post(reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}))
        guest_client.post(
            reverse('orders:checkout'),
            {
                'customer_name': 'Гість До Логіну',
                'customer_phone': '+380501234567',
                'delivery_address': 'Київ, вул. Франка, 12',
                'payment_method': PaymentMethod.CASH,
            },
        )
        order = Order.objects.get(customer_name='Гість До Логіну')
        self.assertIsNone(order.user)
        self.assertTrue(bool(order.guest_token))

        login_resp = guest_client.post(
            '/accounts/login/',
            {
                'email': self.user.email,
                'password': 'StrongPassword123!',
            },
        )
        self.assertEqual(login_resp.status_code, 302)

        track_resp = guest_client.get(
            reverse('orders:tracking', kwargs={'order_number': order.order_number})
        )
        self.assertEqual(track_resp.status_code, 200)
