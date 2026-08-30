"""Native Ollama client (``/api/chat``, ``/api/embed``).

Uses Ollama's own JSON API rather than the OpenAI-compatible shim, so
options like ``num_predict`` map cleanly. Requires ``httpx`` only — no
``ollama`` SDK.
"""

from __future__ import annotations

import json
import inspect
from typing import Any

from protoprompt.integrations.httpx_client import _raise_for_response


class OllamaClient:
    """Async client for a local (or remote) Ollama server.

    Args:
        host: server root, no trailing slash and no ``/v1`` suffix.
        chat_model: default model for :meth:`chat` when none is passed.
        embed_model: default model for :meth:`embed`; matches the
            project-wide default ``nomic-embed-text``.
        trust_env: whether to honour ambient proxy/CA environment settings;
            disabled by default so local prompt and document data are not
            silently rerouted.
    """

    def __init__(
        self,
        host: str = "http://localhost:11434",
        chat_model: str = "llama3.1",
        embed_model: str = "nomic-embed-text",
        timeout: float = 300.0,
        transport: Any | None = None,
        trust_env: bool = False,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "OllamaClient requires the 'httpx' package. "
                "Install with: pip install 'protoprompt[ollama]'"
            ) from exc

        self._chat_model = chat_model
        self._embed_model = embed_model
        kwargs: dict[str, Any] = {
            "base_url": host.rstrip("/"),
            "timeout": timeout,
            "headers": {"Content-Type": "application/json"},
            "trust_env": trust_env,
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
        ollama_options: dict[str, Any] = {}
        if temperature is not None:
            ollama_options["temperature"] = temperature
        if max_tokens is not None:
            ollama_options["num_predict"] = max_tokens
        for key, value in options.items():
            ollama_options[key] = value

        body: dict[str, Any] = {
            "model": model or self._chat_model,
            "messages": messages,
            "stream": False,
        }
        if ollama_options:
            body["options"] = ollama_options

        response = await self._client.post("/api/chat", json=body)
        _raise_for_response(response)
        payload = response.json()
        return payload.get("message", {}).get("content", "") or ""

    async def chat_stream(
        self,
        messages: list[dict],
        model: str = "",
        *,
        on_token=None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **options: object,
    ) -> str:
        """Потоковый чат: каждый токен отдаётся в ``on_token`` (если задан)."""
        ollama_options: dict[str, Any] = {}
        if temperature is not None:
            ollama_options["temperature"] = temperature
        if max_tokens is not None:
            ollama_options["num_predict"] = max_tokens
        for key, value in options.items():
            ollama_options[key] = value

        body: dict[str, Any] = {
            "model": model or self._chat_model,
            "messages": messages,
            "stream": True,
        }
        if ollama_options:
            body["options"] = ollama_options

        parts: list[str] = []
        async with self._client.stream("POST", "/api/chat", json=body) as response:
            _raise_for_response(response)
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = chunk.get("message", {}).get("content", "")
                if content:
                    if on_token is not None:
                        callback_result = on_token(content)
                        if inspect.isawaitable(callback_result):
                            await callback_result
                    parts.append(content)
                if chunk.get("done"):
                    break
        return "".join(parts)

    async def embed(
        self, texts: list[str], model: str = ""
    ) -> list[list[float]]:
        response = await self._client.post("/api/embed", json={
            "model": model or self._embed_model,
            "input": texts,
        })
        _raise_for_response(response)
        return [list(map(float, vec)) for vec in response.json()["embeddings"]]

    async def aclose(self) -> None:
        await self._client.aclose()
