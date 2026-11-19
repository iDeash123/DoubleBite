from decimal import Decimal

from accounts.models import Role
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from menu.models import Category, Dish, DishOption

from orders.models import Cart, Order

User = get_user_model()


class CartViewsTest(TestCase):
    def setUp(self):
        self.client = Client()

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
            title='Діавола',
            slug='diavola',
            price=Decimal('310.00'),
            weight_grams=480,
            is_available=True,
        )

    def test_cart_view_empty_cart(self):
        response = self.client.get(reverse('orders:cart'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'orders/cart.html')
        self.assertIn('Ваш кошик порожній', response.content.decode('utf-8'))

    def test_cart_add_htmx_returns_badge_partial(self):
        url = reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id})
        response = self.client.post(
            url,
            {'quantity': '2'},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="cart-badge"', content)
        self.assertIn('2', content)
        self.assertNotIn('<!DOCTYPE html>', content)

    def test_legacy_direct_cart_endpoints_removed(self):
        self.assertEqual(self.client.get('/cart/').status_code, 404)
        self.assertEqual(self.client.get('/cart/drawer/').status_code, 404)
        self.assertEqual(
            self.client.post(f'/cart/add/{self.dish2.id}/', {'quantity': '1'}).status_code,
            404,
        )
        self.assertEqual(self.client.post('/cart/clear/').status_code, 404)

    def test_cart_update_and_remove_htmx_returns_cart_partial(self):
        self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}),
            {'quantity': '2'},
            HTTP_HX_REQUEST='true',
        )

        guest_token = self.client.session.get('guest_token')
        self.assertTrue(bool(guest_token))
        cart = Cart.objects.get(guest_token=guest_token, user__isnull=True)
        item = cart.items.first()

        update_url = reverse('orders:cart_update', kwargs={'item_id': item.id})
        response = self.client.post(
            update_url,
            {'quantity': '3'},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="cart-content"', content)
        self.assertIn('Маргарита', content)
        self.assertIn('3', content)

        remove_url = reverse('orders:cart_remove', kwargs={'item_id': item.id})
        response = self.client.post(
            remove_url,
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="cart-content"', content)
        self.assertIn('Ваш кошик порожній', content)

    def test_cart_clear(self):
        self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}),
            {'quantity': '2'},
        )
        self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish2.id}),
            {'quantity': '1'},
        )

        response = self.client.post(reverse('orders:cart_clear'), HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Ваш кошик порожній', content)

    def test_cart_drawer_view(self):
        response = self.client.get(reverse('orders:cart_drawer'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'partials/cart_drawer.html')

        htmx_resp = self.client.get(reverse('orders:cart_drawer'), HTTP_HX_REQUEST='true')
        self.assertEqual(htmx_resp.status_code, 200)
        self.assertIn('id="cart-drawer-container"', htmx_resp.content.decode('utf-8'))

    def test_cart_update_and_remove_from_drawer(self):
        self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}),
            {'quantity': '1'},
            HTTP_HX_REQUEST='true',
        )
        guest_token = self.client.session.get('guest_token')
        self.assertTrue(bool(guest_token))
        cart = Cart.objects.get(guest_token=guest_token, user__isnull=True)
        item = cart.items.first()

        update_url = reverse('orders:cart_update', kwargs={'item_id': item.id})
        response = self.client.post(
            update_url,
            {'quantity': '4'},
            HTTP_HX_REQUEST='true',
            HTTP_HX_TARGET='cart-drawer-container',
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="cart-drawer-container"', content)
        self.assertIn('4', content)
        self.assertIn('id="cart-badge"', content)

        remove_url = reverse('orders:cart_remove', kwargs={'item_id': item.id})
        response = self.client.post(
            remove_url,
            HTTP_HX_REQUEST='true',
            HTTP_HX_TARGET='cart-drawer-container',
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="cart-drawer-container"', content)
        self.assertIn('Кошик порожній', content)


    def test_cart_add_unavailable_dish_returns_400(self):
        unavailable_dish = Dish.objects.create(
            category=self.category,
            title='Недоступна піца',
            slug='unavailable-pizza',
            price=Decimal('280.00'),
            weight_grams=400,
            is_available=False,
        )
        url = reverse('orders:cart_add', kwargs={'dish_id': unavailable_dish.id})
        response = self.client.post(url, {'quantity': '1'}, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 400)
        self.assertIn('недоступна', response.content.decode('utf-8'))

    def test_cart_add_staff_forbidden(self):
        admin_user = User.objects.create_user(
            email='admin@test.com',
            password='adminpassword123',
            role=Role.RESTAURANT_ADMIN,
        )
        self.client.force_login(admin_user)
        url = reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id})
        response = self.client.post(url, {'quantity': '1'}, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 403)

    def test_order_list_view_permissions(self):
        url = reverse('orders:order_list')

        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

        customer = User.objects.create_user(
            email='customer@test.com',
            password='customerpassword123',
            role=Role.CUSTOMER,
        )
        self.client.force_login(customer)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'orders/order_list.html')

        admin_user = User.objects.create_user(
            email='admin_staff@test.com',
            password='adminpassword123',
            role=Role.RESTAURANT_ADMIN,
        )
        self.client.force_login(admin_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

        superuser = User.objects.create_superuser(
            email='superuser@test.com',
            password='superpassword123',
        )
        self.client.force_login(superuser)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_cart_add_triggers_open_cart_drawer_header(self):
        url = reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id})
        response = self.client.post(url, {'quantity': '1'}, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('HX-Trigger'), 'open-cart-drawer')

    def test_cart_add_superuser_allowed(self):
        superuser = User.objects.create_superuser(
            email='superuser_cart@test.com',
            password='superpassword123',
        )
        self.client.force_login(superuser)
        url = reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id})
        response = self.client.post(url, {'quantity': '1'}, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('HX-Trigger'), 'open-cart-drawer')

    def test_cart_add_safe_referer_redirect(self):
        url = reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id})
        response = self.client.post(
            url,
            {'quantity': '1'},
            HTTP_REFERER='https://evil-phishing.com/steal',
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('menu:catalog'))

        valid_referer = 'http://testserver/menu/'
        response_valid = self.client.post(
            url,
            {'quantity': '1'},
            HTTP_REFERER=valid_referer,
        )
        self.assertEqual(response_valid.status_code, 302)
        self.assertEqual(response_valid.url, valid_referer)

    def test_checkout_form_phone_validation(self):
        from orders.forms import OrderCheckoutForm

        form_invalid = OrderCheckoutForm(data={
            'customer_name': 'Test User',
            'customer_phone': '12345',
            'delivery_address': 'Kyiv, Main St, 1',
            'payment_method': 'CARD',
        })
        self.assertFalse(form_invalid.is_valid())
        self.assertIn('customer_phone', form_invalid.errors)

        form_valid = OrderCheckoutForm(data={
            'customer_name': 'Test User',
            'customer_phone': '+380 50 123 45 67',
            'delivery_address': 'Kyiv, Main St, 1',
            'payment_method': 'CARD',
        })
        self.assertTrue(form_valid.is_valid())
        self.assertEqual(form_valid.cleaned_data['customer_phone'], '+380501234567')

        form_parentheses = OrderCheckoutForm(data={
            'customer_name': 'Test User',
            'customer_phone': '+380 (50) 123-45-67',
            'delivery_address': 'Kyiv, Main St, 1',
            'payment_method': 'CARD',
        })
        self.assertTrue(form_parentheses.is_valid())
        self.assertEqual(form_parentheses.cleaned_data['customer_phone'], '+380501234567')

    def test_cart_add_view_rejects_invalid_quantities_and_options_with_400(self):
        url = reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id})
        for bad_qty in ('²', 'abc', str(2**31), '0', '-5', '100'):
            resp = self.client.post(url, {'quantity': bad_qty}, HTTP_HX_REQUEST='true')
            self.assertEqual(resp.status_code, 400, f'Expected 400 for qty={bad_qty!r}')

        for bad_opt in ('²', 'abc', '0', '-1', str(2**31)):
            resp = self.client.post(
                url,
                {'quantity': '1', 'option_id': bad_opt},
                HTTP_HX_REQUEST='true',
            )
            self.assertEqual(resp.status_code, 400, f'Expected 400 for option_id={bad_opt!r}')

        foreign_opt = DishOption.objects.create(
            dish=self.dish2,
            name='Чужий соус',
            price_delta=Decimal('20.00'),
        )
        foreign_resp = self.client.post(
            url,
            {'quantity': '1', 'option_id': str(foreign_opt.id)},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(foreign_resp.status_code, 400)

        missing_dish_resp = self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': 999999}),
            {'quantity': '1'},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(missing_dish_resp.status_code, 404)

    def test_cart_update_view_rejects_invalid_quantities_with_400_without_resetting(self):
        self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}),
            {'quantity': '3'},
            HTTP_HX_REQUEST='true',
        )
        guest_token = self.client.session.get('guest_token')
        cart = Cart.objects.get(guest_token=guest_token, user__isnull=True)
        item = cart.items.first()

        update_url = reverse('orders:cart_update', kwargs={'item_id': item.id})
        for bad_qty in ('²', 'abc', str(2**31), '-1', '100'):
            resp = self.client.post(
                update_url,
                {'quantity': bad_qty},
                HTTP_HX_REQUEST='true',
            )
            self.assertEqual(resp.status_code, 400, f'Expected 400 for update qty={bad_qty!r}')
            item.refresh_from_db()
            self.assertEqual(item.quantity, 3)

    def test_cart_badge_view_returns_current_count(self):
        badge_url = reverse('orders:cart_badge')
        empty_resp = self.client.get(badge_url)
        self.assertEqual(empty_resp.status_code, 200)
        self.assertIn('id="cart-badge"', empty_resp.content.decode('utf-8'))

        self.client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish1.id}),
            {'quantity': '2'},
        )
        filled_resp = self.client.get(badge_url)
        self.assertEqual(filled_resp.status_code, 200)
        content = filled_resp.content.decode('utf-8')
        self.assertIn('id="cart-badge"', content)
        self.assertIn('2', content)

    def test_order_list_view_paginates_15_per_page(self):
        customer = User.objects.create_user(
            email='paginated@test.com',
            password='customerpassword123',
            role=Role.CUSTOMER,
        )
        for idx in range(20):
            Order.objects.create(
                user=customer,
                customer_name='Клієнт',
                customer_phone='+380501112233',
                delivery_address=f'Київ, вул. Тестова, {idx + 1}',
                total_amount=Decimal('250.00'),
            )

        self.client.force_login(customer)
        url = reverse('orders:order_list')

        page1_resp = self.client.get(url)
        self.assertEqual(page1_resp.status_code, 200)
        self.assertEqual(len(page1_resp.context['page_obj']), 15)
        self.assertEqual(page1_resp.context['page_obj'].paginator.num_pages, 2)
        self.assertIn('?page=2', page1_resp.content.decode('utf-8'))

        page2_resp = self.client.get(f'{url}?page=2')
        self.assertEqual(page2_resp.status_code, 200)
        self.assertEqual(len(page2_resp.context['page_obj']), 5)
        self.assertIn('?page=1', page2_resp.content.decode('utf-8'))


