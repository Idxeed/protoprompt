from __future__ import annotations

import pytest

from protoprompt import MemoryScope, MemoryService
from protoprompt.profile.manager import ProfileManager
from protoprompt.profile.store import InMemoryProfileStore
from protoprompt.store.memory import InMemStore

from _mocks import MockLLM


def test_memory_service_requires_host_scope():
    with pytest.raises(ValueError, match="non-empty"):
        MemoryService(InMemStore(), MockLLM(), MemoryScope())


@pytest.mark.asyncio
async def test_memory_service_remember_search_explain_forget():
    service = MemoryService(
        InMemStore(),
        MockLLM(embed_dim=8),
        MemoryScope(tenant="acme", user="alice", thread="chat"),
    )
    stored = await service.remember("The contract renews in May", memory_id="renewal")
    assert stored == {"memory_id": "renewal", "stored": True}

    hits = await service.search("contract", top_k=3)
    assert hits[0]["memory_id"] == "renewal"
    assert hits[0]["text"] == "The contract renews in May"
    assert "text" not in service.explain()["results"][0]
    assert service.manifest()["confirmed_memory_ids"] == ["renewal"]

    assert await service.forget("renewal") == {
        "memory_id": "renewal",
        "forgotten": True,
    }
    assert await service.search("contract") == []


@pytest.mark.asyncio
async def test_memory_service_scope_prevents_cross_tenant_search_and_forget():
    store = InMemStore()
    llm = MockLLM(embed_dim=8)
    alice = MemoryService(store, llm, MemoryScope(tenant="acme", user="alice"))
    bob = MemoryService(store, llm, MemoryScope(tenant="acme", user="bob"))
    await alice.remember("alice private", memory_id="same")
    await bob.remember("bob private", memory_id="same")

    assert [item["text"] for item in await alice.search("private")] == ["alice private"]
    await alice.forget("same")
    assert await alice.search("private") == []
    assert [item["text"] for item in await bob.search("private")] == ["bob private"]


@pytest.mark.asyncio
async def test_memory_service_profile_is_host_user_pinned():
    scope = MemoryScope(tenant="acme", user="alice")
    manager = ProfileManager(InMemoryProfileStore(), scope=scope)
    service = MemoryService(
        InMemStore(),
        MockLLM(),
        scope,
        profile_manager=manager,
    )

    profile = await service.profile_update("Пожалуйста, отвечай по-русски")
    assert profile["user_id"] == "alice"
    assert (await service.current_profile())["user_id"] == "alice"


@pytest.mark.asyncio
async def test_memory_service_rejects_scope_metadata_override():
    service = MemoryService(
        InMemStore(), MockLLM(), MemoryScope(tenant="acme", user="alice")
    )
    with pytest.raises(ValueError, match="conflicts"):
        await service.remember(
            "private",
            metadata={"scope_tenant": "attacker"},
        )
