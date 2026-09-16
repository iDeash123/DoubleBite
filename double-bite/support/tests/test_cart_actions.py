import json
import os
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import AsyncClient, RequestFactory, TestCase
from menu.models import Category, Dish, DishOption
from orders.models import Cart
from orders.services import CartService

from support.agent.tools import execute_agent_tool
from support.models import ChatSession

os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'

User = get_user_model()


async def mock_stream_with_cart_tool(*args, **kwargs):
    yield {
        'cart_mutation': True,
        'cart_items_count': 2,
        'total_amount': 560.0,
        'action': 'add_to_cart',
        'dish_id': 1,
    }
    yield {'token': 'Додав дві порції піци до вашого кошика!'}


class AutonomousCartActionsTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.async_client = AsyncClient()
        self.user = User.objects.create_user(
            email='cart_ai@example.com',
            password='password123',
            phone='+380501234567',
        )
        self.category = Category.objects.create(name='Піца', slug='pizza', is_active=True)
        self.dish_pepperoni = Dish.objects.create(
            category=self.category,
            title='Пепероні',
            slug='pepperoni',
            price=Decimal('280.00'),
            weight_grams=480,
            calories=720,
            is_available=True,
        )
        self.dish_unavailable = Dish.objects.create(
            category=self.category,
            title='Сезонна піца',
            slug='seasonal',
            price=Decimal('310.00'),
            weight_grams=450,
            calories=690,
            is_available=False,
        )
        self.option_spicy = DishOption.objects.create(
            dish=self.dish_pepperoni,
            name='Гострий перець халапеньйо',
            price_delta=Decimal('35.00'),
        )
        self.session = ChatSession.objects.create(user=self.user)

    def _get_request(self):
        request = self.factory.post('/support/chat/stream/')
        request.user = self.user
        request.session = self.client.session
        return request

    def test_autonomous_add_to_cart_without_options(self):
        req = self._get_request()
        result = execute_agent_tool(
            'add_to_cart',
            {'dish_id': self.dish_pepperoni.id, 'quantity': 2},
            request=req,
            session=self.session,
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['cart_items_count'], 2)
        self.assertEqual(result['total_amount'], 560.0)

        cart = Cart.objects.filter(user=self.user).first()
        self.assertIsNotNone(cart)
        self.assertEqual(cart.items.count(), 1)
        item = cart.items.first()
        self.assertEqual(item.dish, self.dish_pepperoni)
        self.assertEqual(item.quantity, 2)

    def test_autonomous_add_to_cart_with_option(self):
        req = self._get_request()
        result = execute_agent_tool(
            'add_to_cart',
            {
                'dish_id': self.dish_pepperoni.id,
                'quantity': 1,
                'option_id': self.option_spicy.id,
            },
            request=req,
            session=self.session,
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['total_amount'], 315.0)

        cart = Cart.objects.filter(user=self.user).first()
        item = cart.items.first()
        self.assertEqual(len(item.selected_options), 1)
        self.assertEqual(item.selected_options[0]['name'], 'Гострий перець халапеньйо')

    def test_autonomous_add_unavailable_dish_returns_error(self):
        req = self._get_request()
        result = execute_agent_tool(
            'add_to_cart',
            {'dish_id': self.dish_unavailable.id, 'quantity': 1},
            request=req,
            session=self.session,
        )
        self.assertIn('error', result)
        cart = Cart.objects.filter(user=self.user).first()
        self.assertTrue(cart is None or cart.items.count() == 0)

    def test_autonomous_view_cart(self):
        req = self._get_request()
        CartService.add_dish(req, self.dish_pepperoni.id, quantity=1)

        result = execute_agent_tool('view_cart', {}, request=req, session=self.session)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['title'], 'Пепероні')
        self.assertEqual(result['cart_items_count'], 1)
        self.assertEqual(result['total_amount'], 280.0)

    def test_autonomous_remove_from_cart(self):
        req = self._get_request()
        item = CartService.add_dish(req, self.dish_pepperoni.id, quantity=1)

        result = execute_agent_tool(
            'remove_from_cart',
            {'item_id': item.id},
            request=req,
            session=self.session,
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['cart_items_count'], 0)
        self.assertEqual(result['total_amount'], 0.0)

        cart = Cart.objects.filter(user=self.user).first()
        self.assertEqual(cart.items.count(), 0)

    def test_autonomous_add_to_cart_by_name(self):
        req = self._get_request()
        result = execute_agent_tool(
            'add_to_cart',
            {'dish_name': 'Пепероні', 'quantity': 2},
            request=req,
            session=self.session,
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['dish_title'], 'Пепероні')
        self.assertEqual(result['cart_items_count'], 2)
        self.assertEqual(result['total_amount'], 560.0)

    def test_autonomous_update_cart_quantity_by_name(self):
        req = self._get_request()
        CartService.add_dish(req, self.dish_pepperoni.id, quantity=4)

        result = execute_agent_tool(
            'update_cart_quantity',
            {'dish_name': 'Пепероні', 'quantity': 2},
            request=req,
            session=self.session,
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['action'], 'update_cart_quantity')
        self.assertEqual(result['quantity'], 2)
        self.assertEqual(result['cart_items_count'], 2)
        self.assertEqual(result['total_amount'], 560.0)

    def test_autonomous_update_cart_quantity_to_zero_removes(self):
        req = self._get_request()
        CartService.add_dish(req, self.dish_pepperoni.id, quantity=2)

        result = execute_agent_tool(
            'update_cart_quantity',
            {'dish_name': 'Пепероні', 'quantity': 0},
            request=req,
            session=self.session,
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['cart_items_count'], 0)
        self.assertEqual(result['total_amount'], 0.0)

    def test_autonomous_remove_from_cart_by_name(self):
        req = self._get_request()
        CartService.add_dish(req, self.dish_pepperoni.id, quantity=1)

        result = execute_agent_tool(
            'remove_from_cart',
            {'dish_name': 'Пепероні'},
            request=req,
            session=self.session,
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['cart_items_count'], 0)
        cart = Cart.objects.filter(user=self.user).first()
        self.assertEqual(cart.items.count(), 0)

    @patch('support.agent.client.MistralSupportAgent.stream_chat_response', side_effect=mock_stream_with_cart_tool)
    async def test_sse_stream_cart_mutation_event(self, mock_stream):
        await self.async_client.aforce_login(self.user)
        response = await self.async_client.post(
            '/support/chat/stream/',
            data=json.dumps({'message': 'Додай дві пепероні'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)

        chunks = []
        async for chunk in response.streaming_content:
            chunks.append(chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk)

        joined = ''.join(chunks)
        self.assertIn('cart_mutation', joined)
        self.assertIn('560.0', joined)
        self.assertIn('Додав дві порції піци', joined)
