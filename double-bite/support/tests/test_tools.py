from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from menu.models import Category, Dish, DishOption
from orders.models import Order, OrderStatus
from orders.services import CartService

from support.agent.tools import SUPPORT_AGENT_TOOLS, execute_agent_tool
from support.models import ChatSession, FAQKnowledge, SupportTicket, TicketReason

User = get_user_model()


class AgentToolsTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            email='tools_user@example.com',
            password='password123',
            phone='+380501234567',
        )
        self.other_user = User.objects.create_user(
            email='other_user@example.com',
            password='password123',
        )

        self.category_pizza = Category.objects.create(
            name='Піца',
            slug='pizza',
            display_order=1,
            is_active=True,
        )
        self.category_drinks = Category.objects.create(
            name='Напої',
            slug='drinks',
            display_order=2,
            is_active=True,
        )

        self.dish_margarita = Dish.objects.create(
            category=self.category_pizza,
            title='Піца Маргарита',
            slug='pizza-margarita',
            description='Класична італійська піца з томатами та моцарелою',
            price=Decimal('220.00'),
            weight_grams=450,
            calories=650,
            allergens='лактоза, глютен',
            is_vegetarian=True,
            is_available=True,
        )
        self.dish_unavailable = Dish.objects.create(
            category=self.category_pizza,
            title='Трюфельна піца',
            slug='truffle-pizza',
            description='Сезонна піца',
            price=Decimal('350.00'),
            weight_grams=500,
            calories=750,
            is_available=False,
        )
        self.option_cheese = DishOption.objects.create(
            dish=self.dish_margarita,
            name='Подвійний сир',
            price_delta=Decimal('45.00'),
        )

        self.order = Order.objects.create(
            user=self.user,
            order_number='DB-3001',
            customer_name='Олексій',
            customer_phone='+380501234567',
            delivery_address='вул. Велика Васильківська, 20',
            status=OrderStatus.ON_WAY,
            total_amount=Decimal('265.00'),
            eta_minutes=20,
        )

        self.faq = FAQKnowledge.objects.create(
            question='Яка мінімальна сума замовлення?',
            answer='Мінімальна сума становить 200 грн.',
            category='Доставка',
            is_active=True,
        )

        self.session = ChatSession.objects.create(user=self.user)

    def _get_request(self, user=None):
        request = self.factory.post('/support/chat/stream/')
        request.user = user or self.user
        request.session = self.client.session
        return request

    def test_tools_schema_definitions(self):
        tool_names = [t['function']['name'] for t in SUPPORT_AGENT_TOOLS]
        self.assertIn('check_order_status', tool_names)
        self.assertIn('search_dishes', tool_names)
        self.assertIn('get_faq_answer', tool_names)
        self.assertIn('escalate_to_operator', tool_names)
        self.assertIn('add_to_cart', tool_names)
        self.assertIn('remove_from_cart', tool_names)
        self.assertIn('view_cart', tool_names)

    def test_check_order_status_success(self):
        req = self._get_request(self.user)
        result = execute_agent_tool('check_order_status', {'order_id': self.order.id}, request=req)
        self.assertEqual(result['order_id'], self.order.id)
        self.assertEqual(result['order_number'], 'DB-3001')
        self.assertEqual(result['status'], OrderStatus.ON_WAY)
        self.assertEqual(result['eta_minutes'], 20)

    def test_check_order_status_idor_blocked(self):
        req_other = self._get_request(self.other_user)
        result = execute_agent_tool('check_order_status', {'order_id': self.order.id}, request=req_other)
        self.assertIn('error', result)
        self.assertEqual(result['error'], 'Замовлення не знайдено або доступ заборонено')

    def test_search_dishes(self):
        result = execute_agent_tool(
            'search_dishes',
            {
                'query': 'Маргарита',
                'is_vegetarian': True,
                'max_price': 300,
            }
        )
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['dishes'][0]['title'], 'Піца Маргарита')
        self.assertEqual(result['dishes'][0]['price'], 220.0)

    def test_get_faq_answer(self):
        result = execute_agent_tool('get_faq_answer', {'query': 'мінімальна сума'})
        self.assertGreaterEqual(result['count'], 1)
        self.assertIn('200 грн', result['faqs'][0]['answer'])

    def test_escalate_to_operator(self):
        req = self._get_request(self.user)
        result = execute_agent_tool(
            'escalate_to_operator',
            {
                'reason': TicketReason.COMPLAINT,
                'details': 'Запізнення на 50 хвилин',
                'order_id': self.order.id,
            },
            request=req,
            session=self.session,
        )
        self.assertIsNotNone(result['ticket_id'])
        self.assertEqual(result['status'], 'OPEN')
        self.assertEqual(result['support_phone'], '+380 44 123 45 67')

        ticket = SupportTicket.objects.get(id=result['ticket_id'])
        self.assertEqual(ticket.details, 'Запізнення на 50 хвилин')
        self.session.refresh_from_db()
        self.assertTrue(self.session.is_escalated)

    def test_add_and_view_cart(self):
        req = self._get_request(self.user)

        add_res = execute_agent_tool(
            'add_to_cart',
            {'dish_id': self.dish_margarita.id, 'quantity': 2},
            request=req,
            session=self.session,
        )
        self.assertTrue(add_res['success'])
        self.assertEqual(add_res['quantity'], 2)
        self.assertEqual(add_res['dish_title'], 'Піца Маргарита')
        self.assertEqual(add_res['cart_items_count'], 2)
        self.assertEqual(add_res['total_amount'], 440.0)

        view_res = execute_agent_tool('view_cart', {}, request=req)
        self.assertEqual(len(view_res['items']), 1)
        self.assertEqual(view_res['items'][0]['title'], 'Піца Маргарита')
        self.assertEqual(view_res['total_amount'], 440.0)

    def test_add_unavailable_dish_to_cart(self):
        req = self._get_request(self.user)
        add_res = execute_agent_tool(
            'add_to_cart',
            {'dish_id': self.dish_unavailable.id, 'quantity': 1},
            request=req,
        )
        self.assertIn('error', add_res)

    def test_remove_from_cart(self):
        req = self._get_request(self.user)
        CartService.add_dish(req, self.dish_margarita.id, quantity=1)

        rem_res = execute_agent_tool(
            'remove_from_cart',
            {'dish_id': self.dish_margarita.id},
            request=req,
        )
        self.assertTrue(rem_res['success'])
        self.assertEqual(rem_res['cart_items_count'], 0)

    def test_unknown_tool_returns_error(self):
        result = execute_agent_tool('make_coffee', {})
        self.assertIn('error', result)
