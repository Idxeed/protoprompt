from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from protoprompt import CachedLLMClient, MemoryScope, UserProfile
from protoprompt.integrations.redis import (
    RedisEmbeddingCache,
    RedisProfileStore,
    RedisSession,
)

from _mocks import MockLLM


@pytest.fixture
def client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_redis_embedding_cache_ttl_and_async_cached_client(client):
    cache = RedisEmbeddingCache(
        client=client,
        ttl_seconds=60,
        scope=MemoryScope(tenant="acme", user="alice"),
    )
    inner = MockLLM(embed_dim=4)
    cached = CachedLLMClient(inner, cache)

    first = await cached.embed(["same text"], model="embedding-model")
    second = await cached.embed(["same text"], model="embedding-model")

    assert first == second
    assert len(inner.embed_calls) == 1
    keys = await client.keys("protoprompt:embedding:*")
    assert len(keys) == 1
    assert "same text" not in keys[0]
    assert await client.ttl(keys[0]) > 0
    await client.delete(keys[0])
    assert await cache.get("missing") is None


@pytest.mark.asyncio
async def test_redis_session_matches_tail_pop_clear_and_ttl(client):
    session = RedisSession(
        "thread-1",
        scope=MemoryScope(tenant="acme", user="alice", thread="thread-1"),
        client=client,
        ttl_seconds=60,
    )
    items = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    await session.add_items(items)
    assert await session.get_items(limit=2) == items[-2:]
    assert await session.get_items(limit=0) == []
    assert await session.pop_item() == items[-1]
    assert await session.get_items() == items[:-1]
    assert await client.ttl(session._key) > 0
    await session.clear_session()
    assert await session.get_items() == []


@pytest.mark.asyncio
async def test_redis_profile_tenant_isolation_and_concurrent_cas(client):
    acme = RedisProfileStore(client=client, tenant="acme", ttl_seconds=60)
    other = RedisProfileStore(client=client, tenant="other", ttl_seconds=60)
    initial = UserProfile(user_id="alice", version=0)
    assert await acme.compare_and_put(initial, expected_version=None)
    assert await other.get("alice") is None

    candidates = [
        UserProfile(user_id="alice", facts={"winner": str(index)}, version=1)
        for index in range(20)
    ]
    outcomes = await asyncio.gather(*[
        acme.compare_and_put(profile, expected_version=0)
        for profile in candidates
    ])
    assert outcomes.count(True) == 1
    winner = await acme.get("alice")
    assert winner is not None
    assert winner.version == 1
    assert await client.ttl(acme._key("alice")) > 0


@pytest.mark.asyncio
async def test_corrupt_cache_entry_fails_closed_and_is_removed(client):
    cache = RedisEmbeddingCache(client=client)
    redis_key = cache._key("corrupt")
    await client.set(redis_key, "not-json")
    assert await cache.get("corrupt") is None
    assert await client.exists(redis_key) == 0


def test_redis_adapters_validate_host_policy(client):
    with pytest.raises(ValueError, match="non-empty MemoryScope"):
        RedisSession("s", scope=MemoryScope(), client=client)
    with pytest.raises(ValueError, match="ttl_seconds"):
        RedisEmbeddingCache(client=client, ttl_seconds=0)
    with pytest.raises(ValueError, match="tenant"):
        RedisProfileStore(client=client, tenant="")
