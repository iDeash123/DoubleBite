import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import stripe
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from menu.models import Category, Dish

from orders.models import (
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from orders.services import StripeService

User = get_user_model()


class StripeServiceTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='stripe.client@doublebite.ua',
            password='StrongPassword123!',
            first_name='Оксана',
            last_name='Петренко',
        )
        self.category = Category.objects.create(
            name='Піца',
            slug='pizza-stripe',
            is_active=True,
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Карбонара',
            slug='carbonara',
            price=Decimal('280.00'),
            weight_grams=450,
            is_available=True,
        )
        self.order = Order.objects.create(
            user=self.user,
            customer_name='Оксана Петренко',
            customer_phone='+380509998877',
            delivery_address='Київ, вул. Володимирська, 10',
            payment_method=PaymentMethod.ONLINE,
            total_amount=Decimal('560.00'),
        )
        self.order_item = OrderItem.objects.create(
            order=self.order,
            dish=self.dish,
            dish_title='Карбонара',
            price=Decimal('280.00'),
            quantity=2,
        )

    @override_settings(STRIPE_SECRET_KEY='sk_test_fake_secret_key')
    @patch('stripe.checkout.Session.create')
    def test_create_checkout_session_success(self, mock_session_create):
        mock_session = MagicMock()
        mock_session.id = 'cs_test_session_123456'
        mock_session.url = 'https://checkout.stripe.com/c/pay/cs_test_session_123456'
        mock_session_create.return_value = mock_session

        factory = RequestFactory()
        req = factory.get('/')

        session = StripeService.create_checkout_session(self.order, request=req)

        self.assertEqual(session.id, 'cs_test_session_123456')
        self.order.refresh_from_db()
        self.assertEqual(self.order.stripe_session_id, 'cs_test_session_123456')

        mock_session_create.assert_called_once()
        _, kwargs = mock_session_create.call_args

        self.assertEqual(kwargs['mode'], 'payment')
        self.assertEqual(kwargs['client_reference_id'], self.order.order_number)
        self.assertEqual(kwargs['customer_email'], 'stripe.client@doublebite.ua')
        self.assertEqual(kwargs['metadata']['order_number'], self.order.order_number)
        self.assertEqual(kwargs['metadata']['order_id'], str(self.order.id))
        self.assertEqual(kwargs['payment_intent_data']['metadata']['order_number'], self.order.order_number)
        self.assertEqual(kwargs['payment_intent_data']['metadata']['order_id'], str(self.order.id))

        self.assertEqual(len(kwargs['line_items']), 1)
        item_data = kwargs['line_items'][0]
        self.assertEqual(item_data['quantity'], 2)
        self.assertEqual(item_data['price_data']['currency'], 'uah')
        self.assertEqual(item_data['price_data']['unit_amount'], 28000)
        self.assertEqual(item_data['price_data']['product_data']['name'], 'Карбонара')

        self.assertIn(self.order.order_number, kwargs['success_url'])
        self.assertIn('{CHECKOUT_SESSION_ID}', kwargs['success_url'])
        self.assertIn(self.order.order_number, kwargs['cancel_url'])

    @override_settings(STRIPE_SECRET_KEY='')
    @patch.dict('os.environ', {'STRIPE_SECRET_KEY': ''})
    def test_create_checkout_session_missing_secret_key_raises_error(self):
        with self.assertRaises(ValueError):
            StripeService.create_checkout_session(self.order)

    @override_settings(STRIPE_SECRET_KEY='sk_test_fake_secret_key')
    @patch('stripe.checkout.Session.create')
    def test_create_checkout_session_fallback_line_items(self, mock_session_create):
        empty_order = Order.objects.create(
            user=self.user,
            customer_name='Оксана',
            customer_phone='+380509998877',
            delivery_address='Київ',
            total_amount=Decimal('350.00'),
        )
        mock_session = MagicMock()
        mock_session.id = 'cs_test_empty_order'
        mock_session.url = 'https://checkout.stripe.com/pay'
        mock_session_create.return_value = mock_session

        StripeService.create_checkout_session(empty_order)

        mock_session_create.assert_called_once()
        _, kwargs = mock_session_create.call_args
        self.assertEqual(len(kwargs['line_items']), 1)
        self.assertEqual(kwargs['line_items'][0]['price_data']['unit_amount'], 35000)

    @override_settings(STRIPE_SECRET_KEY='sk_test_fake_secret_key')
    @patch('stripe.checkout.Session.create')
    def test_create_checkout_session_with_item_none_price(self, mock_session_create):
        order = Order.objects.create(
            user=self.user,
            customer_name='Оксана',
            customer_phone='+380509998877',
            delivery_address='Київ',
            total_amount=Decimal('200.00'),
        )
        item = OrderItem(
            order=order,
            dish_title='Страва без ціни',
            price=None,
            quantity=1,
        )
        mock_session = MagicMock()
        mock_session.id = 'cs_test_none_price'
        mock_session.url = 'https://checkout.stripe.com/pay'
        mock_session_create.return_value = mock_session

        with patch.object(order.items, 'all', return_value=[item]):
            StripeService.create_checkout_session(order)

        mock_session_create.assert_called_once()
        _, kwargs = mock_session_create.call_args
        self.assertEqual(len(kwargs['line_items']), 1)
        self.assertEqual(kwargs['line_items'][0]['price_data']['unit_amount'], 20000)

    def test_handle_checkout_session_completed_marks_paid(self):
        self.order.stripe_session_id = 'cs_test_webhook_001'
        self.order.save()

        session_data = {
            'id': 'cs_test_webhook_001',
            'client_reference_id': self.order.order_number,
            'payment_intent': 'pi_3MtwBwLkdIwHu7ix28a3tqPa',
            'metadata': {'order_number': self.order.order_number, 'order_id': str(self.order.id)},
        }

        updated_order = StripeService.handle_checkout_session_completed(session_data)

        self.assertIsNotNone(updated_order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)
        self.assertIn(self.order.status, [OrderStatus.PAID, OrderStatus.PREPARING])
        self.assertEqual(self.order.stripe_payment_intent_id, 'pi_3MtwBwLkdIwHu7ix28a3tqPa')

    @override_settings(STRIPE_ORDER_SUCCESS_STATUS='PREPARING')
    def test_handle_checkout_session_completed_transitions_to_preparing(self):
        self.order.stripe_session_id = 'cs_test_preparing_001'
        self.order.save()

        session_data = {
            'id': 'cs_test_preparing_001',
            'client_reference_id': self.order.order_number,
            'payment_intent': 'pi_preparing_123',
            'metadata': {'order_number': self.order.order_number},
        }

        StripeService.handle_checkout_session_completed(session_data, target_status=OrderStatus.PREPARING)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PREPARING)
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)

    def test_handle_checkout_session_completed_unknown_order_returns_none(self):
        session_data = {
            'id': 'cs_non_existent',
            'client_reference_id': 'DB-99999',
            'metadata': {},
        }
        result = StripeService.handle_checkout_session_completed(session_data)
        self.assertIsNone(result)

    def test_handle_payment_failed_marks_failed(self):
        self.order.stripe_payment_intent_id = 'pi_failed_123'
        self.order.save()

        payment_intent = {
            'id': 'pi_failed_123',
            'metadata': {'order_number': self.order.order_number},
        }

        updated = StripeService.handle_payment_failed(payment_intent)
        self.assertIsNotNone(updated)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.FAILED)


class StripeCheckoutFlowTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.factory = RequestFactory()

        self.user = User.objects.create_user(
            email='buyer@doublebite.ua',
            password='StrongPassword123!',
            first_name='Михайло',
            last_name='Грушевський',
            phone='+380503332211',
        )
        self.category = Category.objects.create(
            name='Бургери',
            slug='burgers',
            is_active=True,
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Класичний Бургер',
            slug='classic-burger',
            price=Decimal('220.00'),
            weight_grams=350,
            is_available=True,
        )

    @override_settings(STRIPE_SECRET_KEY='sk_test_fake_secret_key')
    @patch('orders.services.StripeService.create_checkout_session')
    def test_checkout_post_online_redirects_to_stripe_session(self, mock_create_session):
        self.client.force_login(self.user)
        self.client.post(reverse('orders:cart_add', kwargs={'dish_id': self.dish.id}))

        mock_session = MagicMock()
        mock_session.url = 'https://checkout.stripe.com/pay/cs_test_session_url'
        mock_create_session.return_value = mock_session

        response = self.client.post(
            reverse('orders:checkout'),
            {
                'customer_name': 'Михайло Грушевський',
                'customer_phone': '+380503332211',
                'delivery_address': 'Київ, вул. Володимирська, 35',
                'payment_method': 'ONLINE',
                'notes': 'Телефонувати за 10 хв',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, 'https://checkout.stripe.com/pay/cs_test_session_url')
        mock_create_session.assert_called_once()

        order = Order.objects.filter(user=self.user).first()
        self.assertIsNotNone(order)
        self.assertEqual(order.payment_method, PaymentMethod.ONLINE)

    @override_settings(STRIPE_SECRET_KEY='sk_test_fake_secret_key')
    @patch('orders.services.StripeService.create_checkout_session')
    def test_checkout_post_stripe_method_redirects_to_stripe_session(self, mock_create_session):
        self.client.force_login(self.user)
        self.client.post(reverse('orders:cart_add', kwargs={'dish_id': self.dish.id}))

        mock_session = MagicMock()
        mock_session.url = 'https://checkout.stripe.com/pay/cs_stripe_url'
        mock_create_session.return_value = mock_session

        response = self.client.post(
            reverse('orders:checkout'),
            {
                'customer_name': 'Михайло',
                'customer_phone': '+380503332211',
                'delivery_address': 'Київ, вул. Володимирська, 35',
                'payment_method': 'STRIPE',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, 'https://checkout.stripe.com/pay/cs_stripe_url')

    def test_checkout_post_cash_redirects_to_tracking(self):
        self.client.force_login(self.user)
        self.client.post(reverse('orders:cart_add', kwargs={'dish_id': self.dish.id}))

        response = self.client.post(
            reverse('orders:checkout'),
            {
                'customer_name': 'Михайло',
                'customer_phone': '+380503332211',
                'delivery_address': 'Київ, вул. Володимирська, 35',
                'payment_method': 'CASH',
            },
        )

        order = Order.objects.filter(user=self.user).first()
        self.assertIsNotNone(order)
        self.assertEqual(order.payment_method, PaymentMethod.CASH)
        self.assertRedirects(response, reverse('orders:tracking', kwargs={'order_number': order.order_number}))

    @override_settings(STRIPE_SECRET_KEY='sk_test_fake_secret_key')
    @patch('orders.services.StripeService.create_checkout_session')
    def test_direct_stripe_checkout_view(self, mock_create_session):
        order = Order.objects.create(
            user=self.user,
            customer_name='Михайло',
            customer_phone='+380503332211',
            delivery_address='Київ',
            payment_method=PaymentMethod.ONLINE,
            total_amount=Decimal('220.00'),
        )
        mock_session = MagicMock()
        mock_session.url = 'https://checkout.stripe.com/pay/direct_link'
        mock_create_session.return_value = mock_session

        self.client.force_login(self.user)
        response = self.client.get(reverse('orders:stripe_checkout', kwargs={'order_number': order.order_number}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, 'https://checkout.stripe.com/pay/direct_link')

    def test_direct_stripe_checkout_already_paid_redirects_tracking(self):
        order = Order.objects.create(
            user=self.user,
            customer_name='Михайло',
            customer_phone='+380503332211',
            delivery_address='Київ',
            status=OrderStatus.PAID,
            payment_status=PaymentStatus.PAID,
            total_amount=Decimal('220.00'),
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('orders:stripe_checkout', kwargs={'order_number': order.order_number}))
        self.assertRedirects(response, reverse('orders:tracking', kwargs={'order_number': order.order_number}))

    def test_direct_stripe_checkout_forbidden_for_other_user(self):
        other_user = User.objects.create_user(
            email='other@doublebite.ua',
            password='Password123!',
        )
        order = Order.objects.create(
            user=self.user,
            customer_name='Михайло',
            customer_phone='+380503332211',
            delivery_address='Київ',
            payment_method=PaymentMethod.ONLINE,
            total_amount=Decimal('220.00'),
        )
        self.client.force_login(other_user)
        response = self.client.get(reverse('orders:stripe_checkout', kwargs={'order_number': order.order_number}))
        self.assertEqual(response.status_code, 403)


class StripeWebhookTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.webhook_url = reverse('orders:stripe_webhook')

        self.user = User.objects.create_user(
            email='webhook.user@doublebite.ua',
            password='StrongPassword123!',
            first_name='Тарас',
            last_name='Шевченко',
        )
        self.order = Order.objects.create(
            user=self.user,
            customer_name='Тарас Шевченко',
            customer_phone='+380501112233',
            delivery_address='Київ, вул. Шовковична, 5',
            payment_method=PaymentMethod.ONLINE,
            status=OrderStatus.PENDING,
            payment_status=PaymentStatus.PENDING,
            total_amount=Decimal('450.00'),
        )

    def test_webhook_get_method_not_allowed(self):
        response = self.client.get(self.webhook_url)
        self.assertEqual(response.status_code, 405)

    def test_webhook_missing_signature_header_returns_400(self):
        response = self.client.post(
            self.webhook_url,
            data=json.dumps({'type': 'checkout.session.completed'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('Missing signature', response.content.decode('utf-8'))

    @override_settings(STRIPE_WEBHOOK_SECRET='')
    @patch.dict('os.environ', {'STRIPE_WEBHOOK_SECRET': ''})
    def test_webhook_missing_secret_returns_500(self):
        response = self.client.post(
            self.webhook_url,
            data=json.dumps({'type': 'checkout.session.completed'}),
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='t=123,v1=sig',
        )
        self.assertEqual(response.status_code, 500)
        self.assertIn('Webhook secret not configured', response.content.decode('utf-8'))

    @override_settings(STRIPE_WEBHOOK_SECRET='whsec_test_secret')
    @patch('stripe.Webhook.construct_event')
    def test_webhook_invalid_signature_returns_400(self, mock_construct):
        mock_construct.side_effect = stripe.SignatureVerificationError('Invalid signature', 'sig_header')

        response = self.client.post(
            self.webhook_url,
            data=json.dumps({'type': 'checkout.session.completed'}),
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='t=123,v1=bad_sig',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid signature', response.content.decode('utf-8'))

    @override_settings(STRIPE_WEBHOOK_SECRET='whsec_test_secret')
    @patch('stripe.Webhook.construct_event')
    def test_webhook_invalid_payload_returns_400(self, mock_construct):
        mock_construct.side_effect = ValueError('Invalid JSON')

        response = self.client.post(
            self.webhook_url,
            data='invalid json data',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='t=123,v1=sig',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid payload', response.content.decode('utf-8'))

    @override_settings(STRIPE_WEBHOOK_SECRET='whsec_test_secret')
    @patch('stripe.Webhook.construct_event')
    def test_webhook_checkout_session_completed_marks_order_paid(self, mock_construct):
        self.order.stripe_session_id = 'cs_wh_test_123'
        self.order.save()

        mock_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_wh_test_123',
                    'client_reference_id': self.order.order_number,
                    'payment_intent': 'pi_test_intent_999',
                    'metadata': {
                        'order_number': self.order.order_number,
                        'order_id': str(self.order.id),
                    },
                }
            },
        }
        mock_construct.return_value = mock_event

        response = self.client.post(
            self.webhook_url,
            data=json.dumps(mock_event),
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='t=123,v1=valid_sig',
        )

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)
        self.assertIn(self.order.status, [OrderStatus.PAID, OrderStatus.PREPARING])
        self.assertEqual(self.order.stripe_payment_intent_id, 'pi_test_intent_999')

    @override_settings(STRIPE_WEBHOOK_SECRET='whsec_test_secret')
    @patch('stripe.Webhook.construct_event')
    def test_webhook_payment_intent_failed_marks_order_failed(self, mock_construct):
        self.order.stripe_payment_intent_id = 'pi_test_fail_777'
        self.order.save()

        mock_event = {
            'type': 'payment_intent.payment_failed',
            'data': {
                'object': {
                    'id': 'pi_test_fail_777',
                    'metadata': {'order_number': self.order.order_number},
                }
            },
        }
        mock_construct.return_value = mock_event

        response = self.client.post(
            self.webhook_url,
            data=json.dumps(mock_event),
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='t=123,v1=valid_sig',
        )

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.FAILED)

    @override_settings(STRIPE_WEBHOOK_SECRET='whsec_test_secret')
    @patch('stripe.Webhook.construct_event')
    def test_webhook_async_payment_succeeded(self, mock_construct):
        self.order.stripe_session_id = 'cs_wh_async_123'
        self.order.save()

        mock_event = {
            'type': 'checkout.session.async_payment_succeeded',
            'data': {
                'object': {
                    'id': 'cs_wh_async_123',
                    'client_reference_id': self.order.order_number,
                    'payment_intent': 'pi_async_999',
                    'metadata': {'order_number': self.order.order_number},
                }
            },
        }
        mock_construct.return_value = mock_event

        response = self.client.post(
            self.webhook_url,
            data=json.dumps(mock_event),
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='t=123,v1=valid_sig',
        )

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)

    @override_settings(STRIPE_WEBHOOK_SECRET='whsec_test_secret')
    @patch('stripe.Webhook.construct_event')
    def test_webhook_charge_refunded_marks_order_refunded(self, mock_construct):
        self.order.stripe_payment_intent_id = 'pi_refund_123'
        self.order.payment_status = PaymentStatus.PAID
        self.order.save()

        mock_event = {
            'type': 'charge.refunded',
            'data': {
                'object': {
                    'payment_intent': 'pi_refund_123',
                    'metadata': {'order_number': self.order.order_number},
                }
            },
        }
        mock_construct.return_value = mock_event

        response = self.client.post(
            self.webhook_url,
            data=json.dumps(mock_event),
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='t=123,v1=valid_sig',
        )

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.REFUNDED)

    @override_settings(STRIPE_WEBHOOK_SECRET='whsec_test_secret')
    @patch('stripe.Webhook.construct_event')
    def test_webhook_unhandled_event_returns_200(self, mock_construct):
        mock_event = {
            'type': 'customer.subscription.created',
            'data': {'object': {}},
        }
        mock_construct.return_value = mock_event

        response = self.client.post(
            self.webhook_url,
            data=json.dumps(mock_event),
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='t=123,v1=valid_sig',
        )
        self.assertEqual(response.status_code, 200)
