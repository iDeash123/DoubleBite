"""Minimal async Gemini client (OpenAI-compatible endpoint) for streaming chat."""

import json
from types import SimpleNamespace
from typing import Any

import httpx

GEMINI_OPENAI_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta/openai'


class GeminiAPIError(Exception):
    def __init__(self, status_code: int, body: str):
        super().__init__(f'Gemini API error {status_code}: {body[:500]}')
        self.status_code = status_code
        self.body = body


def _to_ns(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _to_ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_ns(v) for v in value]
    return value


class _GeminiStream:
    def __init__(self, response: httpx.Response):
        self._response = response

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        async for line in self._response.aiter_lines():
            line = line.strip()
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            if payload == '[DONE]':
                break
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                continue
            yield SimpleNamespace(data=_to_ns(parsed))

    async def close(self) -> None:
        await self._response.aclose()


class _GeminiChat:
    def __init__(self, client: httpx.AsyncClient, api_key: str):
        self._client = client
        self._api_key = api_key

    async def stream_async(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_ms: int | None = None,
        reasoning_effort: str | None = None,
    ) -> _GeminiStream:
        body: dict[str, Any] = {
            'model': model,
            'messages': messages,
            'stream': True,
            'stream_options': {'include_usage': True},
        }
        if tools:
            body['tools'] = tools
            body['tool_choice'] = 'auto'
        if reasoning_effort:
            body['reasoning_effort'] = reasoning_effort
        if temperature is not None:
            body['temperature'] = temperature
        if max_tokens is not None:
            body['max_tokens'] = max_tokens

        req_kwargs: dict[str, Any] = {
            'json': body,
            'headers': {'Authorization': f'Bearer {self._api_key}'},
        }
        if timeout_ms is not None and timeout_ms > 0:
            req_kwargs['timeout'] = timeout_ms / 1000
        request = self._client.build_request(
            'POST',
            f'{GEMINI_OPENAI_BASE_URL}/chat/completions',
            **req_kwargs,
        )
        response = await self._client.send(request, stream=True)
        if response.status_code >= 400:
            try:
                text = (await response.aread()).decode('utf-8', 'replace')
            finally:
                await response.aclose()
            raise GeminiAPIError(response.status_code, text)
        return _GeminiStream(response)


class GeminiClient:
    def __init__(self, api_key: str, timeout_ms: int = 30000):
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=timeout_ms / 1000)
        self.chat = _GeminiChat(self._http, api_key)

    async def __aenter__(self) -> 'GeminiClient':
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._http.aclose()
