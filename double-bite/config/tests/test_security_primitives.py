import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.core.cache.backends.redis import RedisCache
from django.core.exceptions import DisallowedHost, ValidationError
from django.db import transaction
from django.shortcuts import render as django_render
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from redis.exceptions import RedisError

from config.security import (
    build_rate_limit_key,
    consume_rate_limit,
    get_client_ip,
    get_safe_next_url,
    rate_limit_429_response,
    reset_rate_limit,
)
from config.transactions import run_on_commit, schedule_on_commit
from config.validators import (
    extract_phone_digits,
    normalize_email,
    normalize_phone,
    phone_validator,
)


class TrustedProxyClientIPTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(TRUST_PROXY_HEADERS=False, TRUSTED_PROXY_IPS=['127.0.0.1'], NUM_PROXIES=0)
    def test_spoofed_xff_ignored_when_trust_proxy_headers_false(self):
        request = self.factory.get(
            '/',
            REMOTE_ADDR='127.0.0.1',
            HTTP_X_FORWARDED_FOR='1.2.3.4',
        )
        self.assertEqual(get_client_ip(request), '127.0.0.1')

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=[], NUM_PROXIES=0)
    def test_spoofed_xff_ignored_when_trusted_proxy_ips_empty(self):
        request = self.factory.get(
            '/',
            REMOTE_ADDR='127.0.0.1',
            HTTP_X_FORWARDED_FOR='1.2.3.4',
        )
        self.assertEqual(get_client_ip(request), '127.0.0.1')

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=['10.0.0.0/8'], NUM_PROXIES=0)
    def test_spoofed_xff_ignored_when_remote_addr_not_in_trusted_proxies(self):
        request = self.factory.get(
            '/',
            REMOTE_ADDR='198.51.100.99',
            HTTP_X_FORWARDED_FOR='1.2.3.4',
        )
        self.assertEqual(get_client_ip(request), '198.51.100.99')

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=['10.0.0.0/8'], NUM_PROXIES=0)
    def test_multi_hop_xff_extracts_first_untrusted_ip_from_right(self):
        request = self.factory.get(
            '/',
            REMOTE_ADDR='10.0.0.4',
            HTTP_X_FORWARDED_FOR='203.0.113.50, 10.0.0.2, 10.0.0.3',
        )
        self.assertEqual(get_client_ip(request), '203.0.113.50')

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=['10.0.0.0/8'], NUM_PROXIES=0)
    def test_leftmost_spoofed_ip_ignored_when_proxy_appends_real_client(self):
        request = self.factory.get(
            '/',
            REMOTE_ADDR='10.0.0.4',
            HTTP_X_FORWARDED_FOR='6.6.6.6, 203.0.113.50, 10.0.0.2',
        )
        self.assertEqual(get_client_ip(request), '203.0.113.50')

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=['10.0.0.0/8'], NUM_PROXIES=0)
    def test_malformed_right_xff_entry_falls_back_to_remote_addr(self):
        request = self.factory.get(
            '/',
            REMOTE_ADDR='10.0.0.4',
            HTTP_X_FORWARDED_FOR='203.0.113.50, not-an-ip',
        )
        self.assertEqual(get_client_ip(request), '10.0.0.4')

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=['10.0.0.0/8'], NUM_PROXIES=0)
    def test_all_trusted_xff_entries_falls_back_to_remote_addr(self):
        request = self.factory.get(
            '/',
            REMOTE_ADDR='10.0.0.4',
            HTTP_X_FORWARDED_FOR='10.0.0.2, 10.0.0.3',
        )
        self.assertEqual(get_client_ip(request), '10.0.0.4')

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=['127.0.0.1'], NUM_PROXIES=2)
    def test_num_proxies_skips_configured_proxy_hops_from_right(self):
        request = self.factory.get(
            '/',
            REMOTE_ADDR='127.0.0.1',
            HTTP_X_FORWARDED_FOR='6.6.6.6, 203.0.113.50, 172.16.0.1',
        )
        self.assertEqual(get_client_ip(request), '203.0.113.50')

        short_request = self.factory.get(
            '/',
            REMOTE_ADDR='127.0.0.1',
            HTTP_X_FORWARDED_FOR='203.0.113.50',
        )
        self.assertEqual(get_client_ip(short_request), '127.0.0.1')

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=['fc00::/7', '10.0.0.0/8'], NUM_PROXIES=0)
    def test_ipv6_and_ipv4_mapped_trusted_proxies(self):
        request = self.factory.get(
            '/',
            REMOTE_ADDR='::ffff:10.0.0.5',
            HTTP_X_FORWARDED_FOR='2001:db8::42, fd00::1',
        )
        self.assertEqual(get_client_ip(request), '2001:db8::42')

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=['127.0.0.1'], NUM_PROXIES=0)
    def test_missing_remote_addr_never_trusts_xff(self):
        request = self.factory.get('/', HTTP_X_FORWARDED_FOR='1.2.3.4')
        request.META.pop('REMOTE_ADDR', None)
        self.assertEqual(get_client_ip(request), '')

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=['::ffff:10.0.0.0/104'], NUM_PROXIES=0)
    def test_ipv4_mapped_client_in_xff_canonicalized_and_symmetric_cidr(self):
        request = self.factory.get(
            '/',
            REMOTE_ADDR='10.0.0.4',
            HTTP_X_FORWARDED_FOR='::ffff:203.0.113.50, 10.0.0.2',
        )
        self.assertEqual(get_client_ip(request), '203.0.113.50')

    @override_settings(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_IPS=['127.0.0.1'], NUM_PROXIES=1)
    def test_num_proxies_one_and_empty_xff(self):
        request = self.factory.get(
            '/',
            REMOTE_ADDR='127.0.0.1',
            HTTP_X_FORWARDED_FOR='6.6.6.6, 203.0.113.50',
        )
        self.assertEqual(get_client_ip(request), '203.0.113.50')

        empty_xff_req = self.factory.get('/', REMOTE_ADDR='127.0.0.1', HTTP_X_FORWARDED_FOR='   ')
        self.assertEqual(get_client_ip(empty_xff_req), '127.0.0.1')


class RateLimiterTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_locmem_rate_limit_allows_up_to_limit_and_returns_retry_after(self):
        for _ in range(3):
            limited, retry_after = consume_rate_limit('login', '198.51.100.1', limit=3, period=60)
            self.assertFalse(limited)
            self.assertEqual(retry_after, 0)

        limited, retry_after = consume_rate_limit('login', '198.51.100.1', limit=3, period=60)
        self.assertTrue(limited)
        self.assertGreaterEqual(retry_after, 1)
        self.assertLessEqual(retry_after, 60)

        reset_rate_limit('login', '198.51.100.1')
        limited_after_reset, retry_after_reset = consume_rate_limit('login', '198.51.100.1', limit=3, period=60)
        self.assertFalse(limited_after_reset)
        self.assertEqual(retry_after_reset, 0)

    def test_locmem_rate_limit_does_not_reset_ttl_on_increments(self):
        consume_rate_limit('chat', 'user@example.com', limit=5, period=30)
        full_key = next(k for k in cache._expire_info if 'rl:chat:' in k)
        initial_exp = cache._expire_info[full_key]
        time.sleep(0.05)
        consume_rate_limit('chat', 'user@example.com', limit=5, period=30)
        self.assertEqual(cache._expire_info[full_key], initial_exp)

    def test_locmem_rate_limit_window_expires(self):
        consume_rate_limit('burst', '198.51.100.5', limit=1, period=1)
        limited, _ = consume_rate_limit('burst', '198.51.100.5', limit=1, period=1)
        self.assertTrue(limited)
        time.sleep(1.1)
        limited_after_window, _ = consume_rate_limit('burst', '198.51.100.5', limit=1, period=1)
        self.assertFalse(limited_after_window)

    def test_locmem_rate_limit_handles_concurrent_threads_atomically(self):
        num_threads = 20
        limit = 5
        barrier = threading.Barrier(num_threads)

        def hit():
            barrier.wait()
            return consume_rate_limit('concurrent', 'client-1', limit=limit, period=60)[0]

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            results = list(pool.map(lambda _: hit(), range(num_threads)))

        allowed_count = sum(1 for blocked in results if not blocked)
        blocked_count = sum(1 for blocked in results if blocked)
        self.assertEqual(allowed_count, limit)
        self.assertEqual(blocked_count, num_threads - limit)

    def test_locmem_rate_limit_recovers_if_key_expires_between_add_and_incr(self):
        mock_backend = MagicMock(spec=['add', 'incr', 'make_and_validate_key'])
        mock_backend.make_and_validate_key.side_effect = lambda k: k
        mock_backend.add.side_effect = [False, True]
        mock_backend.incr.side_effect = ValueError('Key expired')

        limited, retry_after = consume_rate_limit('edge', 'id1', limit=2, period=30, cache_backend=mock_backend)
        self.assertFalse(limited)
        self.assertEqual(retry_after, 0)
        self.assertEqual(mock_backend.add.call_count, 2)

    def test_invalid_limit_or_period_raises_value_error(self):
        for bad_limit, bad_period in (
            (-1, 60),
            (True, 60),
            (5, 0),
            (5, -10),
            (5, 0.5),
            (5, True),
        ):
            with self.assertRaises(ValueError):
                consume_rate_limit('scope', 'id', limit=bad_limit, period=bad_period)

    def test_build_rate_limit_key_sanitizes_newlines_long_scopes_and_zero_id(self):
        newline_key = build_rate_limit_key('login', '198.51.100.1\n')
        self.assertNotIn('\n', newline_key)

        long_key = build_rate_limit_key('a' * 300, 'b' * 128)
        self.assertLess(len(long_key), 250)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            cache.make_and_validate_key(long_key)
        self.assertEqual(caught, [])

        self.assertEqual(build_rate_limit_key('scope', 0), 'rl:scope:0')

    def test_locmem_corrupted_value_and_near_zero_ttl_recover_cleanly(self):
        raw_key = build_rate_limit_key('corrupt', 'id-1')
        cache.set(raw_key, 'not-an-int', timeout=30)
        limited, retry_after = consume_rate_limit('corrupt', 'id-1', limit=2, period=30)
        self.assertFalse(limited)
        self.assertEqual(retry_after, 0)

        fake_backend = MagicMock(spec=['add', 'incr', 'make_and_validate_key', '_expire_info', '_lock'])
        fake_backend.make_and_validate_key.return_value = 'full:k'
        fake_backend.add.return_value = False
        fake_backend.incr.return_value = 3
        fake_backend._lock = threading.Lock()
        fake_backend._expire_info = {'full:k': time.time() - 0.01}

        limited, retry_after = consume_rate_limit('near', 'id-2', limit=2, period=60, cache_backend=fake_backend)
        self.assertTrue(limited)
        self.assertEqual(retry_after, 1)

    def test_redis_backend_uses_atomic_lua_script(self):
        backend = RedisCache('redis://127.0.0.1:6379/0', {'KEY_PREFIX': 'db'})
        state: dict[str, tuple[int, int]] = {}

        def fake_eval(script, numkeys, key, period):
            self.assertEqual(numkeys, 1)
            self.assertIn('INCR', script)
            self.assertIn('EXPIRE', script)
            self.assertIn('TTL', script)
            current, ttl = state.get(key, (0, -1))
            current += 1
            if current == 1 or ttl == -1:
                ttl = int(period)
            state[key] = (current, ttl)
            return [current, ttl]

        fake_redis_client = MagicMock()
        fake_redis_client.eval.side_effect = fake_eval

        with patch.object(backend._cache, 'get_client', return_value=fake_redis_client) as mock_get_client:
            for _ in range(2):
                limited, retry_after = consume_rate_limit(
                    'login', '203.0.113.10', limit=2, period=45, cache_backend=backend
                )
                self.assertFalse(limited)
                self.assertEqual(retry_after, 0)

            limited, retry_after = consume_rate_limit(
                'login', '203.0.113.10', limit=2, period=45, cache_backend=backend
            )
            self.assertTrue(limited)
            self.assertEqual(retry_after, 45)
            self.assertEqual(mock_get_client.call_count, 3)
            self.assertEqual(fake_redis_client.eval.call_count, 3)

    def test_redis_backend_error_fails_open_with_warning(self):
        backend = RedisCache('redis://127.0.0.1:6379/0', {})
        fake_redis_client = MagicMock()
        fake_redis_client.eval.side_effect = RedisError('Redis unavailable')

        with (
            patch.object(backend._cache, 'get_client', return_value=fake_redis_client),
            self.assertLogs('config', level='WARNING') as logs,
        ):
            limited, retry_after = consume_rate_limit(
                'login', '203.0.113.10', limit=5, period=60, cache_backend=backend
            )

        self.assertFalse(limited)
        self.assertEqual(retry_after, 0)
        self.assertTrue(any('Redis rate limit check failed' in line for line in logs.output))

    def test_redis_ttl_zero_vs_missing_sentinel_and_reset_fail_open(self):
        backend = RedisCache('redis://127.0.0.1:6379/0', {})
        fake_redis_client = MagicMock()
        fake_redis_client.eval.side_effect = [[3, 0], [3, -1]]
        fake_redis_client.delete.side_effect = RedisError('Redis unavailable')

        with patch.object(backend._cache, 'get_client', return_value=fake_redis_client):
            limited_zero, retry_zero = consume_rate_limit(
                'login', '203.0.113.10', limit=2, period=45, cache_backend=backend
            )
            limited_missing, retry_missing = consume_rate_limit(
                'login', '203.0.113.10', limit=2, period=45, cache_backend=backend
            )
            with self.assertLogs('config', level='WARNING') as logs:
                reset_rate_limit('login', '203.0.113.10', cache_backend=backend)

        self.assertTrue(limited_zero)
        self.assertEqual(retry_zero, 1)
        self.assertTrue(limited_missing)
        self.assertEqual(retry_missing, 45)
        self.assertTrue(any('Redis rate limit reset failed' in line for line in logs.output))

    def test_rate_limit_429_response_text_and_json(self):
        rf = RequestFactory()
        request = rf.post('/accounts/login/')

        text_resp = rate_limit_429_response(request, retry_after=42, message='Too many requests')
        self.assertEqual(text_resp.status_code, 429)
        self.assertEqual(text_resp['Retry-After'], '42')
        self.assertEqual(text_resp.content.decode('utf-8'), 'Too many requests')

        json_resp = rate_limit_429_response(request, retry_after=0, message='Slow down', json_body=True)
        self.assertEqual(json_resp.status_code, 429)
        self.assertEqual(json_resp['Retry-After'], '1')
        self.assertEqual(json_resp['Content-Type'], 'application/json')
        self.assertJSONEqual(json_resp.content, {'error': 'Slow down', 'retry_after': 1})

        with patch('config.security.render', wraps=django_render) as mock_render:
            html_resp = rate_limit_429_response(
                request,
                retry_after=15,
                message='Зачекайте 15 с.',
                template_name='emails/base_email.html',
                context={'content': 'rate-limited'},
            )
        self.assertEqual(html_resp.status_code, 429)
        self.assertEqual(html_resp['Retry-After'], '15')
        mock_render.assert_called_once()
        passed_ctx = mock_render.call_args.args[2]
        self.assertEqual(passed_ctx['rate_limit_message'], 'Зачекайте 15 с.')
        self.assertEqual(passed_ctx['retry_after'], 15)


class OnCommitHelperTests(TestCase):
    def test_schedule_on_commit_executes_only_after_commit(self):
        calls: list[tuple[str, int]] = []

        def record(name: str, count: int = 1) -> None:
            calls.append((name, count))

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                schedule_on_commit(record, 'order-1', count=2)
                self.assertEqual(calls, [])

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(calls, [('order-1', 2)])

    def test_schedule_on_commit_skips_callback_on_rollback(self):
        calls: list[str] = []

        def record(value: str) -> None:
            calls.append(value)

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    schedule_on_commit(record, 'rolled-back')
                    raise RuntimeError('force rollback')

        self.assertEqual(len(callbacks), 0)
        self.assertEqual(calls, [])

    def test_schedule_on_commit_logs_exception_without_crashing(self):
        def failing_callback() -> None:
            raise RuntimeError('SMTP down')

        with (
            self.assertLogs('config', level='ERROR') as logs,
            self.captureOnCommitCallbacks(execute=True) as callbacks,
        ):
            with transaction.atomic():
                run_on_commit(failing_callback)

        self.assertEqual(len(callbacks), 1)
        self.assertTrue(any('Post-commit callback' in entry and 'SMTP down' in entry for entry in logs.output))

    def test_schedule_on_commit_skips_nested_savepoint_on_rollback(self):
        calls: list[str] = []

        def record(value: str) -> None:
            calls.append(value)

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                schedule_on_commit(record, 'outer-committed')
                try:
                    with transaction.atomic():
                        schedule_on_commit(record, 'inner-rolled-back')
                        raise RuntimeError('inner failure')
                except RuntimeError:
                    pass

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(calls, ['outer-committed'])

    def test_schedule_on_commit_requires_callable(self):
        with self.assertRaises(TypeError):
            schedule_on_commit('not-callable')  # type: ignore[arg-type]


class ValidatorsAndSafeRedirectTests(SimpleTestCase):
    def test_phone_validator_accepts_valid_ukrainian_numbers(self):
        for valid in ('+380501234567', '380671112233'):
            phone_validator(valid)

    def test_phone_validator_rejects_invalid_numbers(self):
        for invalid in (
            '0501234567',
            '+38050123456',
            '+3805012345678',
            '+14155552671',
            'abc',
            '+380٥٠١٢٣٤٥٦٧',
            '+380501234567\n',
        ):
            with self.assertRaises(ValidationError):
                phone_validator(invalid)

    def test_normalize_phone(self):
        self.assertEqual(normalize_phone('+380 (50) 123-45-67'), '+380501234567')
        self.assertEqual(normalize_phone('050 123-45-67'), '+380501234567')
        self.assertEqual(normalize_phone('050\u2013123\u201445-67'), '+380501234567')
        self.assertEqual(normalize_phone('+38 (050) 123 45 67'), '+380501234567')
        self.assertEqual(normalize_phone('380671112233'), '+380671112233')
        self.assertEqual(normalize_phone(''), '')
        self.assertEqual(normalize_phone(None), '')
        self.assertEqual(normalize_phone('abc'), 'abc')

    def test_extract_phone_digits(self):
        self.assertEqual(extract_phone_digits('+380 (50) 123-45-67'), '380501234567')
        self.assertEqual(extract_phone_digits('050-123-45-67', max_len=10), '0501234567')
        self.assertEqual(extract_phone_digits('0' * 30, max_len=20), '0' * 20)
        self.assertEqual(extract_phone_digits('+380501234567', max_len=0), '')
        self.assertEqual(extract_phone_digits(None), '')

    def test_normalize_email(self):
        self.assertEqual(normalize_email('  User@Example.COM  '), 'user@example.com')
        self.assertEqual(normalize_email(''), '')
        self.assertEqual(normalize_email(None), '')

    def test_get_safe_next_url(self):
        rf = RequestFactory()
        http_req = rf.get('/accounts/login/')
        https_req = rf.get('/accounts/login/', secure=True)

        self.assertEqual(get_safe_next_url(http_req, '/menu/'), '/menu/')
        self.assertEqual(get_safe_next_url(http_req, '  /orders/cart/?step=1  '), '/orders/cart/?step=1')
        self.assertEqual(get_safe_next_url(http_req, 'http://testserver/menu/'), 'http://testserver/menu/')
        self.assertEqual(get_safe_next_url(https_req, 'https://testserver/menu/'), 'https://testserver/menu/')

        self.assertEqual(get_safe_next_url(https_req, 'http://testserver/menu/'), '/')
        self.assertEqual(get_safe_next_url(None, '/menu/'), '/menu/')
        self.assertEqual(get_safe_next_url(None, 'https://testserver/menu/'), '/')
        for unsafe in (
            None,
            '',
            '   ',
            '//evil.com',
            '///evil.com',
            '/\\evil.com',
            '\\\\evil.com',
            'https://evil.com/phish',
            'https:evil.com',
            'http:evil.com',
            'javascript:alert(1)',
            '/menu/\r\nSet-Cookie: a=b',
            '/\x85evil',
        ):
            self.assertEqual(get_safe_next_url(http_req, unsafe, fallback='/accounts/profile/'), '/accounts/profile/')

        bad_host_req = MagicMock()
        bad_host_req.get_host.side_effect = DisallowedHost('bad host')
        self.assertEqual(get_safe_next_url(bad_host_req, '/menu/'), '/')
