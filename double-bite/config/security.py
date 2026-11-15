import hashlib
import ipaddress
import logging
import math
import re
import threading
import time
from typing import Any

from django.conf import settings
from django.core.cache import DEFAULT_CACHE_ALIAS, caches
from django.core.exceptions import DisallowedHost
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.http import url_has_allowed_host_and_scheme
from redis.exceptions import RedisError

logger = logging.getLogger('config')

_RATE_LIMIT_LOCK = threading.Lock()
_SAFE_SCOPE_RE = re.compile(r'[^A-Za-z0-9._:-]+')
_SAFE_ID_RE = re.compile(r'^[A-Za-z0-9._:/-]+\Z')

_REDIS_RATE_LIMIT_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
elseif redis.call('TTL', KEYS[1]) == -1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
local ttl = redis.call('TTL', KEYS[1])
return {current, ttl}
""".strip()

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _parse_trusted_networks(raw_entries: Any) -> tuple[IPNetwork, ...]:
    if not raw_entries:
        return ()
    entries = raw_entries.split(',') if isinstance(raw_entries, str) else raw_entries
    networks: list[IPNetwork] = []
    for entry in entries:
        cleaned = str(entry or '').strip()
        if not cleaned:
            continue
        try:
            networks.append(ipaddress.ip_network(cleaned, strict=False))
        except ValueError:
            logger.warning('Ignoring invalid TRUSTED_PROXY_IPS entry: %r', cleaned)
    return tuple(networks)


def _parse_ip(raw: Any) -> IPAddress | None:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        return ipaddress.ip_address(cleaned)
    except ValueError:
        return None


def _is_trusted_ip(ip_obj: IPAddress, trusted_networks: tuple[IPNetwork, ...]) -> bool:
    if any(ip_obj in network for network in trusted_networks):
        return True
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        mapped = ip_obj.ipv4_mapped
        return any(mapped in network for network in trusted_networks)
    if isinstance(ip_obj, ipaddress.IPv4Address):
        mapped_v6 = ipaddress.IPv6Address(f'::ffff:{ip_obj}')
        return any(mapped_v6 in network for network in trusted_networks)
    return False


def _format_ip(ip_obj: IPAddress) -> str:
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        return str(ip_obj.ipv4_mapped)
    return str(ip_obj)


def get_client_ip(request: HttpRequest) -> str:
    meta = getattr(request, 'META', None) or {}
    raw_remote = str(meta.get('REMOTE_ADDR') or '').strip()

    if not getattr(settings, 'TRUST_PROXY_HEADERS', False):
        return raw_remote

    trusted_networks = _parse_trusted_networks(getattr(settings, 'TRUSTED_PROXY_IPS', ()))
    if not trusted_networks:
        return raw_remote

    remote_ip = _parse_ip(raw_remote)
    if remote_ip is None or not _is_trusted_ip(remote_ip, trusted_networks):
        return raw_remote

    xff = str(meta.get('HTTP_X_FORWARDED_FOR') or '').strip()
    if not xff:
        return raw_remote

    xff_parts = [part.strip() for part in xff.split(',')]
    try:
        num_proxies = max(0, int(getattr(settings, 'NUM_PROXIES', 0) or 0))
    except (TypeError, ValueError):
        num_proxies = 0

    if num_proxies > 0:
        if len(xff_parts) < num_proxies:
            return raw_remote
        for part in reversed(xff_parts[len(xff_parts) - (num_proxies - 1) :]):
            if _parse_ip(part) is None:
                return raw_remote
        candidate_slice = xff_parts[: len(xff_parts) - (num_proxies - 1)]
    else:
        candidate_slice = xff_parts

    for part in reversed(candidate_slice):
        ip_obj = _parse_ip(part)
        if ip_obj is None:
            return raw_remote
        if _is_trusted_ip(ip_obj, trusted_networks):
            continue
        return _format_ip(ip_obj)

    return raw_remote


def build_rate_limit_key(scope: str, identifier: Any) -> str:
    safe_scope = (_SAFE_SCOPE_RE.sub('_', (scope or 'default').strip()) or 'default')[:64]
    raw_id = '' if identifier is None else str(identifier)
    if raw_id and len(raw_id) <= 128 and _SAFE_ID_RE.match(raw_id):
        safe_id = raw_id
    elif raw_id:
        safe_id = hashlib.sha256(raw_id.encode('utf-8')).hexdigest()
    else:
        safe_id = '_empty_'
    return f'rl:{safe_scope}:{safe_id}'


def _get_non_redis_ttl(backend: Any, key: str, full_key: str, period: int) -> int:
    ttl_fn = getattr(backend, 'ttl', None)
    if callable(ttl_fn):
        try:
            raw_ttl = ttl_fn(key)
            if raw_ttl is not None and int(raw_ttl) >= 0:
                return max(1, min(period, int(raw_ttl)))
        except (TypeError, ValueError):
            pass

    expire_info = getattr(backend, '_expire_info', None)
    backend_lock = getattr(backend, '_lock', None)
    if isinstance(expire_info, dict) and backend_lock is not None:
        with backend_lock:
            exp = expire_info.get(full_key)
        if isinstance(exp, int | float):
            remaining = math.ceil(exp - time.time())
            return max(1, min(period, remaining))

    return period


def consume_rate_limit(
    scope: str,
    identifier: Any,
    limit: int,
    period: int,
    *,
    cache_backend: Any = None,
) -> tuple[bool, int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError('limit must be a non-negative integer')
    if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        raise ValueError('period must be a positive integer')

    key = build_rate_limit_key(scope, identifier)
    backend = cache_backend if cache_backend is not None else caches[DEFAULT_CACHE_ALIAS]
    full_key = backend.make_and_validate_key(key) if hasattr(backend, 'make_and_validate_key') else key

    redis_cache_client = getattr(backend, '_cache', None)
    get_client = getattr(redis_cache_client, 'get_client', None)

    if callable(get_client):
        try:
            redis_client = get_client(full_key, write=True)
            raw_result = redis_client.eval(_REDIS_RATE_LIMIT_LUA, 1, full_key, period)
            current = int(raw_result[0])
            ttl = int(raw_result[1])
            if ttl < 0:
                ttl = period
            else:
                ttl = max(1, ttl)
        except (RedisError, OSError, TypeError, ValueError, IndexError) as exc:
            logger.warning('Redis rate limit check failed for %s: %s', key, exc)
            return (False, 0)
    else:
        with _RATE_LIMIT_LOCK:
            if backend.add(key, 1, timeout=period):
                current = 1
            else:
                try:
                    current = int(backend.incr(key))
                except (ValueError, TypeError):
                    if backend.add(key, 1, timeout=period):
                        current = 1
                    else:
                        try:
                            current = int(backend.incr(key))
                        except (ValueError, TypeError):
                            backend.set(key, 1, timeout=period)
                            current = 1
            ttl = _get_non_redis_ttl(backend, key, full_key, period) if current > limit else 0

    if current > limit:
        return (True, max(1, ttl))
    return (False, 0)


def reset_rate_limit(scope: str, identifier: Any, *, cache_backend: Any = None) -> None:
    key = build_rate_limit_key(scope, identifier)
    backend = cache_backend if cache_backend is not None else caches[DEFAULT_CACHE_ALIAS]
    redis_cache_client = getattr(backend, '_cache', None)
    if callable(getattr(redis_cache_client, 'get_client', None)):
        try:
            backend.delete(key)
        except (RedisError, OSError) as exc:
            logger.warning('Redis rate limit reset failed for %s: %s', key, exc)
    else:
        with _RATE_LIMIT_LOCK:
            backend.delete(key)


def rate_limit_429_response(
    request: HttpRequest,
    retry_after: int,
    message: str,
    *,
    json_body: bool = False,
    template_name: str | None = None,
    context: dict[str, Any] | None = None,
) -> HttpResponse:
    safe_retry_after = max(1, int(retry_after or 1))
    if json_body:
        response: HttpResponse = JsonResponse(
            {'error': message, 'retry_after': safe_retry_after},
            status=429,
        )
    elif template_name:
        render_context = dict(context) if context else {}
        render_context.setdefault('rate_limit_message', message)
        render_context.setdefault('retry_after', safe_retry_after)
        response = render(request, template_name, render_context, status=429)
    else:
        response = HttpResponse(
            message,
            status=429,
            content_type='text/plain; charset=utf-8',
        )
    response['Retry-After'] = str(safe_retry_after)
    return response


def get_safe_next_url(
    request: HttpRequest | None,
    raw_next: str | None,
    fallback: str = '/',
) -> str:
    if not isinstance(raw_next, str):
        return fallback
    candidate = raw_next.strip()
    if not candidate or any(ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F for ch in candidate):
        return fallback

    if request is not None:
        try:
            allowed_hosts = {request.get_host()}
        except DisallowedHost:
            return fallback
        require_https = request.is_secure()
    else:
        allowed_hosts = set()
        require_https = True

    if url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts=allowed_hosts,
        require_https=require_https,
    ):
        return candidate
    return fallback
