from __future__ import annotations

import pytest

from protoprompt.profile.async_store import (
    AsyncInMemoryProfileStore,
    as_async_profile,
)
from protoprompt.profile.store import (
    InMemoryProfileStore,
    SqliteProfileStore,
    profile_from_dict,
    profile_to_dict,
)
from protoprompt.profile.types import Preferences, Traits, UserProfile


def _profile() -> UserProfile:
    p = UserProfile(user_id="u1", version=5, source="llm")
    p.traits = Traits(expertise="expert", style="concise")
    p.preferences = Preferences(format="bullets", language="ru", topics=["ai"])
    p.facts = {"name": "Илья", "stack": "python"}
    p.summary = "опытный бэкендер"
    p.updated_at = "2026-01-01T00:00:00"
    return p


# ── serialization ────────────────────────────────────────────────


def test_roundtrip_serialization():
    data = profile_to_dict(_profile())
    back = profile_from_dict(data)
    assert back == _profile()


def test_deserialization_ignores_future_fields_and_bad_types():
    back = profile_from_dict({
        "user_id": "u1",
        "traits": {"style": "concise", "future": "ignored"},
        "preferences": {"topics": "not-a-list", "future": True},
        "facts": None,
        "version": "bad",
    })
    assert back.traits.style == "concise"
    assert back.preferences.topics == []
    assert back.facts == {}
    assert back.version == 0


# ── InMemoryProfileStore ─────────────────────────────────────────


def test_inmemory_get_put_delete():
    store = InMemoryProfileStore()
    assert store.get("u1") is None
    store.put(_profile())
    assert store.get("u1") == _profile()
    store.delete("u1")
    assert store.get("u1") is None


def test_inmemory_put_overwrites():
    store = InMemoryProfileStore()
    store.put(UserProfile(user_id="u1", version=1))
    store.put(UserProfile(user_id="u1", version=2))
    assert store.get("u1").version == 2


def test_inmemory_compare_and_put_rejects_stale_version():
    store = InMemoryProfileStore()
    assert store.compare_and_put(
        UserProfile(user_id="u1", version=1), expected_version=None
    )
    assert not store.compare_and_put(
        UserProfile(user_id="u1", version=2), expected_version=0
    )
    assert store.get("u1").version == 1


# ── SqliteProfileStore ───────────────────────────────────────────


def test_sqlite_roundtrip():
    store = SqliteProfileStore(":memory:")
    store.put(_profile())
    assert store.get("u1") == _profile()


def test_sqlite_upsert():
    store = SqliteProfileStore(":memory:")
    store.put(UserProfile(user_id="u1", version=1))
    store.put(UserProfile(user_id="u1", version=2, summary="new"))
    got = store.get("u1")
    assert got.version == 2
    assert got.summary == "new"


def test_sqlite_delete():
    store = SqliteProfileStore(":memory:")
    store.put(_profile())
    store.delete("u1")
    assert store.get("u1") is None


def test_sqlite_compare_and_put_rejects_stale_version():
    with SqliteProfileStore(":memory:") as store:
        assert store.compare_and_put(
            UserProfile(user_id="u1", version=1), expected_version=None
        )
        assert not store.compare_and_put(
            UserProfile(user_id="u1", version=2), expected_version=0
        )
        assert store.get("u1").version == 1


# ── async helpers ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_inmemory_roundtrip():
    store = AsyncInMemoryProfileStore()
    await store.put(_profile())
    assert await store.get("u1") == _profile()
    await store.delete("u1")
    assert await store.get("u1") is None


@pytest.mark.asyncio
async def test_as_async_profile_wraps_sync():
    store = as_async_profile(InMemoryProfileStore())
    await store.put(_profile())
    assert await store.get("u1") == _profile()


@pytest.mark.asyncio
async def test_as_async_profile_passes_async_through():
    store = AsyncInMemoryProfileStore()
    assert as_async_profile(store) is store
