from __future__ import annotations

import asyncio
import pytest

from protoprompt.profile.manager import ProfileManager
from protoprompt.profile.source import RuleProfileSource
from protoprompt.profile.store import InMemoryProfileStore
from protoprompt.profile.types import FactOp, ProfileDelta, Signal, UserProfile


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
