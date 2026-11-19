import asyncio
import inspect
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncGenerator
from typing import Any

from asgiref.sync import sync_to_async
from config.constants import DEFAULT_RESTAURANT_HOTLINE
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, SynchronousOnlyOperation
from django.db import DatabaseError
from django.http import HttpRequest

from support.models import ChatSession

from .prompts import SYSTEM_PROMPT
from .tools import SUPPORT_AGENT_TOOLS, execute_agent_tool

try:
    from mistralai import Mistral
except (ImportError, AttributeError):
    from mistralai.client import Mistral

logger = logging.getLogger('support')

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2
DEFAULT_MISTRAL_TIMEOUT_MS = 30000
DEFAULT_MAX_ROUNDS = 3
CART_MUTATION_TOOLS = frozenset({'add_to_cart', 'remove_from_cart', 'update_cart_quantity'})
INVALID_TOOL_ARGS_ERROR = 'Invalid tool arguments, expected JSON object'
_SYSTEM_RANDOM = secrets.SystemRandom()


class MistralSupportAgent:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_ms: int | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ):
        if api_key is not None:
            self.api_key = api_key
        else:
            self.api_key = (
                os.getenv('MISTRAL_API_KEY')
                or getattr(settings, 'MISTRAL_API_KEY', '')
                or ''
            )

        self.model = model or os.getenv('MISTRAL_MODEL', 'mistral-small-latest')
        self.temperature = float(
            temperature
            if temperature is not None
            else os.getenv('MISTRAL_TEMPERATURE', '0.2')
        )
        self.max_tokens = int(
            max_tokens
            if max_tokens is not None
            else os.getenv('MISTRAL_MAX_TOKENS', '1024')
        )

        if timeout_ms is not None:
            resolved_timeout = int(timeout_ms)
        else:
            raw_timeout = os.getenv('MISTRAL_TIMEOUT_MS', '').strip()
            try:
                resolved_timeout = (
                    int(raw_timeout) if raw_timeout else DEFAULT_MISTRAL_TIMEOUT_MS
                )
            except ValueError:
                resolved_timeout = DEFAULT_MISTRAL_TIMEOUT_MS
        self.timeout_ms = (
            resolved_timeout if resolved_timeout > 0 else DEFAULT_MISTRAL_TIMEOUT_MS
        )
        self.max_rounds = max(1, int(max_rounds))

    @staticmethod
    def _get_hotline() -> str:
        hotline = getattr(settings, 'RESTAURANT_HOTLINE', DEFAULT_RESTAURANT_HOTLINE)
        return str(hotline or DEFAULT_RESTAURANT_HOTLINE)

    @staticmethod
    def _get_cart_context(request: HttpRequest | None) -> str:
        if not request:
            return ''
        try:
            from orders.services import CartService

            cart = CartService.get_cart(request)
            if not cart or cart.items.count() == 0:
                return '\n\nПОТОЧНИЙ СТАН КОШИКА КЛІЄНТА: кошик наразі порожній.'
            items_desc = [
                f'- {item.dish.title} ({item.quantity} шт.)'
                for item in cart.items.select_related('dish').all()
                if item.dish
            ]
            return (
                '\n\nПОТОЧНИЙ СТАН КОШИКА КЛІЄНТА:\n'
                + '\n'.join(items_desc)
                + f'\nЗагальна сума: {cart.total_amount} грн'
            )
        except (
            ImportError,
            DatabaseError,
            ObjectDoesNotExist,
            SynchronousOnlyOperation,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            logger.warning('Failed to load cart context for support agent: %s', exc)
            return ''

    async def stream_chat_response(
        self,
        chat_history: list[dict[str, Any]],
        request: HttpRequest | None = None,
        session: ChatSession | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        hotline = self._get_hotline()
        if not self.api_key:
            logger.warning('MISTRAL_API_KEY is not configured.')
            yield {
                'token': (
                    'Вибачте, асистент тимчасово недоступний '
                    '(відсутній ключ доступу). '
                    f'Будь ласка, зверніться за телефоном: {hotline}.'
                )
            }
            return

        cart_context = await sync_to_async(
            self._get_cart_context, thread_sensitive=True
        )(request)
        system_content = SYSTEM_PROMPT + cart_context

        messages: list[dict[str, Any]] = [
            {'role': 'system', 'content': system_content}
        ]
        for msg in chat_history:
            role = msg.get('role', 'user')
            if role == 'operator':
                role = 'assistant'
            content = msg.get('content', '')
            messages.append({'role': role, 'content': content})

        try:
            async for chunk in self._stream_mistral(messages, request, session):
                yield chunk
            return
        except Exception:
            logger.exception('Mistral API error')

        yield {
            'error': (
                'Вибачте, асистент тимчасово недоступний. '
                'Будь ласка, спробуйте через хвилину або зверніться '
                f'за телефоном: {hotline}.'
            )
        }

    # ------------------------------------------------------------------
    # Mistral streaming & tool helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_delta_text(content: Any) -> str:
        if not content:
            return ''
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    if part.get('type', 'text') == 'text':
                        text_val = part.get('text')
                        if isinstance(text_val, str):
                            parts.append(text_val)
                else:
                    chunk_type = getattr(part, 'type', None)
                    if chunk_type in (None, 'text'):
                        text_val = getattr(part, 'text', None)
                        if isinstance(text_val, str):
                            parts.append(text_val)
            return ''.join(parts)
        return ''

    @staticmethod
    def _update_round_usage(data: Any, round_usage: dict[str, int]) -> None:
        usage = data.get('usage') if isinstance(data, dict) else getattr(data, 'usage', None)
        if usage is None:
            return
        is_dict = isinstance(usage, dict)
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            val = usage.get(key) if is_dict else getattr(usage, key, None)
            if isinstance(val, int) and not isinstance(val, bool):
                round_usage[key] = val

    @staticmethod
    def _accumulate_tool_calls(
        delta_tool_calls: list[Any] | tuple[Any, ...],
        tool_calls_by_index: dict[int, dict[str, Any]],
        round_num: int,
    ) -> None:
        for pos, tc in enumerate(delta_tool_calls):
            is_dict = isinstance(tc, dict)
            raw_index = tc.get('index') if is_dict else getattr(tc, 'index', None)
            if isinstance(raw_index, int) and not isinstance(raw_index, bool):
                tc_index = raw_index
            else:
                tc_index = pos

            bucket = tool_calls_by_index.setdefault(
                tc_index,
                {
                    'id': f'call_{round_num}_{tc_index}',
                    'has_explicit_id': False,
                    'name': '',
                    'arg_parts': [],
                    'arg_dict': None,
                    'raw_other': None,
                    'has_raw_other': False,
                },
            )

            tc_id = tc.get('id') if is_dict else getattr(tc, 'id', None)
            if isinstance(tc_id, str) and tc_id and tc_id != 'null':
                if not bucket['has_explicit_id']:
                    bucket['id'] = tc_id
                    bucket['has_explicit_id'] = True

            fn = tc.get('function') if is_dict else getattr(tc, 'function', None)
            if fn is None:
                continue

            fn_is_dict = isinstance(fn, dict)
            fn_name = fn.get('name') if fn_is_dict else getattr(fn, 'name', None)
            if isinstance(fn_name, str) and fn_name and not bucket['name']:
                bucket['name'] = fn_name

            fn_args = (
                fn.get('arguments') if fn_is_dict else getattr(fn, 'arguments', None)
            )
            if isinstance(fn_args, str):
                if fn_args:
                    bucket['arg_parts'].append(fn_args)
            elif isinstance(fn_args, dict):
                bucket['arg_dict'] = fn_args
            elif fn_args is not None and type(fn_args).__name__ != 'Unset':
                bucket['raw_other'] = fn_args
                bucket['has_raw_other'] = True

    @staticmethod
    def _resolve_raw_tool_args(bucket: dict[str, Any]) -> Any:
        if bucket['has_raw_other']:
            return bucket['raw_other']
        if bucket['arg_dict'] is not None and not bucket['arg_parts']:
            return bucket['arg_dict']
        return ''.join(bucket['arg_parts'])

    @staticmethod
    def _parse_tool_arguments(
        args_raw: Any,
    ) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        if isinstance(args_raw, dict):
            return args_raw, None
        if isinstance(args_raw, str):
            stripped = args_raw.strip()
            if not stripped:
                return {}, None
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return None, {'error': INVALID_TOOL_ARGS_ERROR}
            if not isinstance(parsed, dict):
                return None, {'error': INVALID_TOOL_ARGS_ERROR}
            return parsed, None
        return None, {'error': INVALID_TOOL_ARGS_ERROR}

    @staticmethod
    def _build_assistant_tool_call_message(
        content: str,
        accumulated_calls: list[tuple[dict[str, Any], Any]],
    ) -> dict[str, Any]:
        serialized_calls: list[dict[str, Any]] = []
        for bucket, raw_args in accumulated_calls:
            if isinstance(raw_args, (str, dict)):
                msg_args = raw_args
            else:
                msg_args = json.dumps(raw_args, ensure_ascii=False)
            serialized_calls.append({
                'id': bucket['id'],
                'type': 'function',
                'function': {
                    'name': bucket['name'],
                    'arguments': msg_args,
                },
            })
        return {
            'role': 'assistant',
            'content': content,
            'tool_calls': serialized_calls,
        }

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        close_fn = getattr(stream, 'close', None)
        if callable(close_fn) and inspect.iscoroutinefunction(close_fn):
            await close_fn()

    async def _stream_mistral(
        self,
        messages: list[dict[str, Any]],
        request: HttpRequest | None,
        session: ChatSession | None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        start_time = time.monotonic()
        rounds_used = 0
        tools_called: list[str] = []
        usage_totals = {
            'prompt_tokens': 0,
            'completion_tokens': 0,
            'total_tokens': 0,
        }
        pending_cart_mutations: list[dict[str, Any]] = []
        pending_tool_calls = False
        hotline = self._get_hotline()

        async with Mistral(api_key=self.api_key, timeout_ms=self.timeout_ms) as client:
            for _round_idx in range(self.max_rounds):
                rounds_used += 1
                stream = await self._call_with_retry(
                    client.chat.stream_async,
                    model=self.model,
                    messages=messages,
                    tools=SUPPORT_AGENT_TOOLS,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    timeout_ms=self.timeout_ms,
                )
                round_text_parts: list[str] = []
                tool_calls_by_index: dict[int, dict[str, Any]] = {}
                round_usage = {
                    'prompt_tokens': 0,
                    'completion_tokens': 0,
                    'total_tokens': 0,
                }

                try:
                    async for chunk in stream:
                        data = getattr(chunk, 'data', None)
                        if data is None:
                            continue
                        self._update_round_usage(data, round_usage)
                        choices = getattr(data, 'choices', None)
                        if not choices:
                            continue
                        delta = getattr(choices[0], 'delta', None)
                        if delta is None:
                            continue

                        text = self._extract_delta_text(getattr(delta, 'content', None))
                        if text:
                            round_text_parts.append(text)
                            yield {'token': text}

                        delta_tool_calls = getattr(delta, 'tool_calls', None)
                        if isinstance(delta_tool_calls, (list, tuple)):
                            self._accumulate_tool_calls(
                                delta_tool_calls,
                                tool_calls_by_index,
                                rounds_used,
                            )
                finally:
                    await self._close_stream(stream)

                for key in usage_totals:
                    usage_totals[key] += round_usage[key]

                if not tool_calls_by_index:
                    pending_tool_calls = False
                    break

                accumulated_calls = [
                    (
                        tool_calls_by_index[idx],
                        self._resolve_raw_tool_args(tool_calls_by_index[idx]),
                    )
                    for idx in sorted(tool_calls_by_index)
                ]
                messages.append(
                    self._build_assistant_tool_call_message(
                        ''.join(round_text_parts),
                        accumulated_calls,
                    )
                )

                for bucket, raw_args in accumulated_calls:
                    fn_name = bucket['name']
                    tc_id = bucket['id']
                    parsed_args, parse_error = self._parse_tool_arguments(raw_args)
                    if parse_error is not None or parsed_args is None:
                        tool_res: dict[str, Any] = parse_error or {
                            'error': INVALID_TOOL_ARGS_ERROR,
                        }
                    else:
                        tools_called.append(fn_name)
                        tool_res = await sync_to_async(
                            execute_agent_tool, thread_sensitive=True
                        )(
                            fn_name,
                            parsed_args,
                            request=request,
                            session=session,
                        )

                        if fn_name == 'escalate_to_operator' and 'error' not in tool_res:
                            yield {
                                'escalated': True,
                                'ticket_id': tool_res.get('ticket_id'),
                                'support_phone': tool_res.get(
                                    'support_phone', hotline
                                ),
                            }

                        if (
                            fn_name in CART_MUTATION_TOOLS
                            and tool_res.get('success')
                        ):
                            pending_cart_mutations.append({
                                'cart_mutation': True,
                                'cart_items_count': tool_res.get(
                                    'cart_items_count', 0
                                ),
                                'total_amount': tool_res.get('total_amount', 0.0),
                                'action': tool_res.get('action'),
                                'dish_id': tool_res.get('dish_id'),
                            })

                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tc_id,
                        'name': fn_name,
                        'content': json.dumps(tool_res, ensure_ascii=False),
                    })

                pending_tool_calls = True

            if pending_tool_calls:
                rounds_used += 1
                final_stream = await self._call_with_retry(
                    client.chat.stream_async,
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    timeout_ms=self.timeout_ms,
                )
                final_usage = {
                    'prompt_tokens': 0,
                    'completion_tokens': 0,
                    'total_tokens': 0,
                }
                try:
                    async for chunk in final_stream:
                        data = getattr(chunk, 'data', None)
                        if data is None:
                            continue
                        self._update_round_usage(data, final_usage)
                        choices = getattr(data, 'choices', None)
                        if not choices:
                            continue
                        delta = getattr(choices[0], 'delta', None)
                        if delta is None:
                            continue
                        text = self._extract_delta_text(getattr(delta, 'content', None))
                        if text:
                            yield {'token': text}
                finally:
                    await self._close_stream(final_stream)

                for key in usage_totals:
                    usage_totals[key] += final_usage[key]

            for mutation_event in pending_cart_mutations:
                yield mutation_event

        latency_ms = round((time.monotonic() - start_time) * 1000, 1)
        logger.info(
            'Mistral chat completed: model=%s rounds=%d latency_ms=%.1f '
            'tools_called=%s usage=%s',
            self.model,
            rounds_used,
            latency_ms,
            tools_called,
            json.dumps(usage_totals),
            extra={
                'model': self.model,
                'rounds': rounds_used,
                'latency_ms': latency_ms,
                'tools_called': tools_called,
                'usage': usage_totals,
            },
        )

    # ------------------------------------------------------------------
    # Retry helper (Mistral only)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """Return True if *exc* looks like an HTTP 429 rate-limit error."""
        # mistralai.client.errors.SDKError stores status on the response
        if hasattr(exc, 'status_code') and exc.status_code == 429:
            return True
        # Some SDK versions expose the raw response object
        raw = getattr(exc, 'raw_response', None) or getattr(exc, 'http_res', None)
        if raw is not None and getattr(raw, 'status_code', None) == 429:
            return True
        return bool('429' in str(exc) and 'rate' in str(exc).lower())

    async def _call_with_retry(self, func, *args, **kwargs):
        """Call *func* with retry + exponential backoff + jitter on 429 errors."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                if self._is_rate_limit_error(exc) and attempt < MAX_RETRIES:
                    base_delay = RETRY_BASE_DELAY * (2 ** attempt)
                    jitter = _SYSTEM_RANDOM.uniform(0.0, base_delay * 0.5)
                    delay = base_delay + jitter
                    logger.warning(
                        'Rate-limited by Mistral API (attempt %d/%d). '
                        'Retrying in %.2fs…',
                        attempt + 1,
                        MAX_RETRIES,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
