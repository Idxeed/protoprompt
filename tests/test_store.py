from __future__ import annotations

import pytest

from protoprompt.store.memory import InMemStore


def test_inmem_add_and_count():
    store = InMemStore()
    store.add("doc1", ["chunk A", "chunk B"], [[0.1] * 5, [0.2] * 5])
    assert store.count() == 2


def test_inmem_query_returns_top_results():
    store = InMemStore()
    emb_a = [1.0, 0.0, 0.0]
    emb_b = [0.0, 1.0, 0.0]
    emb_c = [0.0, 0.0, 1.0]
    store.add("doc1", ["A"], [emb_a])
    store.add("doc1", ["B"], [emb_b])
    store.add("doc1", ["C"], [emb_c])

    results = store.query([1.0, 0.1, 0.0], top_k=2)
    assert len(results) == 2
    assert results[0]["document"] == "A"


def test_inmem_query_with_where_filter():
    store = InMemStore()
    store.add("doc_a", ["A1"], [[1.0, 0.0]])
    store.add("doc_b", ["B1"], [[1.0, 0.0]])

    results = store.query([1.0, 0.0], top_k=5, where={"doc_id": "doc_a"})
    assert len(results) == 1
    assert results[0]["document"] == "A1"


def test_inmem_delete():
    store = InMemStore()
    store.add("doc_x", ["X1", "X2"], [[1.0], [1.0]])
    store.add("doc_y", ["Y1"], [[1.0]])
    assert store.count() == 3
    store.delete("doc_x")
    assert store.count() == 1


def test_inmem_empty_query():
    store = InMemStore()
    results = store.query([1.0], top_k=5)
    assert results == []
