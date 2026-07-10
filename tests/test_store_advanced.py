from __future__ import annotations

import pytest

from protoprompt.store.memory import InMemStore
from protoprompt.store.protocol import StoreProtocol


def test_inmem_supports_arbitrary_metadata_filter():
    store = InMemStore()
    store.add("a", ["alpha"], [[1.0, 0.0]], {"chat_id": "c1", "kind": "summary"})
    store.add("b", ["beta"], [[0.0, 1.0]], {"chat_id": "c2", "kind": "summary"})
    store.add("c", ["gamma"], [[1.0, 0.0]], {"chat_id": "c1", "kind": "rag"})

    out = store.query([1.0, 0.0], top_k=10, where={"chat_id": "c1", "kind": "rag"})
    assert len(out) == 1
    assert out[0]["document"] == "gamma"


def test_inmem_supports_in_operator():
    store = InMemStore()
    store.add("a", ["alpha"], [[1.0, 0.0]], {"chat_id": "c1"})
    store.add("b", ["beta"], [[0.0, 1.0]], {"chat_id": "c2"})
    store.add("c", ["gamma"], [[1.0, 0.0]], {"chat_id": "c3"})

    out = store.query(
        [1.0, 0.0],
        top_k=10,
        where={"chat_id": {"$in": ["c1", "c3"]}},
    )
    docs = {h["document"] for h in out}
    assert docs == {"alpha", "gamma"}


def test_inmem_score_threshold():
    store = InMemStore()
    store.add("a", ["alpha"], [[1.0, 0.0]])
    store.add("b", ["beta"], [[0.0, 1.0]])

    # Query close to alpha, threshold should keep only alpha.
    out = store.query([1.0, 0.01], top_k=10, score_threshold=0.9)
    assert len(out) == 1
    assert out[0]["document"] == "alpha"


def test_inmem_is_protocol():
    store = InMemStore()
    assert isinstance(store, StoreProtocol)
