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

logger = logging.getLogger('support')


class MistralSupportAgent:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
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

    async def stream_chat_response(
        self,
        chat_history: list[dict[str, Any]],
        request: HttpRequest | None = None,
        session: ChatSession | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        if not self.api_key:
            logger.warning('MISTRAL_API_KEY is not configured.')
            yield {
                'token': (
                    'Вибачте, асистент тимчасово недоступний (відсутній ключ доступу). '
                    'Будь ласка, зверніться за телефоном: +380 44 123 45 67.'
                )
            }
            return

        messages: list[dict[str, Any]] = [
            {'role': 'system', 'content': SYSTEM_PROMPT}
        ]
        for msg in chat_history:
            role = msg.get('role', 'user')
            content = msg.get('content', '')
            messages.append({'role': role, 'content': content})

        try:
            async with Mistral(api_key=self.api_key) as client:
                response = await client.chat.complete_async(
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

                        if fn_name in ('add_to_cart', 'remove_from_cart') and tool_res.get('success'):
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

                    stream = await client.chat.stream_async(
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
                        stream = await client.chat.stream_async(
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

        except Exception:
            logger.exception('Mistral API error')
            yield {
                'error': (
                    'Вибачте, асистент тимчасово недоступний. Будь ласка, спробуйте через '
                    'хвилину або зверніться за телефоном: +380 44 123 45 67.'
                )
            }
