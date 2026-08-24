"""Embedding cache and a caching decorator for LLM clients.

Repeated context builds re-embed the same query text over and over.
``CachedLLMClient`` intercepts ``embed()`` and serves repeats from an
``EmbeddingCache``; ``chat()`` passes through untouched.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Protocol

from protoprompt.llm import LLMClientProtocol


class EmbeddingCache(Protocol):
    """Minimal key/value contract; keys are opaque strings."""

    def get(self, key: str) -> list[list[float]] | None:
        ...

    def put(self, key: str, vectors: list[list[float]]) -> None:
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
        cache: EmbeddingCache | None = None,
    ) -> None:
        self._inner = inner
        self._cache: EmbeddingCache = cache or InMemoryEmbeddingCache()

    @property
    def cache(self) -> EmbeddingCache:
        return self._cache

    @property
    def inner(self) -> LLMClientProtocol:
        return self._inner

    async def chat(
        self, messages: list[dict], model: str = "", **options: object
    ) -> str:
        return await self._inner.chat(messages, model=model, **options)

    async def embed(
        self, texts: list[str], model: str = ""
    ) -> list[list[float]]:
        result: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        for i, text in enumerate(texts):
            hit = self._cache.get(cache_key(model, text))
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
                self._cache.put(cache_key(model, texts[i]), [vector])

        return [v for v in result if v is not None]
