import os
import pickle
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.utils import timezone
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from menu.models import (
    VALID_ALLERGEN_CODES,
    AllergenCode,
    Category,
    Dish,
    DishOption,
    Promotion,
    PromoType,
    UpsellGroup,
)
from orders.models import Order
from orders.services import CartService
from redis.exceptions import ConnectionError as RedisConnectionError
from reportlab.pdfbase.ttfonts import TTFont

from support.agent.client import GeminiSupportAgent
from support.cache import (
    STATIC_AI_PREFIX_CACHE_KEY,
    build_static_ai_prefix,
    get_static_ai_prefix,
    invalidate_static_ai_prefix,
)
from support.models import (
    AITurnAuditLog,
    ChatAttachment,
    ChatMessage,
    ChatSession,
    CustomerMemory,
    FAQKnowledge,
    MessageRole,
    StaffAuditDomain,
    StaffAuditLog,
)

User = get_user_model()


class _MockDelta:
    def __init__(self, content: str | None = None):
        self.content = content
        self.tool_calls = None


class _MockChoice:
    def __init__(self, content: str | None = None):
        self.delta = _MockDelta(content=content)


class _MockChunk:
    def __init__(self, content: str | None = None, usage: object | None = None):
        self.data = MagicMock()
        self.data.choices = [_MockChoice(content=content)]
        self.data.usage = usage


class _MockAsyncStream:
    def __init__(self, chunks: list[_MockChunk]):
        self._chunks = chunks
        self.closed = False

    def __aiter__(self):
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        self.closed = True


class _FailingCacheBackend:
    def __init__(self, exc: Exception):
        self._exc = exc

    def get(self, key: str, default: object = None) -> object:
        raise self._exc

    def set(self, key: str, value: object, timeout: int | None = None) -> None:
        raise self._exc

    def delete(self, key: str) -> None:
        raise self._exc


class StaticPrefixCacheContractTest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.factory = RequestFactory()
        self.user_a = User.objects.create_user(
            email='client_a@example.com',
            password='password123',
            phone='+380501111111',
        )
        self.user_b = User.objects.create_user(
            email='client_b@example.com',
            password='password123',
            phone='+380502222222',
        )
        self.pizza_cat = Category.objects.create(
            name='Піца',
            slug='pizza-prefix-test',
            display_order=1,
            is_active=True,
        )
        self.drink_cat = Category.objects.create(
            name='Напої',
            slug='drinks-prefix-test',
            display_order=2,
            is_active=True,
        )
        self.inactive_cat = Category.objects.create(
            name='Архів',
            slug='archive-prefix-test',
            display_order=3,
            is_active=False,
        )
        self.margherita = Dish.objects.create(
            category=self.pizza_cat,
            title='Маргарита Неаполь',
            slug='margherita-neapol',
            ingredients='Тісто, томати San Marzano, моцарела, базилік',
            price=Decimal('245.00'),
            weight_grams=430,
            calories=780,
            proteins_grams=29,
            fats_grams=29,
            carbs_grams=101,
            allergen_codes=[AllergenCode.GLUTEN, AllergenCode.LACTOSE],
            is_vegetarian=True,
            is_vegan=False,
            is_spicy=False,
            contains_raw_fish=False,
            safe_for_pregnancy=True,
            upsell_group=UpsellGroup.MAIN,
            is_available=True,
        )
        self.margherita_opt = DishOption.objects.create(
            dish=self.margherita,
            name='Подвійна моцарела',
            price_delta=Decimal('45.00'),
        )
        self.lemonade = Dish.objects.create(
            category=self.drink_cat,
            title='Лимонад Юдзу',
            slug='lemonade-yuzu-test',
            ingredients='Юдзу, імбир, газована вода',
            price=Decimal('120.00'),
            weight_grams=400,
            calories=95,
            proteins_grams=1,
            fats_grams=0,
            carbs_grams=22,
            allergen_codes=[],
            is_vegetarian=True,
            is_vegan=True,
            upsell_group=UpsellGroup.DRINK,
            is_available=True,
        )
        self.unavailable_dish = Dish.objects.create(
            category=self.pizza_cat,
            title='Стоп-лист Піца',
            slug='stoplist-pizza',
            price=Decimal('399.00'),
            weight_grams=450,
            calories=850,
            is_available=False,
        )
        self.inactive_cat_dish = Dish.objects.create(
            category=self.inactive_cat,
            title='Архівна Страва',
            slug='archived-dish-test',
            price=Decimal('199.00'),
            weight_grams=300,
            calories=500,
            is_available=True,
        )
        self.active_promo = Promotion.objects.create(
            title='Знижка 10% від 800 грн',
            slug='promo-10-800-test',
            description='Діє на все меню від 800 грн.',
            promo_type=PromoType.PERCENT,
            discount_value=Decimal('10.00'),
            min_order_amount=Decimal('800.00'),
            is_active=True,
        )
        self.inactive_promo = Promotion.objects.create(
            title='Вимкнена акція',
            slug='promo-inactive-test',
            description='Не повинна потрапляти у префікс.',
            promo_type=PromoType.FIXED_AMOUNT,
            discount_value=Decimal('50.00'),
            min_order_amount=Decimal('300.00'),
            is_active=False,
        )
        self.expired_promo = Promotion.objects.create(
            title='Прострочена акція',
            slug='promo-expired-test',
            description='Вже завершилась.',
            promo_type=PromoType.FIXED_AMOUNT,
            discount_value=Decimal('75.00'),
            min_order_amount=Decimal('300.00'),
            is_active=True,
            ends_at=timezone.now() - timedelta(hours=1),
        )
        self.faq = FAQKnowledge.objects.create(
            question='Який час доставки?',
            answer='Орієнтовний час доставки становить 30 хвилин.',
            category='Доставка',
            is_active=True,
        )
        self.inactive_faq = FAQKnowledge.objects.create(
            question='Приховане питання',
            answer='Не активне правило.',
            category='Архів',
            is_active=False,
        )
        invalidate_static_ai_prefix()

    def _make_request(self, user: object | None = None):
        request = self.factory.post('/support/chat/stream/')
        store = SessionStore()
        store.create()
        request.session = store
        if user is not None:
            request.user = user
        else:
            from django.contrib.auth.models import AnonymousUser

            request.user = AnonymousUser()
        return request

    def test_static_prefix_excludes_unavailable_and_caches_in_backend(self) -> None:
        prefix = get_static_ai_prefix()
        self.assertIn('Маргарита Неаполь', prefix)
        self.assertIn('245.00', prefix)
        self.assertIn('29/29/101', prefix)
        self.assertIn('gluten,lactose', prefix)
        self.assertIn('Подвійна моцарела:+45.00', prefix)
        self.assertIn('Лимонад Юдзу', prefix)
        self.assertIn('Знижка 10% від 800 грн', prefix)
        self.assertIn('Який час доставки?', prefix)

        self.assertNotIn('Стоп-лист Піца', prefix)
        self.assertNotIn('Архівна Страва', prefix)
        self.assertNotIn('Вимкнена акція', prefix)
        self.assertNotIn('Прострочена акція', prefix)
        self.assertNotIn('Приховане питання', prefix)

        self.assertEqual(cache.get(STATIC_AI_PREFIX_CACHE_KEY), prefix)
        with self.assertNumQueries(0):
            second_prefix = get_static_ai_prefix()
        self.assertEqual(prefix.encode('utf-8'), second_prefix.encode('utf-8'))

    @patch('support.agent.client.GeminiClient')
    async def test_messages_zero_byte_identical_across_clients_and_carts(
        self,
        mock_gemini_cls: MagicMock,
    ) -> None:
        from asgiref.sync import sync_to_async

        captured_messages: list[list[dict]] = []

        async def _capture_stream(**kwargs):
            captured_messages.append([dict(m) for m in kwargs['messages']])
            return _MockAsyncStream([
                _MockChunk(
                    'Вітаю!',
                    usage=SimpleNamespace(
                        prompt_tokens=500,
                        completion_tokens=10,
                        total_tokens=510,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=420),
                    ),
                )
            ])

        mock_client = AsyncMock()
        mock_client.chat.stream_async.side_effect = _capture_stream
        mock_gemini_cls.return_value.__aenter__.return_value = mock_client

        def _prepare_requests():
            req_a = self._make_request(self.user_a)
            CartService.add_dish(req_a, self.margherita.id, quantity=2)
            req_b = self._make_request(self.user_b)
            req_guest = self._make_request(None)
            CartService.add_dish(req_guest, self.lemonade.id, quantity=3)
            return req_a, req_b, req_guest

        req_a, req_b, req_guest = await sync_to_async(_prepare_requests, thread_sensitive=True)()

        agent = GeminiSupportAgent(api_key='fake-key')
        history = [
            {'role': 'assistant', 'content': 'Попереднє обрізане повідомлення'},
            {'role': 'user', 'content': ''},
            {'role': 'user', 'content': 'Що в мене в кошику?'},
        ]

        for req in (req_a, req_b, req_guest):
            events = [ev async for ev in agent.stream_chat_response(history, request=req)]
            self.assertEqual(events, [{'token': 'Вітаю!'}])

        self.assertEqual(len(captured_messages), 3)
        sys_a = captured_messages[0][0]
        sys_b = captured_messages[1][0]
        sys_guest = captured_messages[2][0]

        self.assertEqual(sys_a['role'], 'system')
        self.assertEqual(sys_a['content'].encode('utf-8'), sys_b['content'].encode('utf-8'))
        self.assertEqual(sys_a['content'].encode('utf-8'), sys_guest['content'].encode('utf-8'))

        for sys_msg in (sys_a, sys_b, sys_guest):
            self.assertNotIn('[КОНТЕКСТ ПОТОЧНОГО КЛІЄНТА]', sys_msg['content'])
            self.assertNotIn('ПОТОЧНИЙ СТАН КОШИКА КЛІЄНТА', sys_msg['content'])

        for msg_list in captured_messages:
            self.assertEqual(msg_list[1]['role'], 'user')
            self.assertIn('[КОНТЕКСТ ПОТОЧНОГО КЛІЄНТА]', msg_list[-1]['content'])
            self.assertIn('Що в мене в кошику?', msg_list[-1]['content'])

        self.assertIn('Маргарита Неаполь (2 шт.', captured_messages[0][-1]['content'])
        self.assertIn('кошик наразі порожній', captured_messages[1][-1]['content'])
        self.assertIn('Лимонад Юдзу (3 шт.', captured_messages[2][-1]['content'])

    def test_update_round_usage_extracts_cached_tokens(self) -> None:
        usage_dict = {
            'prompt_tokens': 0,
            'cached_tokens': 0,
            'completion_tokens': 0,
            'total_tokens': 0,
        }
        GeminiSupportAgent._update_round_usage(
            {
                'usage': {
                    'prompt_tokens': 800,
                    'completion_tokens': 40,
                    'total_tokens': 840,
                    'prompt_tokens_details': {'cached_tokens': 650},
                }
            },
            usage_dict,
        )
        self.assertEqual(usage_dict['prompt_tokens'], 800)
        self.assertEqual(usage_dict['cached_tokens'], 650)
        self.assertEqual(usage_dict['completion_tokens'], 40)
        self.assertEqual(usage_dict['total_tokens'], 840)

        usage_obj = {
            'prompt_tokens': 0,
            'cached_tokens': 0,
            'completion_tokens': 0,
            'total_tokens': 0,
        }
        GeminiSupportAgent._update_round_usage(
            SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=900,
                    completion_tokens=30,
                    total_tokens=930,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=700),
                )
            ),
            usage_obj,
        )
        self.assertEqual(usage_obj['cached_tokens'], 700)

    def test_invalidation_on_dish_option_category_promotion_and_faq_mutations(self) -> None:
        initial = get_static_ai_prefix()
        self.assertIn('245.00', initial)

        with self.captureOnCommitCallbacks(execute=True):
            self.margherita.price = Decimal('279.00')
            self.margherita.save(update_fields=['price'])
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        after_price = get_static_ai_prefix()
        self.assertIn('279.00', after_price)
        self.assertNotIn('245.00', after_price)

        with self.captureOnCommitCallbacks(execute=True):
            self.margherita.is_available = False
            self.margherita.save(update_fields=['is_available'])
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        self.assertNotIn('Маргарита Неаполь', get_static_ai_prefix())

        with self.captureOnCommitCallbacks(execute=True):
            self.margherita.is_available = True
            self.margherita.save(update_fields=['is_available'])
        self.assertIn('Маргарита Неаполь', get_static_ai_prefix())

        with self.captureOnCommitCallbacks(execute=True):
            self.margherita_opt.delete()
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        self.assertNotIn('Подвійна моцарела', get_static_ai_prefix())

        with self.captureOnCommitCallbacks(execute=True):
            self.active_promo.is_active = False
            self.active_promo.save(update_fields=['is_active'])
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        self.assertNotIn('Знижка 10% від 800 грн', get_static_ai_prefix())

        with self.captureOnCommitCallbacks(execute=True):
            self.active_promo.is_active = True
            self.active_promo.save(update_fields=['is_active'])
            self.active_promo.applicable_categories.add(self.pizza_cat)
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        self.assertIn('cats:pizza-prefix-test', get_static_ai_prefix())

        with self.captureOnCommitCallbacks(execute=True):
            self.drink_cat.is_active = False
            self.drink_cat.save(update_fields=['is_active'])
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        self.assertNotIn('Лимонад Юдзу', get_static_ai_prefix())

        with self.captureOnCommitCallbacks(execute=True):
            self.faq.delete()
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        self.assertNotIn('Який час доставки?', get_static_ai_prefix())

    def test_invalidation_and_rebuild_are_idempotent(self) -> None:
        self.assertTrue(invalidate_static_ai_prefix())
        self.assertTrue(invalidate_static_ai_prefix())
        self.assertTrue(invalidate_static_ai_prefix())

        first = get_static_ai_prefix(force_refresh=True)
        second = get_static_ai_prefix(force_refresh=True)
        third = get_static_ai_prefix()
        self.assertEqual(first.encode('utf-8'), second.encode('utf-8'))
        self.assertEqual(second.encode('utf-8'), third.encode('utf-8'))

        self.assertTrue(invalidate_static_ai_prefix())
        self.assertTrue(invalidate_static_ai_prefix())
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        rebuilt = get_static_ai_prefix()
        self.assertEqual(first.encode('utf-8'), rebuilt.encode('utf-8'))

    def test_redis_failure_falls_back_to_db_without_error(self) -> None:
        failing = _FailingCacheBackend(RedisConnectionError('Redis unreachable'))
        expected = build_static_ai_prefix()
        actual = get_static_ai_prefix(cache_backend=failing)
        self.assertEqual(actual.encode('utf-8'), expected.encode('utf-8'))
        self.assertFalse(invalidate_static_ai_prefix(cache_backend=failing))

        timeout_backend = _FailingCacheBackend(TimeoutError('Redis timeout'))
        actual_timeout = get_static_ai_prefix(cache_backend=timeout_backend)
        self.assertEqual(actual_timeout.encode('utf-8'), expected.encode('utf-8'))

        corrupt_backend = _FailingCacheBackend(pickle.UnpicklingError('corrupt payload'))
        actual_corrupt = get_static_ai_prefix(cache_backend=corrupt_backend)
        self.assertEqual(actual_corrupt.encode('utf-8'), expected.encode('utf-8'))

        eof_backend = _FailingCacheBackend(EOFError('truncated cache stream'))
        actual_eof = get_static_ai_prefix(cache_backend=eof_backend)
        self.assertEqual(actual_eof.encode('utf-8'), expected.encode('utf-8'))

    def test_promotion_scoped_to_unavailable_targets_is_excluded_and_m2m_invalidates(self) -> None:
        stoplist_only = Promotion.objects.create(
            title='Акція лише на стоп-лист',
            slug='promo-stoplist-only',
            promo_type=PromoType.PERCENT,
            discount_value=Decimal('20.00'),
            is_active=True,
        )
        stoplist_only.applicable_dishes.add(self.unavailable_dish)
        prefix = build_static_ai_prefix()
        self.assertNotIn('Акція лише на стоп-лист', prefix)
        self.assertNotIn(f'dishes:{self.unavailable_dish.id}', prefix)

        mixed = Promotion.objects.create(
            title='Змішана акція',
            slug='promo-mixed-targets',
            promo_type=PromoType.FIXED_AMOUNT,
            discount_value=Decimal('30.00'),
            is_active=True,
        )
        mixed.applicable_dishes.add(self.margherita, self.unavailable_dish)
        mixed_prefix = build_static_ai_prefix()
        self.assertIn('Змішана акція', mixed_prefix)
        self.assertIn(f'dishes:{self.margherita.id}', mixed_prefix)
        self.assertNotIn(f'dishes:{self.unavailable_dish.id}', mixed_prefix)

        get_static_ai_prefix(force_refresh=True)
        with self.captureOnCommitCallbacks(execute=True):
            mixed.applicable_dishes.remove(self.margherita)
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        self.assertNotIn('Змішана акція', get_static_ai_prefix())

        get_static_ai_prefix(force_refresh=True)
        with self.captureOnCommitCallbacks(execute=True):
            mixed.applicable_dishes.clear()
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        self.assertIn('Змішана акція', get_static_ai_prefix())

    def test_prefix_cache_clamps_ttl_to_promotion_time_boundaries(self) -> None:
        now = timezone.now()
        expiring_promo = Promotion.objects.create(
            title='Акція що завершується',
            slug='promo-expiring-boundary',
            promo_type=PromoType.PERCENT,
            discount_value=Decimal('5.00'),
            is_active=True,
            ends_at=now + timedelta(seconds=15),
        )
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        prefix = get_static_ai_prefix(cache_backend=mock_cache)
        self.assertIn('Акція що завершується', prefix)
        stored_ttl = mock_cache.set.call_args.kwargs['timeout']
        self.assertGreaterEqual(stored_ttl, 1)
        self.assertLessEqual(stored_ttl, 16)

        expiring_promo.delete()
        Promotion.objects.create(
            title='Майбутня акція',
            slug='promo-starting-boundary',
            promo_type=PromoType.PERCENT,
            discount_value=Decimal('12.00'),
            is_active=True,
            starts_at=now + timedelta(seconds=22),
        )
        mock_cache.reset_mock()
        mock_cache.get.return_value = None
        prefix_future = get_static_ai_prefix(cache_backend=mock_cache)
        self.assertNotIn('Майбутня акція', prefix_future)
        future_ttl = mock_cache.set.call_args.kwargs['timeout']
        self.assertGreaterEqual(future_ttl, 1)
        self.assertLessEqual(future_ttl, 23)

    def test_agent_init_handles_whitespace_env_vars_and_cart_excludes_stoplist(self) -> None:
        with patch.dict(
            os.environ,
            {
                'GEMINI_API_KEY': '   ',
                'GEMINI_MODEL': '   ',
                'GEMINI_TEMPERATURE': '   ',
                'GEMINI_MAX_TOKENS': '   ',
                'GEMINI_REASONING_EFFORT': '   ',
            },
        ):
            with override_settings(GEMINI_API_KEY=''):
                agent = GeminiSupportAgent()
        self.assertEqual(agent.api_key, '')
        self.assertEqual(agent.model, settings.GEMINI_MODEL)
        self.assertAlmostEqual(agent.temperature, float(settings.GEMINI_TEMPERATURE))
        self.assertEqual(agent.max_tokens, settings.GEMINI_MAX_TOKENS)

        req = self._make_request(self.user_a)
        CartService.add_dish(req, self.margherita.id, quantity=2)
        CartService.add_dish(req, self.lemonade.id, quantity=1)
        self.margherita.is_available = False
        self.margherita.save(update_fields=['is_available'])

        ctx = GeminiSupportAgent._get_cart_context(req)
        self.assertNotIn('Маргарита Неаполь', ctx)
        self.assertIn('Лимонад Юдзу (1 шт., 120.00 грн)', ctx)
        self.assertIn('Загальна сума: 120.00 грн', ctx)

        self.lemonade.is_available = False
        self.lemonade.save(update_fields=['is_available'])
        empty_ctx = GeminiSupportAgent._get_cart_context(req)
        self.assertEqual(empty_ctx, 'ПОТОЧНИЙ СТАН КОШИКА КЛІЄНТА: кошик наразі порожній.')

    @patch('support.agent.client.GeminiClient')
    async def test_agent_succeeds_when_redis_cache_fails(
        self,
        mock_gemini_cls: MagicMock,
    ) -> None:
        mock_client = AsyncMock()
        mock_client.chat.stream_async.return_value = _MockAsyncStream([_MockChunk('Працює без Redis!')])
        mock_gemini_cls.return_value.__aenter__.return_value = mock_client

        failing_caches = {'default': _FailingCacheBackend(RedisConnectionError('Redis down'))}
        agent = GeminiSupportAgent(api_key='fake-key')
        with patch('support.cache.caches', failing_caches):
            events = [
                ev
                async for ev in agent.stream_chat_response(
                    [{'role': 'user', 'content': 'Привіт'}]
                )
            ]
        self.assertEqual(events, [{'token': 'Працює без Redis!'}])


class StaticPrefixRealCommitInvalidationTest(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.category = Category.objects.create(
            name='Суші',
            slug='sushi-tx-test',
            display_order=1,
            is_active=True,
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Рол Філадельфія Тест',
            slug='roll-phila-tx-test',
            price=Decimal('340.00'),
            weight_grams=290,
            calories=520,
            is_available=True,
        )
        self.promo = Promotion.objects.create(
            title='Вечірня акція 15%',
            slug='evening-15-tx-test',
            promo_type=PromoType.PERCENT,
            discount_value=Decimal('15.00'),
            min_order_amount=Decimal('500.00'),
            is_active=True,
        )

    def test_real_transaction_commit_invalidates_cached_prefix_immediately(self) -> None:
        first = get_static_ai_prefix()
        self.assertIn('340.00', first)
        self.assertIn('Вечірня акція 15%', first)
        self.assertIsNotNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))

        self.dish.price = Decimal('365.00')
        self.dish.save(update_fields=['price'])
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        self.assertIn('365.00', get_static_ai_prefix())

        self.dish.is_available = False
        self.dish.save(update_fields=['is_available'])
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        self.assertNotIn('Рол Філадельфія Тест', get_static_ai_prefix())

        self.promo.is_active = False
        self.promo.save(update_fields=['is_active'])
        self.assertIsNone(cache.get(STATIC_AI_PREFIX_CACHE_KEY))
        self.assertNotIn('Вечірня акція 15%', get_static_ai_prefix())

    def test_seed_db_computes_embeddings_outside_atomic_transaction(self) -> None:
        in_atomic_flags: list[bool] = []

        def _spy_get_embedding(text: str):
            in_atomic_flags.append(connection.in_atomic_block)
            return None

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            override_settings(MEDIA_ROOT=tmpdir),
            patch('menu.management.commands.seed_db.get_embedding', side_effect=_spy_get_embedding),
        ):
            (Path(tmpdir) / 'dishes').mkdir(parents=True, exist_ok=True)
            call_command('seed_db', force=True, skip_images=True)

        self.assertGreater(len(in_atomic_flags), 100)
        self.assertFalse(any(in_atomic_flags))

    def test_slice_zero_migrations_reverse_and_reapply_cleanly(self) -> None:
        executor = MigrationExecutor(connection)
        leaf_targets = executor.loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate(
                [('support', '0004_chatmessage_telegram_message_id_and_more')]
            )
            MigrationExecutor(connection).migrate(
                [('orders', '0005_guest_token_payment_enums_and_constraints')]
            )
            MigrationExecutor(connection).migrate(
                [('menu', '0005_ascii_slugs_and_indexes')]
            )
        finally:
            MigrationExecutor(connection).migrate(leaf_targets)
        self.assertIn(('menu', '0006_dish_nutrition_allergens_and_promotions'), leaf_targets)
        self.assertIn(('orders', '0006_order_alphanumeric_receipt_and_chat_promo'), leaf_targets)
        self.assertIn(('support', '0005_gemini_concierge_pro'), leaf_targets)


class GeminiMessageNormalizationPropertyTest(SimpleTestCase):
    @hypothesis_settings(max_examples=75, deadline=None)
    @given(
        static_prefix=st.text(min_size=1, max_size=80).filter(lambda s: bool(s.strip())),
        history=st.lists(
            st.fixed_dictionaries({
                'role': st.sampled_from(['user', 'assistant', 'operator', 'tool', 'system']),
                'content': st.text(max_size=40),
            }),
            max_size=12,
        ),
        dynamic_context=st.text(max_size=60),
    )
    def test_normalize_gemini_messages_preserves_invariants(
        self,
        static_prefix: str,
        history: list[dict[str, str]],
        dynamic_context: str,
    ) -> None:
        result = GeminiSupportAgent._normalize_gemini_messages(
            static_prefix,
            history,
            dynamic_context=dynamic_context,
        )
        self.assertEqual(result[0], {'role': 'system', 'content': static_prefix})
        if len(result) > 1:
            self.assertEqual(result[1]['role'], 'user')
        for idx in range(1, len(result)):
            self.assertIn(result[idx]['role'], ('user', 'assistant'))
            self.assertTrue(str(result[idx]['content']).strip())
            if idx >= 2:
                self.assertNotEqual(result[idx]['role'], result[idx - 1]['role'])
        cleaned_ctx = dynamic_context.strip()
        if cleaned_ctx:
            user_messages = [m for m in result[1:] if m['role'] == 'user']
            self.assertTrue(user_messages)
            self.assertIn(cleaned_ctx, user_messages[-1]['content'])
            if cleaned_ctx not in static_prefix:
                self.assertNotIn(cleaned_ctx, result[0]['content'])


class SliceZeroSchemaSeedAndFontsTest(TestCase):
    def test_dish_allergen_codes_and_promotion_validation(self) -> None:
        cat = Category.objects.create(name='Тест', slug='validation-cat')
        dish = Dish.objects.create(
            category=cat,
            title='Тестова страва',
            slug='validation-dish',
            price=Decimal('150.00'),
            weight_grams=250,
            allergen_codes=['SESAME', 'gluten', 'sesame'],
        )
        self.assertEqual(dish.allergen_codes, ['gluten', 'sesame'])

        invalid_dish = Dish(
            category=cat,
            title='Невалідний алерген',
            slug='invalid-allergen-dish',
            price=Decimal('150.00'),
            weight_grams=250,
            allergen_codes=['unknown_allergen'],
        )
        with self.assertRaises(ValidationError):
            invalid_dish.save()

        invalid_percent = Promotion(
            title='Некоректний відсоток',
            slug='invalid-percent-promo',
            promo_type=PromoType.PERCENT,
            discount_value=Decimal('150.00'),
            min_order_amount=Decimal('0.00'),
        )
        with self.assertRaises(ValidationError):
            invalid_percent.save()

        now = timezone.now()
        invalid_dates = Promotion(
            title='Некоректні дати',
            slug='invalid-dates-promo',
            promo_type=PromoType.FIXED_AMOUNT,
            discount_value=Decimal('50.00'),
            starts_at=now,
            ends_at=now - timedelta(minutes=5),
        )
        with self.assertRaises(ValidationError):
            invalid_dates.save()

        with self.assertRaises(IntegrityError), transaction.atomic():
            Promotion.objects.bulk_create([
                Promotion(
                    title='Відʼємна знижка БД',
                    slug='db-neg-discount',
                    promo_type=PromoType.FIXED_AMOUNT,
                    discount_value=Decimal('-1.00'),
                    min_order_amount=Decimal('0.00'),
                )
            ])

        with self.assertRaises(IntegrityError), transaction.atomic():
            Promotion.objects.bulk_create([
                Promotion(
                    title='Відʼємна мінсума БД',
                    slug='db-neg-min-order',
                    promo_type=PromoType.FIXED_AMOUNT,
                    discount_value=Decimal('10.00'),
                    min_order_amount=Decimal('-5.00'),
                )
            ])

    def test_new_models_and_partial_unique_constraints(self) -> None:
        user = User.objects.create_user(
            email='schema_test@example.com',
            password='password123',
        )
        session = ChatSession.objects.create(user=user)
        user_msg = ChatMessage.objects.create(
            session=session,
            role=MessageRole.USER,
            content='Хочу замовити піцу',
        )
        assistant_msg = ChatMessage.objects.create(
            session=session,
            role=MessageRole.ASSISTANT,
            content='Готово!',
            structured_payload={'kind': 'payment_card', 'order_number': 'DB-0001'},
        )
        attachment = ChatAttachment.objects.create(
            session=session,
            message=user_msg,
            uploader_user=user,
            file='support_attachments/2026/11/test.webp',
            width=400,
            height=300,
            size_bytes=12345,
            sha256_hash='a' * 64,
        )
        self.assertIsNotNone(attachment.pk)

        audit = AITurnAuditLog.objects.create(
            session=session,
            user_message=user_msg,
            assistant_message=assistant_msg,
            model_used='gemini-2.5-flash',
            prompt_tokens=600,
            cached_tokens=450,
            completion_tokens=80,
            latency_ms=420,
        )
        audit.full_clean()

        staff_log = StaffAuditLog.objects.create(
            actor=user,
            actor_role='RESTAURANT_ADMIN',
            domain=StaffAuditDomain.MENU,
            action='PRICE_CHANGED',
            target_model='menu.Dish',
            target_id='1',
            before_state={'price': '245.00'},
            after_state={'price': '260.00'},
        )
        staff_log.full_clean()

        Order.objects.create(
            customer_name='Тест 1',
            customer_phone='+380501111111',
            delivery_address='Київ',
            chat_idempotency_key='',
            receipt_number='',
        )
        Order.objects.create(
            customer_name='Тест 2',
            customer_phone='+380502222222',
            delivery_address='Київ',
            chat_idempotency_key='',
            receipt_number='',
        )
        Order.objects.create(
            customer_name='Тест 3',
            customer_phone='+380503333333',
            delivery_address='Київ',
            chat_idempotency_key='idem-key-1',
            receipt_number='CHK-20261127-000001',
            source_chat_session=session,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.create(
                customer_name='Тест Дубль Ключа',
                customer_phone='+380504444444',
                delivery_address='Київ',
                chat_idempotency_key='idem-key-1',
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.create(
                customer_name='Тест Дубль Чека',
                customer_phone='+380505555555',
                delivery_address='Київ',
                receipt_number='CHK-20261127-000001',
            )

        CustomerMemory.objects.create(user=user, guest_token='')
        CustomerMemory.objects.create(guest_token='guest-mem-1')
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomerMemory.objects.create(guest_token='guest-mem-1')

    def test_seed_db_populates_nutrition_allergens_and_ukrainian_promotions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / 'dishes').mkdir(parents=True, exist_ok=True)
            with override_settings(MEDIA_ROOT=tmpdir):
                call_command('seed_db', force=True, skip_images=True)

        dishes = list(Dish.objects.select_related('category').all())
        self.assertGreaterEqual(len(dishes), 105)
        for dish in dishes:
            self.assertTrue(dish.ingredients.strip(), f'Empty ingredients for {dish.slug}')
            self.assertGreater(
                dish.proteins_grams + dish.fats_grams + dish.carbs_grams,
                0,
                f'Zero macros for {dish.slug}',
            )
            self.assertIsInstance(dish.allergen_codes, list)
            for code in dish.allergen_codes:
                self.assertIn(code, VALID_ALLERGEN_CODES)
            self.assertIn(dish.upsell_group, UpsellGroup.values)
            if dish.contains_raw_fish:
                self.assertFalse(dish.safe_for_pregnancy)

        raw_fish_count = Dish.objects.filter(contains_raw_fish=True).count()
        pregnancy_safe_rolls = Dish.objects.filter(
            category__slug='sushi',
            safe_for_pregnancy=True,
            contains_raw_fish=False,
        ).count()
        self.assertGreater(raw_fish_count, 0)
        self.assertGreater(pregnancy_safe_rolls, 0)
        self.assertGreater(Dish.objects.filter(upsell_group=UpsellGroup.DRINK).count(), 0)
        self.assertGreater(Dish.objects.filter(upsell_group=UpsellGroup.DESSERT).count(), 0)
        self.assertGreater(Dish.objects.filter(upsell_group=UpsellGroup.SALAD_STARTER).count(), 0)

        active_promotions = list(Promotion.objects.filter(is_active=True))
        self.assertGreaterEqual(len(active_promotions), 3)
        for promo in active_promotions:
            self.assertTrue(promo.title.strip())
            self.assertTrue(promo.description.strip())

        orange_juice = Dish.objects.get(slug='fresh-orange-juice')
        self.assertNotIn('основа: очищена фільтрована вода', orange_juice.ingredients.lower())
        vegan_burger = Dish.objects.get(slug='burger-beyond-meat-vegan')
        self.assertTrue(vegan_burger.is_vegan)
        self.assertNotIn('основа: булочка бріош', vegan_burger.ingredients.lower())
        tempura_roll = Dish.objects.get(slug='roll-tempura-ebi-shrimp')
        self.assertIn(AllergenCode.SOY, tempura_roll.allergen_codes)
        for raw_slug in (
            'roll-california-crab-tobiko',
            'roll-spicy-salmon-aburi',
            'roll-philadelphia-grill-nori',
        ):
            raw_roll = Dish.objects.get(slug=raw_slug)
            self.assertTrue(raw_roll.contains_raw_fish, raw_slug)
            self.assertFalse(raw_roll.safe_for_pregnancy, raw_slug)
        for unsafe_set_slug in (
            'set-italian-trio-pizza',
            'set-family-pizza-wings-box',
            'set-chef-tasting-experience',
        ):
            self.assertFalse(Dish.objects.get(slug=unsafe_set_slug).safe_for_pregnancy)

    def test_local_cyrillic_pdf_fonts_load_in_reportlab(self) -> None:
        fonts_dir = Path(settings.BASE_DIR) / 'static' / 'fonts' / 'pdf'
        ukrainian_sample = 'АБВГҐДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯабвгґдеєжзиіїйклмнопрстуфхцчшщьюя№«»'
        for filename in ('JetBrainsMono-Regular.ttf', 'JetBrainsMono-Bold.ttf'):
            font_path = fonts_dir / filename
            self.assertTrue(font_path.is_file(), f'Missing local PDF font: {font_path}')
            font = TTFont(filename, str(font_path))
            cmap = font.face.charToGlyph
            missing = [ch for ch in ukrainian_sample if ord(ch) not in cmap]
            self.assertEqual(missing, [], f'Missing Ukrainian glyphs in {filename}: {missing}')
