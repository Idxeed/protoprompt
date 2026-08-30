from __future__ import annotations

import asyncio
import pytest

from protoprompt.profile.manager import ProfileManager
from protoprompt.profile.source import RuleProfileSource
from protoprompt.profile.store import InMemoryProfileStore, SqliteProfileStore
from protoprompt.profile.types import FactOp, ProfileDelta, Signal, UserProfile
from protoprompt.scope import MemoryScope, scoped_doc_id


class _StubSource:
    def __init__(self, delta: ProfileDelta):
        self._delta = delta

    async def extract(self, user_id, signals):
        return self._delta


def sig(*texts: str) -> list[Signal]:
    return [Signal(user_id="u1", kind="message", role="user", text=t) for t in texts]


@pytest.mark.asyncio
async def test_update_creates_profile():
    store = InMemoryProfileStore()
    delta = ProfileDelta(
        fact_ops=[FactOp("add", "name", "Илья")], summary="backend", source="s"
    )
    mgr = ProfileManager(store, _StubSource(delta), clock=lambda: "T1")
    out = await mgr.update("u1", sig("hello"))
    assert out.version == 1
    assert out.updated_at == "T1"
    assert out.facts == {"name": "Илья"}
    assert store.get("u1") == out


@pytest.mark.asyncio
async def test_update_is_incremental():
    store = InMemoryProfileStore()
    source = _StubSource(ProfileDelta(fact_ops=[FactOp("add", "name", "Илья")]))
    mgr = ProfileManager(store, source, clock=lambda: "T1")
    await mgr.update("u1", sig("a"))

    source._delta = ProfileDelta(fact_ops=[FactOp("add", "stack", "python")])
    out = await mgr.update("u1", sig("b"))
    assert out.version == 2
    assert out.facts == {"name": "Илья", "stack": "python"}


@pytest.mark.asyncio
async def test_update_empty_delta_does_not_persist():
    store = InMemoryProfileStore()
    mgr = ProfileManager(store, _StubSource(ProfileDelta()))
    out = await mgr.update("u1", sig("a"))
    assert out.version == 0
    assert store.get("u1") is None  # nothing persisted


@pytest.mark.asyncio
async def test_get_and_delete():
    store = InMemoryProfileStore()
    store.put(UserProfile(user_id="u1", version=1))
    mgr = ProfileManager(store, _StubSource(ProfileDelta()))
    assert (await mgr.get("u1")).version == 1
    await mgr.delete("u1")
    assert await mgr.get("u1") is None


@pytest.mark.asyncio
async def test_reset_zeroes_version():
    store = InMemoryProfileStore()
    mgr = ProfileManager(store, _StubSource(ProfileDelta()))
    await mgr.update("u1", sig("a"))  # no-op delta, nothing stored
    await mgr.reset("u1")
    fresh = await mgr.get("u1")
    assert fresh.version == 0
    assert fresh.updated_at == ""
    assert fresh.facts == {}


@pytest.mark.asyncio
async def test_default_source_is_rules():
    store = InMemoryProfileStore()
    mgr = ProfileManager(store)
    out = await mgr.update("u1", sig("привет, это довольно длинное сообщение"))
    assert out.source == "rules"
    assert out.preferences.language == "ru"


@pytest.mark.asyncio
async def test_rejects_signals_from_another_user():
    mgr = ProfileManager(InMemoryProfileStore(), _StubSource(ProfileDelta()))
    with pytest.raises(ValueError, match="another user"):
        await mgr.update("u1", [Signal("u2", "message", "private")])


@pytest.mark.asyncio
async def test_concurrent_managers_do_not_lose_updates():
    class BarrierSource:
        def __init__(self, key: str):
            self.key = key

        async def extract(self, user_id, signals):
            arrived.append(self.key)
            if len(arrived) == 2:
                ready.set()
            await ready.wait()
            return ProfileDelta(fact_ops=[FactOp("add", self.key, self.key)])

    store = InMemoryProfileStore()
    ready = asyncio.Event()
    arrived: list[str] = []
    first = ProfileManager(store, BarrierSource("a"), clock=lambda: "T")
    second = ProfileManager(store, BarrierSource("b"), clock=lambda: "T")

    await asyncio.gather(
        first.update("u1", sig("a")),
        second.update("u1", sig("b")),
    )

    saved = store.get("u1")
    assert saved.version == 2
    assert saved.facts == {"a": "a", "b": "b"}


@pytest.mark.asyncio
@pytest.mark.parametrize("store_factory", [InMemoryProfileStore, SqliteProfileStore])
async def test_scoped_managers_isolate_same_user_lifecycle(store_factory):
    store = store_factory()
    acme_scope = MemoryScope(tenant="acme")
    other_scope = MemoryScope(tenant="other")
    acme_source = _StubSource(
        ProfileDelta(fact_ops=[FactOp("add", "tenant", "acme")])
    )
    other_source = _StubSource(
        ProfileDelta(fact_ops=[FactOp("add", "tenant", "other")])
    )
    acme = ProfileManager(store, acme_source, scope=acme_scope)
    other = ProfileManager(store, other_source, scope=other_scope)
    try:
        acme_profile = await acme.update("u1", sig("acme"))
        other_profile = await other.update("u1", sig("other"))

        assert acme_profile.user_id == "u1"
        assert other_profile.user_id == "u1"
        assert (await acme.get("u1")).facts == {"tenant": "acme"}
        assert (await other.get("u1")).facts == {"tenant": "other"}

        acme_source._delta = ProfileDelta(
            fact_ops=[FactOp("add", "acme_only", "yes")]
        )
        updated_acme = await acme.update("u1", sig("acme again"))
        assert updated_acme.version == 2
        assert updated_acme.facts == {"tenant": "acme", "acme_only": "yes"}
        assert (await other.get("u1")).facts == {"tenant": "other"}

        await acme.delete("u1")
        assert await acme.get("u1") is None
        assert (await other.get("u1")).facts == {"tenant": "other"}
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


@pytest.mark.asyncio
async def test_scoped_manager_does_not_adopt_legacy_unscoped_profile():
    store = InMemoryProfileStore()
    store.put(UserProfile(user_id="u1", facts={"legacy": "profile"}))
    scoped = ProfileManager(
        store,
        _StubSource(ProfileDelta(fact_ops=[FactOp("add", "tenant", "acme")])),
        scope=MemoryScope(tenant="acme"),
    )
    legacy = ProfileManager(store, _StubSource(ProfileDelta()))

    assert (await legacy.get("u1")).facts == {"legacy": "profile"}
    assert await scoped.get("u1") is None

    scoped_profile = await scoped.update("u1", sig("scoped"))
    assert scoped_profile.user_id == "u1"
    assert scoped_profile.facts == {"tenant": "acme"}
    assert (await legacy.get("u1")).facts == {"legacy": "profile"}


@pytest.mark.asyncio
@pytest.mark.parametrize("store_factory", [InMemoryProfileStore, SqliteProfileStore])
async def test_scoped_manager_rejects_legacy_physical_key_collision(store_factory):
    store = store_factory()
    scope = MemoryScope(tenant="acme")
    physical_id = scoped_doc_id("u1", scope)
    legacy_collision = UserProfile(user_id=physical_id, facts={"legacy": "private"})
    store.put(legacy_collision)
    manager = ProfileManager(store, _StubSource(ProfileDelta()), scope=scope)
    try:
        assert await manager.get("u1") is None
        assert store.get(physical_id) == legacy_collision
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


@pytest.mark.asyncio
@pytest.mark.parametrize("store_factory", [InMemoryProfileStore, SqliteProfileStore])
async def test_scoped_manager_reset_and_delete_preserve_legacy_key_collision(store_factory):
    store = store_factory()
    scope = MemoryScope(tenant="acme")
    physical_id = scoped_doc_id("u1", scope)
    legacy_collision = UserProfile(user_id=physical_id, facts={"legacy": "private"})
    store.put(legacy_collision)
    manager = ProfileManager(store, _StubSource(ProfileDelta()), scope=scope)
    try:
        with pytest.raises(ValueError, match="different logical user"):
            await manager.reset("u1")
        with pytest.raises(ValueError, match="different logical user"):
            await manager.delete("u1")

        assert store.get(physical_id) == legacy_collision
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


@pytest.mark.asyncio
async def test_scoped_manager_rejects_scope_blind_store_before_it_can_leak():
    class ScopeBlindStore(InMemoryProfileStore):
        supports_profile_scopes = False

    store = ScopeBlindStore()
    scope = MemoryScope(tenant="acme")
    physical_id = scoped_doc_id("u1", scope)
    legacy_collision = UserProfile(user_id=physical_id, facts={"legacy": "private"})
    store.put(legacy_collision)

    with pytest.raises(ValueError, match="native supports_profile_scopes=True"):
        ProfileManager(store, _StubSource(ProfileDelta()), scope=scope)

    # The same custom store remains usable without a scope and retains its
    # pre-0.6.1 behavior.
    unscoped = ProfileManager(store, _StubSource(ProfileDelta()))
    assert (await unscoped.get(physical_id)) == legacy_collision
