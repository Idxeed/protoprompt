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
from protoprompt.scope import MemoryScope, scoped_doc_id


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


@pytest.mark.parametrize("store_factory", [InMemoryProfileStore, SqliteProfileStore])
def test_builtin_profile_stores_scope_physical_key_but_keep_logical_user_id(
    store_factory,
):
    store = store_factory()
    acme = MemoryScope(tenant="acme")
    other = MemoryScope(tenant="other")
    try:
        acme_profile = UserProfile(user_id="u1", facts={"tenant": "acme"})
        other_profile = UserProfile(user_id="u1", facts={"tenant": "other"})
        store.put(acme_profile, scope=acme)
        store.put(other_profile, scope=other)

        assert store.get("u1") is None
        assert store.get("u1", scope=acme) == acme_profile
        assert store.get("u1", scope=other) == other_profile
        assert store.get("u1", scope=acme).user_id == "u1"

        store.delete("u1", scope=acme)
        assert store.get("u1", scope=acme) is None
        assert store.get("u1", scope=other) == other_profile
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


@pytest.mark.parametrize("store_factory", [InMemoryProfileStore, SqliteProfileStore])
def test_scoped_get_rejects_legacy_physical_key_collision(store_factory):
    store = store_factory()
    scope = MemoryScope(tenant="acme")
    physical_id = scoped_doc_id("u1", scope)
    legacy_collision = UserProfile(
        user_id=physical_id,
        facts={"legacy": "must stay private"},
    )
    try:
        store.put(legacy_collision)

        assert store.get(physical_id) == legacy_collision
        assert store.get("u1", scope=scope) is None
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


@pytest.mark.parametrize("store_factory", [InMemoryProfileStore, SqliteProfileStore])
def test_scoped_profile_mutators_refuse_legacy_physical_key_collision(store_factory):
    store = store_factory()
    scope = MemoryScope(tenant="acme")
    physical_id = scoped_doc_id("u1", scope)
    legacy_collision = UserProfile(
        user_id=physical_id,
        facts={"legacy": "must stay private"},
    )
    try:
        store.put(legacy_collision)

        with pytest.raises(ValueError, match="different logical user"):
            store.put(UserProfile(user_id="u1"), scope=scope)
        with pytest.raises(ValueError, match="different logical user"):
            store.compare_and_put(
                UserProfile(user_id="u1"),
                expected_version=None,
                scope=scope,
            )
        with pytest.raises(ValueError, match="different logical user"):
            store.delete("u1", scope=scope)

        assert store.get(physical_id) == legacy_collision
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


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
async def test_async_inmemory_scope_keeps_logical_user_id():
    store = AsyncInMemoryProfileStore()
    scope = MemoryScope(tenant="acme")
    profile = _profile()
    await store.put(profile, scope=scope)
    assert await store.get("u1") is None
    assert await store.get("u1", scope=scope) == profile
    assert (await store.get("u1", scope=scope)).user_id == "u1"


@pytest.mark.asyncio
async def test_async_inmemory_scoped_get_rejects_legacy_physical_key_collision():
    store = AsyncInMemoryProfileStore()
    scope = MemoryScope(tenant="acme")
    physical_id = scoped_doc_id("u1", scope)
    legacy_collision = UserProfile(user_id=physical_id)
    await store.put(legacy_collision)

    assert await store.get(physical_id) == legacy_collision
    assert await store.get("u1", scope=scope) is None


@pytest.mark.asyncio
async def test_as_async_profile_wraps_sync():
    store = as_async_profile(InMemoryProfileStore())
    await store.put(_profile())
    assert await store.get("u1") == _profile()


@pytest.mark.asyncio
async def test_as_async_profile_forwards_native_scope_support():
    store = as_async_profile(InMemoryProfileStore())
    scope = MemoryScope(tenant="acme")
    await store.put(_profile(), scope=scope)
    assert await store.get("u1") is None
    assert await store.get("u1", scope=scope) == _profile()


@pytest.mark.asyncio
async def test_as_async_profile_passes_async_through():
    store = AsyncInMemoryProfileStore()
    assert as_async_profile(store) is store
