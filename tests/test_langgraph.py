from __future__ import annotations

from typing import TypedDict

import pytest

from protoprompt import ContextBuilder, InMemStore, MemoryScope
from protoprompt.integrations.langgraph import (
    ProtoPromptStoreAdapter,
    create_build_context_node,
    create_sync_build_context_node,
)
from protoprompt.rag import DocumentIndexer

from _mocks import MockLLM

langgraph = pytest.importorskip("langgraph")
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.store.base import GetOp, PutOp  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402


class ContextState(TypedDict, total=False):
    query: str
    context: str
    context_provenance: dict


def test_store_adapter_isolates_sync_namespaces_and_bulk_operations():
    inner = InMemoryStore()
    alice = ProtoPromptStoreAdapter(
        inner,
        scope=MemoryScope(tenant="acme", user="alice"),
    )
    bob = ProtoPromptStoreAdapter(
        inner,
        scope=MemoryScope(tenant="acme", user="bob"),
    )

    alice.batch([
        PutOp(("memories", "legal"), "contract", {"renewal": "May"}),
    ])
    results = alice.batch([
        GetOp(("memories", "legal"), "contract"),
    ])
    assert results[0].value == {"renewal": "May"}
    assert results[0].namespace == ("memories", "legal")

    bob.put(("memories", "private"), "contract", {"renewal": "June"})
    assert alice.get(("memories", "legal"), "contract").value["renewal"] == "May"
    assert alice.list_namespaces(prefix=("memories",)) == [("memories", "legal")]
    assert alice.list_namespaces(suffix=("legal",)) == [("memories", "legal")]
    assert bob.list_namespaces() == [("memories", "private")]
    assert bob.get(("memories", "legal"), "contract") is None


@pytest.mark.asyncio
async def test_store_adapter_isolates_async_api():
    inner = InMemoryStore()
    first = ProtoPromptStoreAdapter(inner, scope=MemoryScope(tenant="one"))
    second = ProtoPromptStoreAdapter(inner, scope=MemoryScope(tenant="two"))

    await first.aput(("memory",), "same-key", {"owner": "one"})
    await second.aput(("memory",), "same-key", {"owner": "two"})

    assert (await first.aget(("memory",), "same-key")).value["owner"] == "one"
    assert (await second.aget(("memory",), "same-key")).value["owner"] == "two"
    hits = await first.asearch(("memory",), filter={"owner": "one"})
    assert [item.value for item in hits] == [{"owner": "one"}]
    await first.adelete(("memory",), "same-key")
    assert await first.aget(("memory",), "same-key") is None
    assert await second.aget(("memory",), "same-key") is not None


def _graph(node):
    graph = StateGraph(ContextState)
    graph.add_node("build_context", node)
    graph.add_edge(START, "build_context")
    graph.add_edge("build_context", END)
    return graph


def test_sync_build_context_node_runs_in_real_graph():
    store = InMemStore()
    llm = MockLLM(embed_dim=8)
    scope = MemoryScope(tenant="acme", user="alice", thread="thread-1")
    import asyncio

    asyncio.run(DocumentIndexer(store, llm, scope=scope).index(
        "contract",
        "The contract renews in May.",
    ))
    builder = ContextBuilder(store, llm, scope=scope)
    node = create_sync_build_context_node(
        builder,
        chat_id="thread-1",
        system_prompt="Answer from the retained contract.",
    )
    graph_store = ProtoPromptStoreAdapter(
        InMemoryStore(),
        scope=scope,
    )
    app = _graph(node).compile(store=graph_store)

    result = app.invoke({"query": "When does it renew?"})

    assert "renews in May" in result["context"]
    assert result["context_provenance"]["rag_block_count"] == 1
    assert result["context_provenance"]["rag"][0]["doc_id"] == "contract"


@pytest.mark.asyncio
async def test_async_build_context_node_reads_latest_message_in_real_graph():
    class MessageState(TypedDict, total=False):
        messages: list[dict]
        context: str
        context_provenance: dict

    store = InMemStore()
    llm = MockLLM(embed_dim=8)
    scope = MemoryScope(tenant="acme", user="alice", thread="thread-2")
    await DocumentIndexer(store, llm, scope=scope).index(
        "contract",
        "The support window ends in September.",
    )
    builder = ContextBuilder(store, llm, scope=scope)
    node = create_build_context_node(builder, chat_id="thread-2")
    graph = StateGraph(MessageState)
    graph.add_node("build_context", node)
    graph.add_edge(START, "build_context")
    graph.add_edge("build_context", END)
    app = graph.compile(
        store=ProtoPromptStoreAdapter(InMemoryStore(), scope=scope)
    )

    result = await app.ainvoke({
        "messages": [{"role": "user", "content": "When does support end?"}],
    })

    assert "ends in September" in result["context"]
    assert result["context_provenance"]["profile_used"] is False
    assert result["messages"] == [
        {"role": "user", "content": "When does support end?"}
    ]


def test_store_adapter_requires_nonempty_host_scope():
    with pytest.raises(ValueError, match="non-empty MemoryScope"):
        ProtoPromptStoreAdapter(InMemoryStore(), scope=MemoryScope())
