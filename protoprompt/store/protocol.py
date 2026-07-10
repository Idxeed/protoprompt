from __future__ import annotations

from typing import Protocol, runtime_checkable


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
