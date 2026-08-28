"""Native Anthropic Messages API adapter."""

from __future__ import annotations

from typing import Any

from protoprompt.integrations._messages import response_text, split_system_messages


class AnthropicClient:
    """Async Claude chat client using the official Anthropic SDK.

    System/developer messages are moved to Anthropic's top-level ``system``
    field. Text, tool, image, and document content blocks in user/assistant
    messages are otherwise forwarded unchanged.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ImportError(
                    "AnthropicClient requires the 'anthropic' package. "
                    "Install with: pip install 'protoprompt[anthropic]'"
                ) from exc
            kwargs: dict[str, Any] = {}
            if api_key is not None:
                kwargs["api_key"] = api_key
            if base_url is not None:
                kwargs["base_url"] = base_url
            if timeout is not None:
                kwargs["timeout"] = timeout
            if max_retries is not None:
                kwargs["max_retries"] = max_retries
            client = anthropic.AsyncAnthropic(**kwargs)
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    def _request(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int | None,
        options: dict[str, object],
    ) -> dict[str, Any]:
        system, turns = split_system_messages(messages)
        request: dict[str, Any] = {
            "model": model or self._model,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "messages": turns,
        }
        if system:
            request["system"] = "\n\n".join(system)
        request.update(options)
        return request

    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        **options: object,
    ) -> str:
        request = self._request(messages, model, max_tokens, dict(options))
        if temperature is not None:
            # New Anthropic SDK generations removed sampling kwargs for current
            # models but retain raw API passthrough for older model families.
            extra_body = dict(request.pop("extra_body", {}) or {})
            extra_body["temperature"] = temperature
            request["extra_body"] = extra_body
        response = await self._client.messages.create(**request)
        return response_text(response.content)

    async def count_tokens(
        self,
        messages: list[dict],
        model: str = "",
        **options: object,
    ) -> int:
        request = self._request(messages, model, self._max_tokens, dict(options))
        request.pop("max_tokens", None)
        response = await self._client.messages.count_tokens(**request)
        return int(response.input_tokens)

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
