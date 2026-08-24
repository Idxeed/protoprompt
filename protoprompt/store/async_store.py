"""Async-first helpers around stores.

``AsyncInMemStore`` is a drop-in async twin of :class:`InMemStore`.
``AsyncStoreWrapper`` lifts any sync store onto the event loop by
running each call in a worker thread. ``as_async`` picks the right one.
"""

from __future__ import annotations

import asyncio
from typing import Any

from protoprompt.store.memory import InMemStore
from protoprompt.store.protocol import StoreProtocol, is_async_store


class AsyncInMemStore(InMemStore):
    """Async variant of ``InMemStore``: same semantics, awaitable calls."""

    async def add(
        self,
        doc_id: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict | None = None,
    ) -> None:
        super().add(doc_id, chunks, embeddings, metadata)

    async def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        where: dict | None = None,
        score_threshold: float | None = None,
    ) -> list[dict]:
        return super().query(embedding, top_k=top_k, where=where, score_threshold=score_threshold)

    async def delete(self, doc_id: str) -> None:
        super().delete(doc_id)

    async def count(self) -> int:
        return super().count()


class AsyncStoreWrapper:
    """Expose a sync ``StoreProtocol`` through the async protocol.

    Every call is dispatched via ``asyncio.to_thread`` so a blocking
    backend (ChromaDB, Qdrant client, SQLite) never stalls the loop.
    """

    def __init__(self, store: StoreProtocol) -> None:
        self._sync = store

    @property
    def sync_store(self) -> StoreProtocol:
        return self._sync

    async def add(
        self,
        doc_id: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict | None = None,
    ) -> None:
        await asyncio.to_thread(self._sync.add, doc_id, chunks, embeddings, metadata)

    async def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        where: dict | None = None,
        score_threshold: float | None = None,
    ) -> list[dict]:
        return await asyncio.to_thread(
            self._sync.query, embedding, top_k, where, score_threshold
        )

    async def delete(self, doc_id: str) -> None:
        await asyncio.to_thread(self._sync.delete, doc_id)

    async def count(self) -> int:
        return await asyncio.to_thread(self._sync.count)


def as_async(store: Any) -> Any:
    """Return ``store`` when already async, else wrap it for the loop."""
    if is_async_store(store):
        return store
    return AsyncStoreWrapper(store)
