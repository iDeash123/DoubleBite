from decimal import Decimal

import pytest
from accounts.models import Role, User
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from menu.models import Category, Dish, DishOption
from orders.models import OrderStatus, PaymentMethod
from orders.services import OrderService

from support.agent.tools import execute_agent_tool


@pytest.fixture
def test_setup():
    user = User.objects.create_user(
        email='e2e_user@doublebite.com',
        password='password123',
        first_name='Оксана',
        last_name='Петренко',
        phone='+380509876543',
        role=Role.CUSTOMER,
    )
    category = Category.objects.create(
        name='Піца',
        slug='pizza',
        is_active=True,
    )
    dish = Dish.objects.create(
        category=category,
        title='Піца Маргарита D.O.P.',
        slug='pizza-margherita-dop',
        description='Класична піца з томатами та моцарелою',
        price=Decimal('245.00'),
        weight_grams=430,
        calories=780,
        allergens='глютен, лактоза',
        is_vegetarian=True,
        is_available=True,
        image='dishes/pizza-margherita-dop.jpg',
    )
    option = DishOption.objects.create(
        dish=dish,
        name='Подвійна моцарела',
        price_delta=Decimal('45.00'),
    )
    return user, dish, option


def _build_request(user=None):
    factory = RequestFactory()
    request = factory.get('/')
    middleware = SessionMiddleware(lambda req: None)
    middleware.process_request(request)
    request.session.save()
    request.user = user
    return request


@pytest.mark.django_db
def test_customer_e2e_journey(test_setup):
    user, dish, option = test_setup
    request = _build_request(user)

    search_result = execute_agent_tool(
        'search_dishes',
        {'query': 'Маргарита'},
        request=request,
    )
    assert search_result['count'] >= 1
    found_dish = search_result['dishes'][0]
    assert found_dish['id'] == dish.id
    assert found_dish['title'] == dish.title

    add_result = execute_agent_tool(
        'add_to_cart',
        {'dish_id': dish.id, 'quantity': 2, 'option_id': option.id},
        request=request,
    )
    assert add_result['success'] is True
    assert add_result['dish_title'] == dish.title
    assert add_result['quantity'] == 2

    cart_result = execute_agent_tool(
        'view_cart',
        {},
        request=request,
    )
    assert len(cart_result['items']) == 1
    assert cart_result['cart_items_count'] == 2
    assert cart_result['total_amount'] == 580.0

    order = OrderService.create_order_from_cart(
        request=request,
        customer_name='Оксана Петренко',
        customer_phone='+380509876543',
        delivery_address='м. Київ, вул. Хрещатик, 10, кв. 5',
        payment_method=PaymentMethod.CASH,
    )
    assert order is not None
    assert order.user == user
    assert order.total_amount == Decimal('580.00')
    assert order.items.count() == 1
    assert order.status in (OrderStatus.PENDING, OrderStatus.PREPARING)

    status_result = execute_agent_tool(
        'check_order_status',
        {'order_id': order.id},
        request=request,
    )
    assert 'error' not in status_result
    assert status_result['order_id'] == order.id
    assert status_result['order_number'] == order.order_number
    assert status_result['status'] == order.status
    assert status_result['address'] == order.delivery_address
