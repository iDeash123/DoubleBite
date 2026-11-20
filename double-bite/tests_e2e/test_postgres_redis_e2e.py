import asyncio
import hashlib
import hmac
import json
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

from accounts.models import DeliveryAddress, User
from asgiref.sync import sync_to_async
from config.security import build_rate_limit_key, consume_rate_limit, reset_rate_limit
from config.transactions import schedule_on_commit
from django.core.cache import DEFAULT_CACHE_ALIAS, cache, caches
from django.core.cache.backends.redis import RedisCache
from django.db import IntegrityError, connection, connections, transaction
from django.test import (
    AsyncClient,
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse
from menu.management.commands.load_snapshot import Command as LoadSnapshotCommand
from menu.models import Category, Dish, DishOption
from menu.selectors import filter_dishes, search_dishes_for_agent
from orders.middleware import GUEST_TOKEN_SESSION_KEY, PRE_LOGIN_GUEST_TOKEN_SESSION_KEY
from orders.models import (
    Cart,
    CartItem,
    Order,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    ProcessedStripeEvent,
)
from orders.services import CartService
from support.models import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
    MessageRole,
    SupportTicket,
    TicketReason,
)
from support.services import ChatSessionService
from support.vector_search import generate_mock_embedding, search_dishes_semantic


def _sign_stripe_payload(payload: bytes, secret: str) -> str:
    timestamp = str(int(time.time()))
    signed_payload = f'{timestamp}.{payload.decode("utf-8")}'.encode()
    signature = hmac.new(secret.encode('utf-8'), signed_payload, hashlib.sha256).hexdigest()
    return f't={timestamp},v1={signature}'


async def _mock_e2e_stream_tokens(messages, request=None, session=None):
    yield {'token': 'Дякую за запит! '}
    yield {'token': 'Готуємо вашу піцу.'}


@unittest.skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL + pgvector')
class PostgresPgvectorIntegrationTests(TestCase):
    def setUp(self):
        self.pizza_cat = Category.objects.create(
            name='Авторська Піца',
            slug='artisan-pizza-e2e',
            display_order=1,
            is_active=True,
        )
        self.inactive_cat = Category.objects.create(
            name='Архівна Категорія',
            slug='archived-cat-e2e',
            display_order=2,
            is_active=False,
        )

    def test_pgvector_extension_and_hnsw_index_exist(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            ext_row = cursor.fetchone()
            self.assertIsNotNone(ext_row, 'pgvector extension must be installed in PostgreSQL')

            cursor.execute(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE tablename = %s AND indexname = %s
                """,
                [Dish._meta.db_table, 'dish_embedding_hnsw_idx'],
            )
            idx_row = cursor.fetchone()

        self.assertIsNotNone(idx_row, 'HNSW index dish_embedding_hnsw_idx must exist on Dish')
        index_def = idx_row[1].lower()
        self.assertIn('using hnsw', index_def)
        self.assertIn('vector_cosine_ops', index_def)

    def test_pgvector_cosine_distance_sql_query_and_filters(self):
        high_sim_vec = generate_mock_embedding('піца неаполітанська моцарела томати базилік')
        for idx in range(4):
            Dish.objects.create(
                category=self.pizza_cat,
                title=f'Преміум Мʼясна Піца {idx}',
                slug=f'premium-meat-pizza-{idx}',
                description='піца неаполітанська моцарела томати базилік',
                price=Decimal('490.00'),
                weight_grams=500,
                calories=1100,
                is_vegetarian=False,
                is_available=True,
                embedding=high_sim_vec,
            )

        Dish.objects.create(
            category=self.inactive_cat,
            title='Неактивна Вег Піца',
            slug='inactive-veg-pizza',
            description='піца неаполітанська моцарела томати базилік',
            price=Decimal('180.00'),
            weight_grams=380,
            calories=500,
            is_vegetarian=True,
            is_available=True,
            embedding=high_sim_vec,
        )

        matching_veg = Dish.objects.create(
            category=self.pizza_cat,
            title='Веганська Маргарита Лайт',
            slug='vegan-margherita-light',
            description='піца моцарела томати базилік',
            price=Decimal('210.00'),
            weight_grams=380,
            calories=520,
            is_vegetarian=True,
            is_available=True,
            embedding=generate_mock_embedding('піца моцарела томати базилік'),
        )

        with patch(
            'support.vector_search.cosine_similarity',
            side_effect=AssertionError('PostgreSQL pgvector SQL path must be used instead of Python fallback'),
        ):
            semantic_hits = search_dishes_semantic(
                'піца неаполітанська моцарела томати',
                limit=2,
                max_price=Decimal('250.00'),
                is_vegetarian=True,
                max_calories=600,
            )
            self.assertEqual([d.pk for d in semantic_hits], [matching_veg.pk])

            agent_hits = search_dishes_for_agent(
                query='піца неаполітанська моцарела томати',
                max_price=Decimal('250.00'),
                is_vegetarian=True,
                max_calories=600,
                limit=2,
            )
            self.assertEqual([item['id'] for item in agent_hits], [matching_veg.id])

            unrelated = search_dishes_semantic('космоліт астероїд галактика трактор', limit=5)
            self.assertEqual(unrelated, [])

    def test_postgres_cyrillic_icontains_native_search(self):
        borscht = Dish.objects.create(
            category=self.pizza_cat,
            title='Український Борщ із Пампушками',
            slug='ukrainian-borscht-e2e',
            description='Насичений червоний борщ зі сметаною та часниковими пампушками',
            price=Decimal('195.00'),
            weight_grams=400,
            calories=420,
            allergens='Лактоза, Глютен',
            is_available=True,
            embedding=None,
        )
        salad = Dish.objects.create(
            category=self.pizza_cat,
            title='Італійський Салат Капрезе',
            slug='italian-caprese-e2e',
            description='Свіжі томати та базилік',
            price=Decimal('225.00'),
            weight_grams=280,
            calories=310,
            allergens='Горіхи',
            is_available=True,
            embedding=None,
        )

        for query in ('борщ', 'БОРЩ', 'Борщ', 'ПАМПУШКАМИ'):
            qs = filter_dishes(query=query)
            self.assertEqual([d.pk for d in qs], [borscht.pk], f'Failed Cyrillic query: {query}')

        filtered_no_gluten = filter_dishes(exclude_allergens=['глютен'])
        pks = [d.pk for d in filtered_no_gluten]
        self.assertNotIn(borscht.pk, pks)
        self.assertIn(salad.pk, pks)

        text_fallback_hits = search_dishes_semantic('італійський салат', limit=5)
        self.assertIn(salad.pk, [d.pk for d in text_fallback_hits])

    def test_postgres_partial_unique_constraints_enforced(self):
        user = User.objects.create_user(
            email='Constraint.User@DoubleBite.UA',
            password='StrongPassword123!',
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(email='constraint.user@doublebite.ua', password='x')])

        DeliveryAddress.objects.create(
            user=user,
            title='Дім',
            city='Київ',
            street='вул. Хрещатик',
            building='1',
            is_default=True,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            DeliveryAddress.objects.bulk_create(
                [
                    DeliveryAddress(
                        user=user,
                        title='Офіс',
                        city='Київ',
                        street='вул. Саксаганського',
                        building='10',
                        is_default=True,
                    )
                ]
            )
        DeliveryAddress.objects.bulk_create(
            [
                DeliveryAddress(
                    user=user,
                    title='Додаткова 1',
                    city='Київ',
                    street='вул. Лесі Українки',
                    building='5',
                    is_default=False,
                ),
                DeliveryAddress(
                    user=user,
                    title='Додаткова 2',
                    city='Київ',
                    street='вул. Лесі Українки',
                    building='6',
                    is_default=False,
                ),
            ]
        )

        Cart.objects.create(user=user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Cart.objects.bulk_create([Cart(user=user)])

        Cart.objects.create(guest_token='pg-guest-cart-token-1')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Cart.objects.bulk_create([Cart(guest_token='pg-guest-cart-token-1')])

        ChatSession.objects.create(user=user, status=ChatSessionStatus.ACTIVE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ChatSession.objects.bulk_create([ChatSession(user=user, status=ChatSessionStatus.ESCALATED)])

        ChatSession.objects.create(guest_token='pg-guest-chat-1', status=ChatSessionStatus.ACTIVE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ChatSession.objects.bulk_create(
                [ChatSession(guest_token='pg-guest-chat-1', status=ChatSessionStatus.ACTIVE)]
            )

        ChatSession.objects.bulk_create(
            [
                ChatSession(user=user, status=ChatSessionStatus.RESOLVED),
                ChatSession(user=user, status=ChatSessionStatus.RESOLVED),
            ]
        )
        self.assertEqual(
            ChatSession.objects.filter(user=user, status=ChatSessionStatus.RESOLVED).count(),
            2,
        )

    def test_postgres_sequence_reset_after_explicit_pk_load(self):
        Category.objects.create(
            id=9999,
            name='Снапшот Категорія',
            slug='snapshot-seq-cat',
            display_order=99,
            is_active=True,
        )
        LoadSnapshotCommand()._reset_sequences(['menu'])

        auto_cat = Category.objects.create(
            name='Авто Категорія Після Скидання',
            slug='auto-after-seq-reset',
            display_order=100,
            is_active=True,
        )
        self.assertGreater(auto_cat.pk, 9999)


@unittest.skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL')
class PostgresOnCommitIntegrationTests(TransactionTestCase):
    def test_postgres_on_commit_executes_after_commit_and_skips_on_rollback(self):
        executed: list[str] = []

        with transaction.atomic():
            Category.objects.create(name='Коміт', slug='commit-cat', display_order=1)
            schedule_on_commit(executed.append, 'committed')
            self.assertEqual(executed, [])

        self.assertEqual(executed, ['committed'])

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                Category.objects.create(name='Ролбек', slug='rollback-cat', display_order=2)
                schedule_on_commit(executed.append, 'rolled_back')
                raise RuntimeError('Intentional rollback')

        self.assertEqual(executed, ['committed'])
        self.assertFalse(Category.objects.filter(slug='rollback-cat').exists())


@unittest.skipUnless(
    isinstance(caches[DEFAULT_CACHE_ALIAS], RedisCache),
    'Requires real Redis cache backend (TEST_WITH_REDIS=1)',
)
class RedisRateLimiterIntegrationTests(SimpleTestCase):
    def setUp(self):
        self.scope = 'e2e_redis_rl'
        self.identifier = f'client-{int(time.time() * 1000)}'
        reset_rate_limit(self.scope, self.identifier)

    def tearDown(self):
        reset_rate_limit(self.scope, self.identifier)

    def test_real_redis_lua_rate_limit_enforces_limit_ttl_and_reset(self):
        redis_backend = caches[DEFAULT_CACHE_ALIAS]
        key = build_rate_limit_key(self.scope, self.identifier)
        full_key = redis_backend.make_and_validate_key(key)
        redis_client = redis_backend._cache.get_client(full_key, write=True)

        limited1, retry1 = consume_rate_limit(self.scope, self.identifier, limit=2, period=15)
        self.assertFalse(limited1)
        self.assertEqual(retry1, 0)

        pttl_first = redis_client.pttl(full_key)
        self.assertGreater(pttl_first, 0)
        self.assertLessEqual(pttl_first, 15_000)

        time.sleep(0.15)
        limited2, retry2 = consume_rate_limit(self.scope, self.identifier, limit=2, period=15)
        self.assertFalse(limited2)
        self.assertEqual(retry2, 0)

        pttl_second = redis_client.pttl(full_key)
        self.assertGreater(pttl_second, 0)
        self.assertLessEqual(pttl_second, pttl_first)

        limited3, retry3 = consume_rate_limit(self.scope, self.identifier, limit=2, period=15)
        self.assertTrue(limited3)
        self.assertGreaterEqual(retry3, 1)
        self.assertLessEqual(retry3, 15)

        reset_rate_limit(self.scope, self.identifier)
        self.assertEqual(redis_client.exists(full_key), 0)

        limited_after, retry_after = consume_rate_limit(self.scope, self.identifier, limit=2, period=15)
        self.assertFalse(limited_after)
        self.assertEqual(retry_after, 0)

    def test_real_redis_lua_rate_limit_concurrent_threads_atomic(self):
        workers = 20
        limit = 5
        barrier = threading.Barrier(workers)
        concurrent_id = f'concurrent-{int(time.time() * 1000)}'
        self.addCleanup(reset_rate_limit, self.scope, concurrent_id)

        def worker(_idx: int) -> bool:
            barrier.wait()
            limited, _retry = consume_rate_limit(self.scope, concurrent_id, limit=limit, period=30)
            return limited

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(worker, range(workers)))

        self.assertEqual(sum(1 for blocked in results if not blocked), limit)
        self.assertEqual(sum(1 for blocked in results if blocked), workers - limit)


class FullStackConcurrencyIntegrationTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email='concurrency.e2e@doublebite.ua',
            password='StrongPassword123!',
            first_name='Тарас',
            last_name='Шевченко',
            phone='+380501112233',
        )
        self.category = Category.objects.create(
            name='Піца',
            slug='pizza-conc-e2e',
            display_order=1,
            is_active=True,
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Маргарита',
            slug='margherita-conc-e2e',
            description='Класична піца',
            price=Decimal('250.00'),
            weight_grams=400,
            calories=700,
            is_available=True,
        )

    @override_settings(
        STRIPE_WEBHOOK_SECRET='whsec_e2e_concurrency_secret',
        STRIPE_ORDER_SUCCESS_STATUS=OrderStatus.PREPARING,
    )
    @patch('orders.emails.send_stripe_receipt_email')
    def test_concurrent_stripe_webhooks_process_event_once(self, mock_receipt_email):
        order = Order.objects.create(
            user=self.user,
            customer_name='Тарас Шевченко',
            customer_email=self.user.email,
            customer_phone='+380501112233',
            delivery_address='Київ, вул. Володимирська, 42',
            payment_method=PaymentMethod.ONLINE,
            status=OrderStatus.PENDING,
            payment_status=PaymentStatus.PENDING,
            stripe_session_id='cs_e2e_conc_1',
            total_amount=Decimal('500.00'),
        )
        event_id = 'evt_e2e_conc_duplicate_1'
        payload = json.dumps(
            {
                'id': event_id,
                'type': 'checkout.session.completed',
                'data': {
                    'object': {
                        'id': 'cs_e2e_conc_1',
                        'payment_intent': 'pi_e2e_conc_1',
                        'payment_status': 'paid',
                        'amount_total': 50000,
                        'currency': 'uah',
                        'metadata': {'order_number': order.order_number},
                    }
                },
            }
        ).encode('utf-8')
        signature = _sign_stripe_payload(payload, 'whsec_e2e_concurrency_secret')
        workers = 8
        barrier = threading.Barrier(workers)

        def send_webhook(_idx: int) -> int:
            try:
                client = Client()
                barrier.wait()
                resp = client.post(
                    reverse('orders:stripe_webhook'),
                    data=payload,
                    content_type='application/json',
                    HTTP_STRIPE_SIGNATURE=signature,
                )
                return resp.status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            statuses = list(pool.map(send_webhook, range(workers)))

        self.assertEqual(statuses, [200] * workers)
        self.assertEqual(ProcessedStripeEvent.objects.filter(event_id=event_id).count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        self.assertEqual(order.status, OrderStatus.PREPARING)
        self.assertEqual(mock_receipt_email.call_count, 1)

    def test_concurrent_delivery_address_default_keeps_single_default(self):
        workers = 6
        barrier = threading.Barrier(workers)

        def save_default(idx: int) -> int:
            try:
                barrier.wait()
                addr = DeliveryAddress.objects.create(
                    user=self.user,
                    title=f'Адреса {idx}',
                    city='Київ',
                    street='вул. Хрещатик',
                    building=str(idx + 1),
                    is_default=True,
                )
                return addr.pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            pks = list(pool.map(save_default, range(workers)))

        self.assertEqual(len(pks), workers)
        self.assertEqual(DeliveryAddress.objects.filter(user=self.user).count(), workers)
        self.assertEqual(DeliveryAddress.objects.filter(user=self.user, is_default=True).count(), 1)

    def test_concurrent_cart_create_and_merge_no_duplicates(self):
        workers = 6
        barrier = threading.Barrier(workers)
        rf = RequestFactory()

        def create_user_cart(_idx: int) -> int:
            try:
                req = rf.get('/')
                req.user = self.user
                req.session = {}
                barrier.wait()
                cart = CartService.get_or_create_cart(req)
                return cart.pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            cart_ids = list(pool.map(create_user_cart, range(workers)))

        self.assertEqual(len(set(cart_ids)), 1)
        self.assertEqual(Cart.objects.filter(user=self.user).count(), 1)

        shared_guest_token = 'guest-merge-race-token-1'
        guest_cart = Cart.objects.create(guest_token=shared_guest_token)
        CartItem.objects.create(cart=guest_cart, dish=self.dish, quantity=3)
        merge_barrier = threading.Barrier(workers)

        def race_same_guest_merge(_idx: int) -> str | None:
            try:
                req = rf.get('/')
                req.user = self.user
                req.session = {
                    GUEST_TOKEN_SESSION_KEY: shared_guest_token,
                    'guest_cart_id': guest_cart.id,
                }
                merge_barrier.wait()
                CartService.merge_guest_cart(req, self.user)
                return req.session.get(PRE_LOGIN_GUEST_TOKEN_SESSION_KEY)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            preserved_tokens = list(pool.map(race_same_guest_merge, range(workers)))

        self.assertEqual(preserved_tokens, [shared_guest_token] * workers)
        self.assertFalse(Cart.objects.filter(pk=guest_cart.pk).exists())
        user_cart = Cart.objects.get(user=self.user)
        self.assertEqual(user_cart.items.filter(dish=self.dish).count(), 1)
        self.assertEqual(user_cart.items.get(dish=self.dish).quantity, 3)

        distinct_workers = 4
        distinct_tokens = [f'guest-merge-distinct-{i}' for i in range(distinct_workers)]
        distinct_carts = []
        for token in distinct_tokens:
            g_cart = Cart.objects.create(guest_token=token)
            CartItem.objects.create(cart=g_cart, dish=self.dish, quantity=2)
            distinct_carts.append((token, g_cart.id))

        distinct_barrier = threading.Barrier(distinct_workers)

        def race_distinct_guest_merge(idx: int) -> None:
            token, g_cart_id = distinct_carts[idx]
            try:
                req = rf.get('/')
                req.user = self.user
                req.session = {
                    GUEST_TOKEN_SESSION_KEY: token,
                    'guest_cart_id': g_cart_id,
                }
                distinct_barrier.wait()
                CartService.merge_guest_cart(req, self.user)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=distinct_workers) as pool:
            list(pool.map(race_distinct_guest_merge, range(distinct_workers)))

        self.assertEqual(Cart.objects.filter(guest_token__in=distinct_tokens).count(), 0)
        self.assertEqual(Cart.objects.filter(user=self.user).count(), 1)
        user_cart.refresh_from_db()
        self.assertEqual(user_cart.items.filter(dish=self.dish).count(), 1)
        self.assertEqual(user_cart.items.get(dish=self.dish).quantity, 3 + distinct_workers * 2)

    @patch('support.emails.send_ticket_escalated_email')
    def test_concurrent_support_session_and_escalation_single_ticket(self, mock_email):
        workers = 6
        barrier = threading.Barrier(workers)
        rf = RequestFactory()

        def get_session_and_escalate(idx: int) -> tuple[int, int]:
            try:
                req = rf.get('/')
                req.user = self.user
                req.session = {}
                barrier.wait()
                session = ChatSessionService.get_or_create_active_session(req)
                ticket = ChatSessionService.escalate_session(
                    session=session,
                    reason=TicketReason.HUMAN_REQUESTED,
                    details=f'Concurrent escalation {idx}',
                )
                return session.pk, ticket.pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(get_session_and_escalate, range(workers)))

        session_ids = {r[0] for r in results}
        ticket_ids = {r[1] for r in results}
        self.assertEqual(len(session_ids), 1)
        self.assertEqual(len(ticket_ids), 1)
        self.assertEqual(ChatSession.objects.filter(user=self.user).count(), 1)
        self.assertEqual(SupportTicket.objects.count(), 1)
        self.assertEqual(mock_email.call_count, 1)


class EndToEndGuestFlowAndAsgiWsgiTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.category = Category.objects.create(
            name='Піца',
            slug='pizza-e2e-flow',
            display_order=1,
            is_active=True,
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Маргарита E2E',
            slug='margherita-e2e-flow',
            description='Свіжа моцарела та томати',
            price=Decimal('260.00'),
            weight_grams=420,
            calories=720,
            is_available=True,
        )
        self.option = DishOption.objects.create(
            dish=self.dish,
            name='Подвійний сир',
            price_delta=Decimal('45.00'),
        )

    def test_full_guest_checkout_tracking_token_polling_and_cancel_flow(self):
        guest_client = Client()

        home_resp = guest_client.get(reverse('home'))
        self.assertEqual(home_resp.status_code, 200)

        htmx_catalog = guest_client.get(
            reverse('menu:catalog'),
            {'category': self.category.slug},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(htmx_catalog.status_code, 200)
        self.assertContains(htmx_catalog, 'Маргарита E2E')

        add_resp = guest_client.post(
            reverse('orders:cart_add', kwargs={'dish_id': self.dish.id}),
            {'quantity': '1', 'option_id': str(self.option.id)},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(add_resp.status_code, 200)

        checkout_resp = guest_client.post(
            reverse('orders:checkout'),
            {
                'customer_name': 'Гість Тестовий',
                'customer_email': 'guest.e2e@doublebite.ua',
                'customer_phone': '+380501234567',
                'delivery_address': 'Київ, вул. Хрещатик, 15',
                'payment_method': PaymentMethod.CASH,
                'notes': 'E2E перевірка',
            },
        )
        self.assertEqual(checkout_resp.status_code, 302)

        order = Order.objects.get(customer_email='guest.e2e@doublebite.ua')
        self.assertEqual(order.total_amount, Decimal('305.00'))

        fresh_guest = Client()
        token_url = f"{reverse('orders:tracking', kwargs={'order_number': order.order_number})}?t={order.access_token}"
        token_resp = fresh_guest.get(token_url)
        self.assertEqual(token_resp.status_code, 302)
        self.assertEqual(
            token_resp['Location'],
            reverse('orders:tracking', kwargs={'order_number': order.order_number}),
        )

        tracking_resp = fresh_guest.get(token_resp['Location'])
        self.assertEqual(tracking_resp.status_code, 200)

        poll_resp = fresh_guest.get(
            reverse('orders:tracking_status', kwargs={'order_number': order.order_number}),
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(poll_resp.status_code, 200)

        cancel_resp = fresh_guest.post(
            reverse('orders:order_cancel', kwargs={'order_number': order.order_number}),
        )
        self.assertEqual(cancel_resp.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)

    @patch(
        'support.agent.client.MistralSupportAgent.stream_chat_response',
        side_effect=_mock_e2e_stream_tokens,
    )
    async def test_asgi_sse_chat_stream_works_without_allow_async_unsafe(self, mock_stream):
        self.assertNotIn('DJANGO_ALLOW_ASYNC_UNSAFE', os.environ)
        async_client = AsyncClient()
        response = await async_client.post(
            reverse('support:chat_stream'),
            data=json.dumps({'message': 'Привіт ASGI'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/event-stream')
        chunks = [
            chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk
            async for chunk in response.streaming_content
        ]
        joined = ''.join(chunks)
        self.assertIn('Дякую за запит!', joined)
        self.assertIn('"done": true', joined)

    @patch(
        'support.agent.client.MistralSupportAgent.stream_chat_response',
        side_effect=_mock_e2e_stream_tokens,
    )
    def test_wsgi_sse_chat_stream_works_without_type_error(self, mock_stream):
        self.assertNotIn('DJANGO_ALLOW_ASYNC_UNSAFE', os.environ)
        wsgi_client = Client()
        response = wsgi_client.post(
            reverse('support:chat_stream'),
            data=json.dumps({'message': 'Привіт WSGI'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/event-stream')
        chunks = [
            chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk
            for chunk in response.streaming_content
        ]
        joined = ''.join(chunks)
        self.assertIn('Дякую за запит!', joined)
        self.assertIn('"done": true', joined)
        self.assertEqual(ChatMessage.objects.filter(role=MessageRole.ASSISTANT).count(), 1)

    @unittest.skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL')
    @patch(
        'support.agent.client.MistralSupportAgent.stream_chat_response',
        side_effect=_mock_e2e_stream_tokens,
    )
    def test_wsgi_abrupt_disconnect_does_not_leak_postgres_connections(self, mock_stream):
        def count_other_pg_sessions() -> int:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM pg_stat_activity
                    WHERE datname = current_database() AND pid <> pg_backend_pid()
                    """
                )
                return int(cursor.fetchone()[0])

        orig_max_age = connections['default'].settings_dict.get('CONN_MAX_AGE', 0)
        connections['default'].settings_dict['CONN_MAX_AGE'] = 60
        wsgi_client = Client()
        try:
            resp_first = wsgi_client.post(
                reverse('support:chat_stream'),
                data=json.dumps({'message': 'Обрив 0'}),
                content_type='application/json',
            )
            self.assertEqual(resp_first.status_code, 200)
            first_iter = iter(resp_first.streaming_content)
            next(first_iter)
            resp_first.close()
            baseline_sessions = count_other_pg_sessions()

            for idx in range(1, 5):
                resp = wsgi_client.post(
                    reverse('support:chat_stream'),
                    data=json.dumps({'message': f'Обрив {idx}'}),
                    content_type='application/json',
                )
                self.assertEqual(resp.status_code, 200)
                stream_iter = iter(resp.streaming_content)
                next(stream_iter)
                resp.close()

            after_sessions = count_other_pg_sessions()
            self.assertLessEqual(after_sessions, baseline_sessions)
            self.assertEqual(ChatMessage.objects.filter(role=MessageRole.ASSISTANT).count(), 0)
        finally:
            connections['default'].settings_dict['CONN_MAX_AGE'] = orig_max_age
            asyncio.run(sync_to_async(connections.close_all, thread_sensitive=True)())
            connections.close_all()
