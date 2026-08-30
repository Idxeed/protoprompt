"""OpenAI-compatible REST client on httpx.

Speaks ``/chat/completions`` and ``/embeddings``, so it works against
OpenAI itself plus every compatible server: Ollama (``/v1``), vLLM,
LM Studio, LiteLLM, llama.cpp server. Only needs ``httpx``.
"""

from __future__ import annotations

from typing import Any


def _raise_for_response(response: Any) -> None:
    if response.status_code >= 400:
        snippet = response.text[:300]
        raise RuntimeError(
            f"LLM endpoint returned HTTP {response.status_code}: {snippet}"
        )


class HttpxLLMClient:
    """Thin async client for any OpenAI-compatible endpoint.

    Args:
        base_url: API root, without a trailing slash. For local Ollama
            use ``http://localhost:11434/v1``; LM Studio uses
            ``http://localhost:1234/v1``.
        api_key: bearer token; empty string skips the header entirely,
            which local servers expect.
        chat_model / embed_model: defaults used when callers omit ``model``.
        timeout: per-request timeout in seconds.
        transport: optional ``httpx.AsyncBaseTransport`` (e.g.
            ``httpx.MockTransport``) for tests.
        trust_env: whether to honour ambient proxy/CA environment settings;
            disabled by default so local prompts and bearer credentials are
            not silently rerouted.
        completion_token_field: wire name used for the output-token cap.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        api_key: str = "",
        timeout: float = 120.0,
        transport: Any | None = None,
        chat_model: str = "",
        embed_model: str = "",
        trust_env: bool = False,
        completion_token_field: str = "max_tokens",
    ) -> None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "HttpxLLMClient requires the 'httpx' package. "
                "Install with: pip install 'protoprompt[http]'"
            ) from exc
        if completion_token_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("unsupported completion token field")

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        kwargs: dict[str, Any] = {
            "base_url": base_url.rstrip("/"),
            "timeout": timeout,
            "headers": headers,
            "trust_env": trust_env,
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)
        self._chat_model = chat_model
        self._embed_model = embed_model
        self._completion_token_field = completion_token_field

    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        **options: object,
    ) -> str:
        body: dict[str, Any] = {
            "model": model or self._chat_model,
            "messages": messages,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body[self._completion_token_field] = max_tokens
        body.update(options)
        response = await self._client.post("/chat/completions", json=body)
        _raise_for_response(response)
        payload = response.json()
        return payload["choices"][0]["message"]["content"] or ""

    async def embed(
        self, texts: list[str], model: str = ""
    ) -> list[list[float]]:
        response = await self._client.post(
            "/embeddings", json={"model": model or self._embed_model, "input": texts}
        )
        _raise_for_response(response)
        data = response.json()["data"]
        data = sorted(data, key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]

    async def aclose(self) -> None:
        await self._client.aclose()
