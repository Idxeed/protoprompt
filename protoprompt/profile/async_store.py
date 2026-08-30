"""Async helpers around :class:`~protoprompt.profile.store.ProfileStore`.

``AsyncInMemoryProfileStore`` is an awaitable twin of
:class:`InMemoryProfileStore`. ``AsyncProfileStoreWrapper`` lifts any sync
store onto the event loop via worker threads. ``as_async_profile`` picks
the right one.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Protocol, runtime_checkable

from protoprompt.profile.store import InMemoryProfileStore
from protoprompt.profile.types import UserProfile
from protoprompt.scope import MemoryScope


@runtime_checkable
class AsyncProfileStore(Protocol):
    async def get(self, user_id: str) -> UserProfile | None:
        ...

    async def put(self, profile: UserProfile) -> None:
        ...

    async def delete(self, user_id: str) -> None:
        ...

    async def compare_and_put(
        self, profile: UserProfile, *, expected_version: int | None
    ) -> bool:
        ...


class AsyncInMemoryProfileStore(InMemoryProfileStore):
    """Async variant of ``InMemoryProfileStore``: same semantics, awaitable."""

    async def get(
        self,
        user_id: str,
        *,
        scope: MemoryScope | None = None,
    ) -> UserProfile | None:
        return super().get(user_id, scope=scope)

    async def put(self, profile: UserProfile, *, scope: MemoryScope | None = None) -> None:
        super().put(profile, scope=scope)

    async def delete(self, user_id: str, *, scope: MemoryScope | None = None) -> None:
        super().delete(user_id, scope=scope)

    async def compare_and_put(
        self,
        profile: UserProfile,
        *,
        expected_version: int | None,
        scope: MemoryScope | None = None,
    ) -> bool:
        return super().compare_and_put(
            profile,
            expected_version=expected_version,
            scope=scope,
        )


class AsyncProfileStoreWrapper:
    """Expose a sync ``ProfileStore`` through the async protocol."""

    def __init__(self, store: Any) -> None:
        self._sync = store

    @property
    def sync_store(self) -> Any:
        return self._sync

    @property
    def supports_profile_scopes(self) -> bool:
        """Whether the wrapped store keeps logical ids under scoped keys."""
        return bool(getattr(self._sync, "supports_profile_scopes", False))

    async def get(
        self,
        user_id: str,
        *,
        scope: MemoryScope | None = None,
    ) -> UserProfile | None:
        if scope is not None and self.supports_profile_scopes:
            return await asyncio.to_thread(self._sync.get, user_id, scope=scope)
        return await asyncio.to_thread(self._sync.get, user_id)

    async def put(self, profile: UserProfile, *, scope: MemoryScope | None = None) -> None:
        if scope is not None and self.supports_profile_scopes:
            await asyncio.to_thread(self._sync.put, profile, scope=scope)
            return
        await asyncio.to_thread(self._sync.put, profile)

    async def delete(self, user_id: str, *, scope: MemoryScope | None = None) -> None:
        if scope is not None and self.supports_profile_scopes:
            await asyncio.to_thread(self._sync.delete, user_id, scope=scope)
            return
        await asyncio.to_thread(self._sync.delete, user_id)

    async def compare_and_put(
        self,
        profile: UserProfile,
        *,
        expected_version: int | None,
        scope: MemoryScope | None = None,
    ) -> bool:
        method = getattr(self._sync, "compare_and_put", None)
        if method is None:
            await self.put(profile, scope=scope)
            return True
        if scope is not None and self.supports_profile_scopes:
            return await asyncio.to_thread(
                method,
                profile,
                expected_version=expected_version,
                scope=scope,
            )
        return await asyncio.to_thread(
            method,
            profile,
            expected_version=expected_version,
        )


def as_async_profile(store: Any) -> Any:
    """Return ``store`` when already async, else wrap it for the loop."""
    if inspect.iscoroutinefunction(getattr(store, "get", None)):
        return store
    return AsyncProfileStoreWrapper(store)
