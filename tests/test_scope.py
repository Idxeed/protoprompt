from __future__ import annotations

import pytest

from protoprompt import ContextBuilder, ContextInput, MemoryScope
from protoprompt.agent import WorkingMemory
from protoprompt.rag import DocumentIndexer, Retriever
from protoprompt.scope import (
    LOGICAL_DOC_ID_KEY,
    SCOPE_KIND_KEY,
    SCOPE_TENANT_KEY,
    SCOPE_THREAD_KEY,
    SCOPE_USER_KEY,
    scoped_doc_id,
)
from protoprompt.store.memory import InMemStore
from protoprompt.store.sqlite import SqliteStore

from _mocks import MockLLM


def test_scope_metadata_roundtrip_and_empty_legacy_id():
    scope = MemoryScope(tenant="acme", user="u1", thread="t1", kind="document")

    assert scope.to_metadata() == {
        SCOPE_TENANT_KEY: "acme",
        SCOPE_USER_KEY: "u1",
        SCOPE_THREAD_KEY: "t1",
        SCOPE_KIND_KEY: "document",
    }
    assert MemoryScope.from_metadata(scope.to_metadata()) == scope
    assert scoped_doc_id("doc", MemoryScope()) == "doc"
    assert scoped_doc_id("doc", None) == "doc"
    assert scoped_doc_id("doc", scope) == scoped_doc_id("doc", scope)
    assert scoped_doc_id("doc", scope) != scoped_doc_id(
        "doc", MemoryScope(tenant="other", user="u1", thread="t1", kind="document")
    )


def test_scope_rejects_non_string_fields_and_conflicting_metadata():
    with pytest.raises(TypeError, match="tenant"):
        MemoryScope(tenant=123)  # type: ignore[arg-type]

    scope = MemoryScope(tenant="acme")
    with pytest.raises(ValueError, match=SCOPE_TENANT_KEY):
        scope.merge_metadata({SCOPE_TENANT_KEY: "other"})
    with pytest.raises(ValueError, match=SCOPE_TENANT_KEY):
        scope.merge_where({SCOPE_TENANT_KEY: {"$in": ["acme", "other"]}})


@pytest.mark.asyncio
@pytest.mark.parametrize("store_factory", [InMemStore, SqliteStore])
async def test_document_scope_isolates_same_logical_id_and_deletion(store_factory):
    store = store_factory()
    llm = MockLLM(embed_dim=8)
    alice = MemoryScope(tenant="acme", user="alice", kind="document")
    bob = MemoryScope(tenant="acme", user="bob", kind="document")

    await DocumentIndexer(store, llm, scope=alice).index("contract", "alice terms")
    await DocumentIndexer(store, llm, scope=bob).index("contract", "bob terms")

    alice_hits = await Retriever(store, llm, scope=alice).retrieve(
        "terms", top_k=10, doc_ids=["contract"]
    )
    bob_hits = await Retriever(store, llm, scope=bob).retrieve(
        "terms", top_k=10, doc_ids=["contract"]
    )

    assert [hit.text for hit in alice_hits] == ["alice terms"]
    assert [hit.text for hit in bob_hits] == ["bob terms"]
    assert alice_hits[0].doc_id == "contract"
    assert alice_hits[0].metadata["doc_id"] == "contract"
    assert alice_hits[0].metadata[LOGICAL_DOC_ID_KEY] == "contract"

    store.delete(scoped_doc_id("contract", alice))
    assert await Retriever(store, llm, scope=alice).retrieve(
        "terms", doc_ids=["contract"]
    ) == []
    assert await Retriever(store, llm, scope=bob).retrieve(
        "terms", doc_ids=["contract"]
    )

    close = getattr(store, "close", None)
    if close is not None:
        close()


@pytest.mark.asyncio
async def test_context_builder_uses_pinned_scope_for_rag_and_session():
    store = InMemStore()
    llm = MockLLM(embed_dim=8)
    alice = MemoryScope(tenant="acme", user="alice")
    bob = MemoryScope(tenant="acme", user="bob")

    for scope, owner in ((alice, "alice"), (bob, "bob")):
        await DocumentIndexer(store, llm, scope=scope).index("kb", f"{owner} document")
        logical_session_id = "session_shared"
        store.add(
            scoped_doc_id(logical_session_id, scope),
            [f"{owner} session"],
            [[1.0] * 8],
            scope.merge_metadata(
                {"kind": "session", LOGICAL_DOC_ID_KEY: logical_session_id}
            ),
        )

    out = await ContextBuilder(store, llm, scope=alice).build(
        ContextInput(query="owner", chat_id="shared", doc_ids=["kb"])
    )

    assert out.rag_blocks == ["alice document"]
    assert out.session_blocks == ["alice session"]
    assert "bob" not in out.system_prompt


def test_context_builder_rejects_conflicting_retriever_scope():
    store = InMemStore()
    llm = MockLLM()
    retriever = Retriever(store, llm, scope=MemoryScope(tenant="one"))

    with pytest.raises(ValueError, match="conflicts"):
        ContextBuilder(
            store,
            llm,
            retriever=retriever,
            scope=MemoryScope(tenant="two"),
        )


@pytest.mark.asyncio
async def test_working_memory_cold_zone_is_scope_isolated():
    store = InMemStore()
    alice = MemoryScope(tenant="acme", user="alice", kind="agent")
    bob = MemoryScope(tenant="acme", user="bob", kind="agent")
    alice_memory = WorkingMemory(store=store, max_tokens=100, scope=alice)
    bob_memory = WorkingMemory(store=store, max_tokens=100, scope=bob)

    alice_id = await alice_memory.add("file", "def alice_contract(): pass")
    bob_id = await bob_memory.add("file", "def bob_contract(): pass")
    assert await alice_memory.forget(alice_id)
    assert await bob_memory.forget(bob_id)

    alice_hits = store.query([0.0], top_k=10, where=alice.merge_where())
    bob_hits = store.query([0.0], top_k=10, where=bob.merge_where())
    assert [hit["document"] for hit in alice_hits] == ["def alice_contract(): pass"]
    assert [hit["document"] for hit in bob_hits] == ["def bob_contract(): pass"]

    restored = await alice_memory.recall("alice_contract")
    assert restored
    assert all("bob_contract" not in item.text for item in alice_memory.items.values())
