from __future__ import annotations

import inspect
from typing import Any, Protocol, runtime_checkable


async def await_if_needed(value: Any) -> Any:
    """Await ``value`` when it is a coroutine, return it as-is otherwise.

    Lets the builders accept both sync (``StoreProtocol``) and async
    (``AsyncStoreProtocol``) stores transparently.
    """
    if inspect.isawaitable(value):
        return await value
    return value


def is_async_store(store: Any) -> bool:
    """True when ``store.query`` is a coroutine function."""
    return inspect.iscoroutinefunction(getattr(store, "query", None))


@runtime_checkable
class AsyncStoreProtocol(Protocol):
    """Non-blocking counterpart of ``StoreProtocol``.

    Implementations must be safe to call from a running event loop:
    any blocking I/O belongs in a worker thread inside the store.
    """

    async def add(
        self,
        doc_id: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict | None = None,
    ) -> None:
        ...

    async def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        where: dict | None = None,
        score_threshold: float | None = None,
    ) -> list[dict]:
        ...

    async def delete(self, doc_id: str) -> None:
        ...

    async def count(self) -> int:
        ...


@runtime_checkable
class StoreProtocol(Protocol):
    def add(
        self,
        doc_id: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict | None = None,
    ) -> None:
        ...

    def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        where: dict | None = None,
        score_threshold: float | None = None,
    ) -> list[dict]:
        """Return top-k entries ranked by similarity to ``embedding``.

        ``where`` follows ChromaDB's filter shape:
        - ``{"field": value}`` for equality
        - ``{"field": {"$in": [...]}}`` for set membership

        ``score_threshold`` (when supported by the store) drops entries
        with similarity below the threshold. ``None`` keeps everything.
        """
        ...

    def delete(self, doc_id: str) -> None:
        ...

    def count(self) -> int:
        ...
