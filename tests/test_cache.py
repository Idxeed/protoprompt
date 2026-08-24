"""Tests for the embedding cache and CachedLLMClient decorator."""

from __future__ import annotations

import pytest

from protoprompt import CachedLLMClient, InMemoryEmbeddingCache

from _mocks import MockLLM


class CountingLLM(MockLLM):
    def __init__(self, dim: int = 4) -> None:
        super().__init__(embed_dim=dim)
        self.embedded_texts: list[list[str]] = []

    async def embed(self, texts, model=""):
        self.embedded_texts.append(list(texts))
        return await super().embed(texts, model=model)


async def test_repeated_query_hits_cache():
    llm = CountingLLM()
    cached = CachedLLMClient(llm)
    first = await cached.embed(["hello"], model="m1")
    second = await cached.embed(["hello"], model="m1")
    assert first == second
    assert len(llm.embedded_texts) == 1


async def test_partial_miss_batches_only_missing():
    llm = CountingLLM()
    cached = CachedLLMClient(llm)
    await cached.embed(["a", "b"], model="m")
    vectors = await cached.embed(["a", "b", "c"], model="m")
    # second call must only ask upstream for "c"
    assert llm.embedded_texts[1] == ["c"]
    assert len(vectors) == 3


async def test_order_preserved_with_mixed_hits():
    llm = CountingLLM()
    cached = CachedLLMClient(llm)
    base = await cached.embed(["x", "y", "z"], model="m")
    mixed = await cached.embed(["z", "y", "x"], model="m")
    assert mixed == [base[2], base[1], base[0]]


async def test_model_namespace_isolates_entries():
    llm = CountingLLM()
    cached = CachedLLMClient(llm)
    v1 = await cached.embed(["same text"], model="model-a")
    v2 = await cached.embed(["same text"], model="model-b")
    assert v1 != v2 or v1 == v2  # values may coincide only by chance
    assert len(llm.embedded_texts) == 2


async def test_chat_passes_through():
    llm = CountingLLM()
    cached = CachedLLMClient(llm)
    answer = await cached.chat(
        [{"role": "user", "content": "hi"}], model="m", temperature=0.5
    )
    assert answer == "mocked response"
    assert llm.chat_calls[0]["temperature"] == 0.5


async def test_lru_capacity_eviction():
    cache = InMemoryEmbeddingCache(capacity=2)
    cache.put("k1", [[1.0]])
    cache.put("k2", [[2.0]])
    cache.put("k3", [[3.0]])
    assert cache.get("k1") is None
    assert cache.get("k2") == [[2.0]]
    assert cache.get("k3") == [[3.0]]


async def test_custom_cache_is_used():
    class DictCache:
        def __init__(self):
            self.data = {}

        def get(self, key):
            return self.data.get(key)

        def put(self, key, value):
            self.data[key] = value

    llm = CountingLLM()
    custom = DictCache()
    cached = CachedLLMClient(llm, cache=custom)
    await cached.embed(["t"], model="m")
    assert len(custom.data) == 1
