from __future__ import annotations

import uuid

import pytest

pytest.importorskip("chromadb", reason="chroma extra not installed")

from protoprompt import ContextInput, ContextBuilder
from protoprompt.store.chroma import ChromaStore

pytestmark = pytest.mark.integration


def _make_store(dim: int) -> ChromaStore:
    """A fresh in-memory Chroma collection per call (no persistence)."""
    return ChromaStore(collection_name=f"protoprompt_test_{uuid.uuid4().hex}")


def _populate(store: ChromaStore, dim: int) -> None:
    """No-op helper kept for symmetry with future fixtures."""
    return None


def test_chroma_add_query_delete():
    store = _make_store(dim=3)
    store.add(
        "doc1",
        ["alpha content", "beta content"],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        {"source": "unit"},
    )
    assert store.count() == 2

    out = store.query([1.0, 0.01, 0.0], top_k=1)
    assert out
    assert "alpha" in out[0]["document"]


def test_chroma_where_filter():
    store = _make_store(dim=2)
    store.add("a", ["alpha"], [[1.0, 0.0]], {"kind": "rag"})
    store.add("b", ["beta"], [[1.0, 0.0]], {"kind": "summary"})

    out = store.query([1.0, 0.0], top_k=10, where={"kind": "rag"})
    assert len(out) == 1
    assert out[0]["document"] == "alpha"


def test_chroma_delete():
    store = _make_store(dim=2)
    store.add("x", ["alpha"], [[1.0, 0.0]])
    store.add("y", ["beta"], [[0.0, 1.0]])
    assert store.count() == 2
    store.delete("x")
    assert store.count() == 1


@pytest.mark.asyncio
async def test_chroma_via_context_builder():
    from _mocks import MockLLM

    store = _make_store(dim=16)
    store.add("1", ["Paris is the capital of France"], [[1.0] + [0.0] * 15])
    llm = MockLLM()
    builder = ContextBuilder(store, llm)
    out = await builder.build(ContextInput(
        query="capital of France?",
        system_prompt="sys",
        doc_ids=[1],
    ))
    assert "Paris" in out.system_prompt
