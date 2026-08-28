"""The profile engine orchestrator: load → extract → merge → persist."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable

from protoprompt.events import EventDispatcher, EventSink, ProfileEvent, dispatch, elapsed_ms, new_trace_id, scope_id
from protoprompt.profile.merge import merge as default_merge
from protoprompt.profile.source import ProfileProtocol
from protoprompt.profile.types import Signal, UserProfile
from protoprompt.scope import MemoryScope
from protoprompt.store.protocol import await_if_needed

Clock = Callable[[], str]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProfileManager:
    """Cross-session incremental profile engine.

    Each :meth:`update` loads the current profile, runs the configured
    :class:`~protoprompt.profile.source.ProfileProtocol` source to produce
    a :class:`~protoprompt.profile.types.ProfileDelta`, folds it in, and
    persists the result. The store may be sync or async (dispatched via
    :func:`~protoprompt.store.protocol.await_if_needed`).

    Args:
        store: a ``ProfileStore`` (sync or async).
        source: the extractor (defaults to the rule-based source if omitted).
        merger: the merge function (defaults to
            :func:`protoprompt.profile.merge.merge`).
        clock: zero-arg callable returning an ISO-8601 timestamp for
            ``updated_at`` (injectable for tests).
    """

    def __init__(
        self,
        store: Any,
        source: ProfileProtocol | None = None,
        *,
        merger: Callable[..., UserProfile] | None = None,
        clock: Clock = _utcnow,
        scope: MemoryScope | None = None,
        event_sink: EventSink | EventDispatcher | None = None,
    ) -> None:
        if source is None:
            from protoprompt.profile.source import RuleProfileSource

            source = RuleProfileSource()
        self._store = store
        self._source = source
        self._merger: Callable[..., UserProfile] = merger or default_merge
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._scope = scope
        self._event_sink = event_sink

    async def update(self, user_id: str, signals: list[Signal]) -> UserProfile:
        """Fold ``signals`` into the user's profile and persist the result."""
        started_at = perf_counter()
        trace_id = new_trace_id()
        mismatched = [s.user_id for s in signals if s.user_id and s.user_id != user_id]
        if mismatched:
            raise ValueError(
                f"signals for another user cannot update {user_id!r}: {mismatched[0]!r}"
            )

        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            existing = await await_if_needed(self._store.get(user_id))
            profile = existing if existing is not None else UserProfile(user_id=user_id)
            delta = await self._source.extract(user_id, signals)
            now = self._clock()

            # Optimistic concurrency protects multiple manager instances that
            # share one store. The delta is deterministic and can be folded
            # over the newest profile after a compare-and-swap conflict.
            for _ in range(8):
                merged = self._merger(profile, delta, now=now)
                if merged is profile:
                    self._emit_profile(
                        "unchanged",
                        trace_id,
                        started_at,
                        signal_count=len(signals),
                        version=profile.version,
                    )
                    return merged

                compare_and_put = getattr(self._store, "compare_and_put", None)
                if compare_and_put is None:
                    await await_if_needed(self._store.put(merged))
                    self._emit_profile(
                        "updated",
                        trace_id,
                        started_at,
                        signal_count=len(signals),
                        version=merged.version,
                    )
                    return merged

                expected = profile.version if existing is not None else None
                saved = await await_if_needed(
                    compare_and_put(merged, expected_version=expected)
                )
                if saved:
                    self._emit_profile(
                        "updated",
                        trace_id,
                        started_at,
                        signal_count=len(signals),
                        version=merged.version,
                    )
                    return merged

                existing = await await_if_needed(self._store.get(user_id))
                profile = (
                    existing if existing is not None else UserProfile(user_id=user_id)
                )

            raise RuntimeError(f"profile update for {user_id!r} conflicted repeatedly")

    async def get(self, user_id: str) -> UserProfile | None:
        return await await_if_needed(self._store.get(user_id))

    async def reset(self, user_id: str) -> UserProfile:
        """Start over: persist and return a fresh, empty profile."""
        fresh = UserProfile(user_id=user_id)
        await await_if_needed(self._store.put(fresh))
        self._emit_profile("reset", new_trace_id(), perf_counter(), version=0)
        return fresh

    async def delete(self, user_id: str) -> None:
        await await_if_needed(self._store.delete(user_id))
        self._emit_profile("deleted", new_trace_id(), perf_counter())

    def _emit_profile(
        self,
        action: str,
        trace_id: str,
        started_at: float,
        **attributes: Any,
    ) -> None:
        dispatch(self._event_sink, ProfileEvent(
            action=action,
            trace_id=trace_id,
            scope_id=scope_id(self._scope),
            duration_ms=elapsed_ms(started_at),
            attributes=attributes,
        ))
