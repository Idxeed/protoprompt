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
        timeout: per-request timeout in seconds.
        transport: optional ``httpx.AsyncBaseTransport`` (e.g.
            ``httpx.MockTransport``) for tests.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        api_key: str = "",
        timeout: float = 120.0,
        transport: Any | None = None,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "HttpxLLMClient requires the 'httpx' package. "
                "Install with: pip install 'protoprompt[http]'"
            ) from exc

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        kwargs: dict[str, Any] = {
            "base_url": base_url.rstrip("/"),
            "timeout": timeout,
            "headers": headers,
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        **options: object,
    ) -> str:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        body.update(options)
        response = await self._client.post("/chat/completions", json=body)
        _raise_for_response(response)
        payload = response.json()
        return payload["choices"][0]["message"]["content"] or ""

    async def embed(
        self, texts: list[str], model: str = ""
    ) -> list[list[float]]:
        response = await self._client.post(
            "/embeddings", json={"model": model, "input": texts}
        )
        _raise_for_response(response)
        data = response.json()["data"]
        data = sorted(data, key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]

    async def aclose(self) -> None:
        await self._client.aclose()
