"""Embedding cache and a caching decorator for LLM clients.

Repeated context builds re-embed the same query text over and over.
``CachedLLMClient`` intercepts ``embed()`` and serves repeats from an
``EmbeddingCache``; ``chat()`` passes through untouched.
"""

from __future__ import annotations

import hashlib
import inspect
from collections import OrderedDict
from typing import Any, Protocol, runtime_checkable

from protoprompt.events import CacheEvent, EventDispatcher, EventSink, dispatch, new_trace_id, scope_id
from protoprompt.llm import LLMClientProtocol
from protoprompt.scope import MemoryScope


@runtime_checkable
class EmbeddingCache(Protocol):
    """Minimal key/value contract; keys are opaque strings."""

    def get(self, key: str) -> list[list[float]] | None:
        ...

    def put(self, key: str, vectors: list[list[float]]) -> None:
        ...


@runtime_checkable
class AsyncEmbeddingCache(Protocol):
    """Non-blocking embedding cache accepted by ``CachedLLMClient``."""

    async def get(self, key: str) -> list[list[float]] | None:
        ...

    async def put(self, key: str, vectors: list[list[float]]) -> None:
        ...


class InMemoryEmbeddingCache:
    """LRU cache with a bounded number of entries (default 2048)."""

    def __init__(self, capacity: int = 2048) -> None:
        self._capacity = max(1, capacity)
        self._data: OrderedDict[str, list[list[float]]] = OrderedDict()

    def get(self, key: str) -> list[list[float]] | None:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return None

    def put(self, key: str, vectors: list[list[float]]) -> None:
        self._data[key] = vectors
        self._data.move_to_end(key)
        while len(self._data) > self._capacity:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


def cache_key(model: str, text: str) -> str:
    """Collision-free cache key: model namespace + content digest."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{model}\x00{digest}"


class CachedLLMClient:
    """Decorate any ``LLMClientProtocol`` with embedding reuse.

    Vectors are cached per single text, so partial hits still batch the
    misses into one upstream call while preserving input order.
    """

    def __init__(
        self,
        inner: LLMClientProtocol,
        cache: EmbeddingCache | AsyncEmbeddingCache | None = None,
        *,
        scope: MemoryScope | None = None,
        event_sink: EventSink | EventDispatcher | None = None,
    ) -> None:
        self._inner = inner
        self._cache: EmbeddingCache | AsyncEmbeddingCache = (
            cache or InMemoryEmbeddingCache()
        )
        self._scope = scope
        self._event_sink = event_sink

    @property
    def cache(self) -> EmbeddingCache | AsyncEmbeddingCache:
        return self._cache

    @property
    def inner(self) -> LLMClientProtocol:
        return self._inner

    async def chat(
        self, messages: list[dict], model: str = "", **options: object
    ) -> str:
        return await self._inner.chat(messages, model=model, **options)

    async def chat_stream(
        self,
        messages: list[dict],
        model: str = "",
        *,
        on_token=None,
        **options: object,
    ) -> str:
        """Прокси для потокового чата; требует поддержки у inner-клиента."""
        method = getattr(self._inner, "chat_stream", None)
        if method is None:
            raise AttributeError(
                "underlying client does not support chat_stream"
            )
        return await method(
            messages, model=model, on_token=on_token, **options
        )

    async def embed(
        self, texts: list[str], model: str = ""
    ) -> list[list[float]]:
        result: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        for i, text in enumerate(texts):
            hit = await _await_cache(self._cache.get(cache_key(model, text)))
            if hit is not None:
                result[i] = hit[0]
            else:
                missing.append(i)

        if missing:
            vectors = await self._inner.embed(
                [texts[i] for i in missing], model=model
            )
            for i, vector in zip(missing, vectors):
                result[i] = vector
                await _await_cache(
                    self._cache.put(cache_key(model, texts[i]), [vector])
                )

        dispatch(self._event_sink, CacheEvent(
            action="lookup",
            trace_id=new_trace_id(),
            scope_id=scope_id(self._scope),
            attributes={
                "request_count": len(texts),
                "hit_count": len(texts) - len(missing),
                "miss_count": len(missing),
                "model_selected": bool(model),
            },
        ))

        return [v for v in result if v is not None]


async def _await_cache(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
