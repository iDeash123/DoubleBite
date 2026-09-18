import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import HttpRequest

from support.models import ChatSession

from .prompts import SYSTEM_PROMPT
from .tools import SUPPORT_AGENT_TOOLS, execute_agent_tool

try:
    from mistralai import Mistral
except (ImportError, AttributeError):
    from mistralai.client import Mistral

from google import genai
from google.genai import types as genai_types

logger = logging.getLogger('support')

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds; delays will be 2, 4, 8 …


# ---------------------------------------------------------------------------
# Gemini tool-schema converter
# ---------------------------------------------------------------------------

def _mistral_tools_to_gemini() -> list[genai_types.Tool]:
    """Convert SUPPORT_AGENT_TOOLS (OpenAI/Mistral format) → Gemini format."""
    declarations: list[genai_types.FunctionDeclaration] = []
    for tool_def in SUPPORT_AGENT_TOOLS:
        fn = tool_def['function']
        params_schema = fn.get('parameters', {})
        declarations.append(genai_types.FunctionDeclaration(
            name=fn['name'],
            description=fn.get('description', ''),
            parameters=params_schema if params_schema.get('properties') else None,
        ))
    return [genai_types.Tool(function_declarations=declarations)]


class MistralSupportAgent:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        gemini_api_key: str | None = None,
        gemini_model: str | None = None,
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

        if gemini_api_key is not None:
            self.gemini_api_key = gemini_api_key
        else:
            self.gemini_api_key = (
                os.getenv('GEMINI_API_KEY')
                or getattr(settings, 'GEMINI_API_KEY', '')
                or ''
            )
        self.gemini_model = (
            gemini_model
            or getattr(settings, 'GEMINI_MODEL', '')
            or os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
        )

    @staticmethod
    def _get_cart_context(request: HttpRequest | None) -> str:
        if not request:
            return ""
        try:
            from orders.services import CartService
            cart = CartService.get_cart(request)
            if not cart or cart.items.count() == 0:
                return "\n\nПОТОЧНИЙ СТАН КОШИКА КЛІЄНТА: кошик наразі порожній."
            items_desc = [
                f"- {item.dish.title} ({item.quantity} шт.)"
                for item in cart.items.select_related('dish').all()
                if item.dish
            ]
            return (
                "\n\nПОТОЧНИЙ СТАН КОШИКА КЛІЄНТА:\n"
                + "\n".join(items_desc)
                + f"\nЗагальна сума: {cart.total_amount} грн"
            )
        except Exception:
            return ""

    async def stream_chat_response(
        self,
        chat_history: list[dict[str, Any]],
        request: HttpRequest | None = None,
        session: ChatSession | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        if not self.api_key and not self.gemini_api_key:
            logger.warning('Neither MISTRAL_API_KEY nor GEMINI_API_KEY is configured.')
            yield {
                'token': (
                    'Вибачте, асистент тимчасово недоступний (відсутній ключ доступу). '
                    'Будь ласка, зверніться за телефоном: +380 44 123 45 67.'
                )
            }
            return

        cart_context = await sync_to_async(self._get_cart_context, thread_sensitive=True)(request)
        system_content = SYSTEM_PROMPT + cart_context

        messages: list[dict[str, Any]] = [
            {'role': 'system', 'content': system_content}
        ]
        for msg in chat_history:
            role = msg.get('role', 'user')
            content = msg.get('content', '')
            messages.append({'role': role, 'content': content})

        # --- Try Mistral first ---
        if self.api_key:
            try:
                async for chunk in self._stream_mistral(messages, request, session):
                    yield chunk
                return
            except Exception:
                logger.warning('Mistral API unavailable or rate limited, falling back to Gemini…')

        if self.gemini_api_key:
            yielded_any = False
            try:
                async for chunk in self._stream_gemini(messages, request, session):
                    yielded_any = True
                    yield chunk
                return
            except Exception:
                logger.exception('Gemini API error')
                if yielded_any:
                    return

        yield {
            'error': (
                'Вибачте, асистент тимчасово недоступний. Будь ласка, спробуйте через '
                'хвилину або зверніться за телефоном: +380 44 123 45 67.'
            )
        }

    # ------------------------------------------------------------------
    # Mistral backend
    # ------------------------------------------------------------------

    async def _stream_mistral(
        self,
        messages: list[dict[str, Any]],
        request: HttpRequest | None,
        session: ChatSession | None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        async with Mistral(api_key=self.api_key) as client:
            response = await self._call_with_retry(
                client.chat.complete_async,
                model=self.model,
                messages=messages,
                tools=SUPPORT_AGENT_TOOLS,
                temperature=self.temperature,
            )

            response_message = response.choices[0].message
            tool_calls = getattr(response_message, 'tool_calls', None)

            if tool_calls:
                if hasattr(response_message, 'model_dump'):
                    dumped_msg = response_message.model_dump(exclude_none=True)
                    messages.append(dumped_msg)
                else:
                    messages.append({
                        'role': 'assistant',
                        'content': getattr(response_message, 'content', '') or '',
                        'tool_calls': [
                            {
                                'id': tc.id,
                                'type': 'function',
                                'function': {
                                    'name': tc.function.name,
                                    'arguments': tc.function.arguments,
                                },
                            }
                            for tc in tool_calls
                        ],
                    })

                for tc in tool_calls:
                    fn_name = tc.function.name
                    args_raw = tc.function.arguments
                    if isinstance(args_raw, str):
                        try:
                            args = json.loads(args_raw)
                        except json.JSONDecodeError:
                            args = {}
                    else:
                        args = dict(args_raw)

                    tool_res = await sync_to_async(execute_agent_tool, thread_sensitive=True)(
                        fn_name, args, request=request, session=session
                    )

                    if fn_name == 'escalate_to_operator':
                        yield {
                            'escalated': True,
                            'ticket_id': tool_res.get('ticket_id'),
                            'support_phone': tool_res.get('support_phone', '+380 44 123 45 67'),
                        }

                    if fn_name in ('add_to_cart', 'remove_from_cart', 'update_cart_quantity') and tool_res.get('success'):
                        yield {
                            'cart_mutation': True,
                            'cart_items_count': tool_res.get('cart_items_count', 0),
                            'total_amount': tool_res.get('total_amount', 0.0),
                            'action': tool_res.get('action'),
                            'dish_id': tool_res.get('dish_id'),
                        }

                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tc.id,
                        'name': fn_name,
                        'content': json.dumps(tool_res, ensure_ascii=False),
                    })

                stream = await self._call_with_retry(
                    client.chat.stream_async,
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
                async for chunk in stream:
                    delta = chunk.data.choices[0].delta
                    content = getattr(delta, 'content', None)
                    if content:
                        yield {'token': content}

            else:
                direct_content = getattr(response_message, 'content', None)
                if direct_content:
                    yield {'token': direct_content}
                else:
                    stream = await self._call_with_retry(
                        client.chat.stream_async,
                        model=self.model,
                        messages=messages,
                        temperature=self.temperature,
                        max_tokens=self.max_tokens,
                    )
                    async for chunk in stream:
                        delta = chunk.data.choices[0].delta
                        content = getattr(delta, 'content', None)
                        if content:
                            yield {'token': content}

    # ------------------------------------------------------------------
    # Gemini fallback backend
    # ------------------------------------------------------------------

    async def _stream_gemini(
        self,
        messages: list[dict[str, Any]],
        request: HttpRequest | None,
        session: ChatSession | None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        client = genai.Client(api_key=self.gemini_api_key)

        system_content = next(
            (msg['content'] for msg in messages if msg.get('role') == 'system'),
            SYSTEM_PROMPT,
        )

        # Build Gemini contents list (system instruction is separate)
        gemini_contents: list[genai_types.Content] = []
        for msg in messages:
            role = msg['role']
            if role == 'system':
                continue  # handled via system_instruction
            gemini_role = 'model' if role == 'assistant' else 'user'
            gemini_contents.append(genai_types.Content(
                role=gemini_role,
                parts=[genai_types.Part(text=msg['content'])],
            ))

        gemini_tools = _mistral_tools_to_gemini()

        config = genai_types.GenerateContentConfig(
            system_instruction=system_content,
            tools=gemini_tools,
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
            temperature=self.temperature,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        )

        response = await client.aio.models.generate_content(
            model=self.gemini_model,
            contents=gemini_contents,
            config=config,
        )

        if not getattr(response, 'candidates', None):
            return

        candidate = response.candidates[0]
        parts = candidate.content.parts if candidate.content and candidate.content.parts else []
        function_calls = [
            part for part in parts
            if getattr(part, 'function_call', None) is not None
        ]

        if function_calls:
            gemini_contents.append(candidate.content)

            function_responses: list[genai_types.Part] = []

            for fc_part in function_calls:
                fc = fc_part.function_call
                fn_name = fc.name
                args = dict(fc.args) if fc.args else {}

                tool_res = await sync_to_async(execute_agent_tool, thread_sensitive=True)(
                    fn_name, args, request=request, session=session
                )

                if fn_name == 'escalate_to_operator':
                    yield {
                        'escalated': True,
                        'ticket_id': tool_res.get('ticket_id'),
                        'support_phone': tool_res.get('support_phone', '+380 44 123 45 67'),
                    }

                if fn_name in ('add_to_cart', 'remove_from_cart', 'update_cart_quantity') and tool_res.get('success'):
                    yield {
                        'cart_mutation': True,
                        'cart_items_count': tool_res.get('cart_items_count', 0),
                        'total_amount': tool_res.get('total_amount', 0.0),
                        'action': tool_res.get('action'),
                        'dish_id': tool_res.get('dish_id'),
                    }

                function_responses.append(genai_types.Part(
                    function_response=genai_types.FunctionResponse(
                        name=fn_name,
                        response=tool_res,
                    )
                ))

            gemini_contents.append(genai_types.Content(
                role='user',
                parts=function_responses,
            ))

            final_config = genai_types.GenerateContentConfig(
                system_instruction=system_content,
                temperature=self.temperature,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            )
            stream = await client.aio.models.generate_content_stream(
                model=self.gemini_model,
                contents=gemini_contents,
                config=final_config,
            )
            async for chunk in stream:
                if chunk.text:
                    yield {'token': chunk.text}
        else:
            text = ''.join(getattr(part, 'text', '') or '' for part in parts)
            if text:
                yield {'token': text}

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
        """Call *func* with retry + exponential backoff on rate-limit errors."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                if self._is_rate_limit_error(exc) and attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        'Rate-limited by Mistral API (attempt %d/%d). '
                        'Retrying in %ds…',
                        attempt + 1,
                        MAX_RETRIES,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
