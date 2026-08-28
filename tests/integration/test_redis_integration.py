from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from protoprompt import CachedLLMClient, MemoryScope, UserProfile
from protoprompt.integrations.redis import (
    RedisEmbeddingCache,
    RedisProfileStore,
    RedisSession,
)

from _mocks import MockLLM

pytestmark = pytest.mark.integration


@pytest.fixture
def redis_url() -> str:
    value = os.environ.get("PROTOPROMPT_REDIS_URL")
    if not value:
        pytest.skip("set PROTOPROMPT_REDIS_URL to run Redis tests")
    return value


@pytest.mark.asyncio
async def test_cache_reconnect_and_ttl(redis_url: str):
    scope = MemoryScope(tenant="integration", user=uuid.uuid4().hex)
    cache = RedisEmbeddingCache(redis_url, ttl_seconds=30, scope=scope)
    cached = CachedLLMClient(MockLLM(embed_dim=4), cache)
    await cached.embed(["persistent cache entry"], model="m")
    await cache.client.connection_pool.disconnect()
    assert await cache.ping()
    assert await cached.embed(["persistent cache entry"], model="m")
    await cache.close()


@pytest.mark.asyncio
async def test_session_and_profile_concurrency(redis_url: str):
    identity = uuid.uuid4().hex
    scope = MemoryScope(tenant="integration", user=identity, thread="thread")
    session = RedisSession("thread", scope=scope, url=redis_url, ttl_seconds=30)
    await session.add_items([{"role": "user", "content": "hello"}])
    assert (await session.get_items())[0]["content"] == "hello"

    profiles = RedisProfileStore(
        client=session.client,
        tenant="integration-" + identity,
        ttl_seconds=30,
    )
    await profiles.put(UserProfile(user_id="alice", version=0))
    outcomes = await asyncio.gather(*[
        profiles.compare_and_put(
            UserProfile(user_id="alice", facts={"n": str(index)}, version=1),
            expected_version=0,
        )
        for index in range(10)
    ])
    assert outcomes.count(True) == 1
    await session.clear_session()
    await profiles.delete("alice")
    await session.close()
