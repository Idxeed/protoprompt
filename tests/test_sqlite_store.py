"""Tests for the zero-dependency persistent SqliteStore."""

from __future__ import annotations

import pytest

from protoprompt import SqliteStore


def _populate(store) -> None:
    store.add("doc-1", ["alpha", "beta"], [[1.0, 0.0], [0.0, 1.0]], {"kind": "fact"})
    store.add("doc-2", ["gamma"], [[0.7, 0.7]], {"kind": "opinion"})


def test_add_query_count(tmp_path):
    store = SqliteStore(str(tmp_path / "v.db"))
    _populate(store)
    assert store.count() == 3
    hits = store.query([1.0, 0.0], top_k=2)
    assert hits[0]["document"] == "alpha"
    assert hits[0]["metadata"]["doc_id"] == "doc-1"
    assert hits[0]["metadata"]["chunk_index"] == 0


def test_where_equality_and_in():
    store = SqliteStore()
    _populate(store)
    hits = store.query([1.0, 0.0], top_k=10, where={"kind": "fact"})
    assert {h["document"] for h in hits} == {"alpha", "beta"}
    hits = store.query([1.0, 0.0], top_k=10, where={"doc_id": {"$in": ["doc-2"]}})
    assert [h["document"] for h in hits] == ["gamma"]


def test_score_threshold_filters():
    store = SqliteStore()
    _populate(store)
    hits = store.query([1.0, 0.0], top_k=10, score_threshold=0.99)
    assert [h["document"] for h in hits] == ["alpha"]


def test_add_replaces_previous_doc():
    store = SqliteStore()
    store.add("d", ["one", "two"], [[1.0], [2.0]])
    store.add("d", ["three"], [[3.0]])
    assert store.count() == 1
    assert store.query([3.0], top_k=5)[0]["document"] == "three"


def test_delete_removes_doc():
    store = SqliteStore()
    _populate(store)
    store.delete("doc-1")
    assert store.count() == 1
    assert store.query([1.0, 0.0], top_k=5, where={"doc_id": "doc-1"}) == []
    assert [h["document"] for h in store.query([0.7, 0.7])] == ["gamma"]


def test_persistence_across_instances(tmp_path):
    db = str(tmp_path / "persist.db")
    first = SqliteStore(db)
    _populate(first)
    first.close()

    second = SqliteStore(db)
    assert second.count() == 3
    hits = second.query([0.0, 1.0], top_k=1)
    assert hits[0]["document"] == "beta"


def test_unicode_roundtrip():
    store = SqliteStore()
    store.add(
        "ru",
        ["Париж — столица Франции"],
        [[0.5, 0.5]],
        {"язык": "русский"},
    )
    hit = store.query([0.5, 0.5], top_k=1)[0]
    assert hit["document"] == "Париж — столица Франции"
    assert hit["metadata"]["язык"] == "русский"


def test_empty_query_top_k_zero():
    store = SqliteStore()
    _populate(store)
    assert store.query([1.0, 0.0], top_k=0) == []
