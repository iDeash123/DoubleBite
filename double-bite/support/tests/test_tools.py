from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from menu.models import Category, Dish, DishOption
from orders.models import Cart, Order, OrderStatus
from orders.services import CartService

from support.agent.tools import SUPPORT_AGENT_TOOLS, _find_dish, execute_agent_tool
from support.models import (
    ChatSession,
    FAQKnowledge,
    SupportTicket,
    TicketReason,
)
from support.services import ChatSessionService

User = get_user_model()


class AgentToolsTest(TestCase):
    def setUp(self):
        cache.clear()
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
        self.category_inactive = Category.objects.create(
            name='Архів',
            slug='archive-cat',
            display_order=99,
            is_active=False,
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
        self.option_cheese = DishOption.objects.create(
            dish=self.dish_margarita,
            name='Подвійний сир',
            price_delta=Decimal('40.00'),
        )
        self.option_truffle = DishOption.objects.create(
            dish=self.dish_margarita,
            name='Трюфельна олія',
            price_delta=Decimal('55.00'),
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
        self.dish_inactive_category = Dish.objects.create(
            category=self.category_inactive,
            title='Архівна Піца',
            slug='archive-pizza',
            price=Decimal('210.00'),
            weight_grams=400,
            calories=600,
            is_available=True,
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
        self.other_order = Order.objects.create(
            user=self.other_user,
            order_number='DB-9991',
            customer_name='Інший',
            customer_phone='+380509999999',
            delivery_address='вул. Інша, 1',
            status=OrderStatus.PENDING,
            total_amount=300.00,
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

        by_name = {t['function']['name']: t['function'] for t in SUPPORT_AGENT_TOOLS}
        self.assertEqual(
            by_name['check_order_status']['parameters']['properties']['order_id']['type'],
            'string',
        )
        self.assertEqual(
            by_name['escalate_to_operator']['parameters']['properties']['order_id']['type'],
            'string',
        )
        for cart_tool in ('update_cart_quantity', 'remove_from_cart'):
            props = by_name[cart_tool]['parameters']['properties']
            self.assertIn('cart_item_id', props)
            self.assertIn('option_id', props)
            self.assertIn('option_name', props)

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
            },
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
        self.assertEqual(ticket.order, self.order)
        self.session.refresh_from_db()
        self.assertTrue(self.session.is_escalated)

    def test_escalate_to_operator_accepts_string_order_number_and_blocks_idor(self):
        req = self._get_request(self.user)
        for order_ref in ('DB-3001', '#DB-3001'):
            fresh_session = ChatSessionService.reset_active_session(req)
            res = execute_agent_tool(
                'escalate_to_operator',
                {
                    'reason': TicketReason.ORDER_ISSUE,
                    'details': f'Питання по {order_ref}',
                    'order_id': order_ref,
                },
                request=req,
                session=fresh_session,
            )
            ticket = SupportTicket.objects.get(id=res['ticket_id'])
            self.assertEqual(ticket.order, self.order)

        idor_session = ChatSessionService.reset_active_session(req)
        res_idor = execute_agent_tool(
            'escalate_to_operator',
            {
                'reason': TicketReason.ORDER_ISSUE,
                'details': 'Чуже замовлення',
                'order_id': 'DB-9991',
            },
            request=req,
            session=idor_session,
        )
        idor_ticket = SupportTicket.objects.get(id=res_idor['ticket_id'])
        self.assertIsNone(idor_ticket.order)

    def test_add_and_view_cart_includes_selected_options(self):
        req = self._get_request(self.user)

        add_res = execute_agent_tool(
            'add_to_cart',
            {
                'dish_id': self.dish_margarita.id,
                'quantity': 2,
                'option_id': self.option_cheese.id,
            },
            request=req,
            session=self.session,
        )
        self.assertTrue(add_res['success'])
        self.assertEqual(add_res['quantity'], 2)
        self.assertEqual(add_res['dish_title'], 'Піца Маргарита')
        self.assertEqual(add_res['cart_items_count'], 2)
        self.assertEqual(add_res['total_amount'], 520.0)

        view_res = execute_agent_tool('view_cart', {}, request=req)
        self.assertEqual(len(view_res['items']), 1)
        self.assertEqual(view_res['items'][0]['title'], 'Піца Маргарита')
        self.assertEqual(len(view_res['items'][0]['selected_options']), 1)
        self.assertEqual(
            view_res['items'][0]['selected_options'][0]['name'],
            'Подвійний сир',
        )
        self.assertEqual(view_res['total_amount'], 520.0)

    def test_add_unavailable_or_invalid_option_returns_error(self):
        req = self._get_request(self.user)
        add_res = execute_agent_tool(
            'add_to_cart',
            {'dish_id': self.dish_unavailable.id, 'quantity': 1},
            request=req,
        )
        self.assertIn('error', add_res)

        opt_res = execute_agent_tool(
            'add_to_cart',
            {'dish_id': self.dish_margarita.id, 'quantity': 1, 'option_id': 999999},
            request=req,
        )
        self.assertIn('error', opt_res)

    def test_update_cart_quantity_returns_clamped_99(self):
        req = self._get_request(self.user)
        item = CartService.add_dish(req, self.dish_margarita.id, quantity=1)

        res = execute_agent_tool(
            'update_cart_quantity',
            {'cart_item_id': item.id, 'quantity': 500},
            request=req,
        )
        self.assertTrue(res['success'])
        self.assertEqual(res['quantity'], 99)
        self.assertEqual(res['cart_items_count'], 99)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 99)

    def test_ambiguous_cart_items_require_disambiguation(self):
        req = self._get_request(self.user)
        item_plain = CartService.add_dish(req, self.dish_margarita.id, quantity=1)
        item_cheese = CartService.add_dish(
            req, self.dish_margarita.id, quantity=1, option_id=self.option_cheese.id
        )

        ambig_update = execute_agent_tool(
            'update_cart_quantity',
            {'dish_id': self.dish_margarita.id, 'quantity': 3},
            request=req,
        )
        self.assertFalse(ambig_update['success'])
        self.assertTrue(ambig_update['ambiguous'])
        self.assertEqual(len(ambig_update['candidates']), 2)

        ambig_remove = execute_agent_tool(
            'remove_from_cart',
            {'dish_name': 'Піца Маргарита'},
            request=req,
        )
        self.assertFalse(ambig_remove['success'])
        self.assertTrue(ambig_remove['ambiguous'])
        self.assertEqual(len(ambig_remove['candidates']), 2)

        by_option_name = execute_agent_tool(
            'update_cart_quantity',
            {
                'dish_id': self.dish_margarita.id,
                'option_name': 'Подвійний сир',
                'quantity': 4,
            },
            request=req,
        )
        self.assertTrue(by_option_name['success'])
        self.assertEqual(by_option_name['cart_item_id'], item_cheese.id)
        self.assertEqual(by_option_name['quantity'], 4)

        by_item_id = execute_agent_tool(
            'remove_from_cart',
            {'cart_item_id': item_plain.id},
            request=req,
        )
        self.assertTrue(by_item_id['success'])
        self.assertEqual(by_item_id['cart_item_id'], item_plain.id)
        cart = Cart.objects.get(user=self.user)
        self.assertEqual(cart.items.count(), 1)
        self.assertEqual(cart.items.first().id, item_cheese.id)

    def test_find_dish_excludes_unavailable_and_handles_cyrillic_casefold(self):
        self.assertIsNone(_find_dish(dish_id=self.dish_unavailable.id))
        self.assertIsNone(_find_dish(dish_name='Трюфельна піца'))
        self.assertIsNone(_find_dish(dish_id=self.dish_inactive_category.id))
        self.assertIsNone(_find_dish(dish_name='Архівна Піца'))

        found_lower = _find_dish(dish_name='піца маргарита')
        found_upper = _find_dish(dish_name='ПІЦА МАРГАРИТА')
        self.assertEqual(found_lower, self.dish_margarita)
        self.assertEqual(found_upper, self.dish_margarita)

        self.assertIsNone(_find_dish(dish_name='Піца Екзотична Неіснуюча'))

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

    def test_escalate_to_operator_throttled_to_single_open_ticket(self):
        req = self._get_request(self.user)
        res1 = execute_agent_tool(
            'escalate_to_operator',
            {'reason': TicketReason.COMPLAINT, 'details': 'Перша скарга'},
            request=req,
            session=self.session,
        )
        res2 = execute_agent_tool(
            'escalate_to_operator',
            {'reason': TicketReason.BOT_CONFUSED, 'details': 'Друга скарга'},
            request=req,
            session=self.session,
        )
        self.assertEqual(res1['ticket_id'], res2['ticket_id'])
        self.assertEqual(SupportTicket.objects.filter(session=self.session).count(), 1)

    def test_escalation_truncates_details_and_sanitizes_phone(self):
        req = self._get_request(self.user)
        new_session = ChatSessionService.reset_active_session(req)
        long_details = 'A' * 2000
        res = execute_agent_tool(
            'escalate_to_operator',
            {
                'reason': TicketReason.COMPLAINT,
                'details': long_details,
                'customer_phone': 'bad-phone-number',
            },
            request=req,
            session=new_session,
        )
        ticket = SupportTicket.objects.get(id=res['ticket_id'])
        self.assertEqual(len(ticket.details), 1000)
        self.assertEqual(ticket.customer_phone, '')

    def test_add_to_cart_staff_forbidden(self):
        from accounts.models import Role

        courier = User.objects.create_user(
            email='courier_tool@example.com',
            password='password123',
            role=Role.COURIER,
        )
        req = self._get_request(courier)
        res = execute_agent_tool(
            'add_to_cart',
            {'dish_id': self.dish_margarita.id, 'quantity': 1},
            request=req,
        )
        self.assertIn('error', res)
        self.assertIn('Персонал ресторану', res['error'])

    def test_tool_arguments_invalid_input_boundaries(self):
        req = self._get_request(self.user)
        res_add = execute_agent_tool(
            'add_to_cart',
            {'dish_id': 'invalid', 'quantity': 'bad'},
            request=req,
        )
        self.assertIn('error', res_add)

        res_update = execute_agent_tool(
            'update_cart_quantity',
            {'dish_id': 'invalid', 'quantity': 'bad'},
            request=req,
        )
        self.assertIn('error', res_update)

        res_remove = execute_agent_tool(
            'remove_from_cart',
            {'dish_id': 'invalid'},
            request=req,
        )
        self.assertFalse(res_remove.get('success', True))
