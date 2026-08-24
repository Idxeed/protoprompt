"""Official OpenAI SDK adapter.

Wraps ``openai.AsyncOpenAI`` behind :class:`protoprompt.LLMClientProtocol`.
Also works with OpenAI-compatible gateways (LiteLLM, vLLM) via
``base_url``.
"""

from __future__ import annotations

from typing import Any


class OpenAIClient:
    """Async client backed by the official SDK.

    Args:
        api_key: defaults to the ``OPENAI_API_KEY`` env var (SDK behaviour).
        base_url: override for gateways and compatible servers.
        chat_model / embed_model: fallbacks used when the caller does not
            pass an explicit ``model``.
        http_client: optional preconfigured ``httpx.AsyncClient``
            (useful for tests via a mock transport).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        organization: str | None = None,
        chat_model: str = "gpt-4o-mini",
        embed_model: str = "text-embedding-3-small",
        timeout: float | None = None,
        http_client: Any | None = None,
    ) -> None:
        try:
            import openai
        except ImportError as exc:
            raise ImportError(
                "OpenAIClient requires the 'openai' package. "
                "Install with: pip install 'protoprompt[openai]'"
            ) from exc

        self._chat_model = chat_model
        self._embed_model = embed_model
        kwargs: dict[str, Any] = {}
        if api_key is not None:
            kwargs["api_key"] = api_key
        if base_url is not None:
            kwargs["base_url"] = base_url
        if organization is not None:
            kwargs["organization"] = organization
        if timeout is not None:
            kwargs["timeout"] = timeout
        if http_client is not None:
            kwargs["http_client"] = http_client
        self._client = openai.AsyncOpenAI(**kwargs)

    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        **options: object,
    ) -> str:
        kwargs: dict[str, Any] = dict(options)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_completion_tokens"] = max_tokens
        response = await self._client.chat.completions.create(
            model=model or self._chat_model,
            messages=messages,
            **kwargs,
        )
        return response.choices[0].message.content or ""

    async def embed(
        self, texts: list[str], model: str = ""
    ) -> list[list[float]]:
        response = await self._client.embeddings.create(
            model=model or self._embed_model,
            input=texts,
        )
        ordered = sorted(response.data, key=lambda d: d.index)
        return [list(d.embedding) for d in ordered]
