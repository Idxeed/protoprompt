"""Persistent SQLite-backed vector store, standard library only.

Embeddings are stored as little-endian float32 blobs; similarity is
computed in Python with the same cosine as ``InMemStore`` — plenty fast
for local sessions up to tens of thousands of chunks. ``add`` replaces
any previous chunks of the same ``doc_id``, so re-adding a document is
idempotent.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import threading

from protoprompt.store.memory import _matches_where

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    document TEXT NOT NULL,
    metadata TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id);
"""


def _pack(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class SqliteStore:
    """Zero-dependency persistent store.

    Args:
        path: SQLite file path, or ``":memory:"`` for an in-memory db.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def add(
        self,
        doc_id: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict | None = None,
    ) -> None:
        meta = metadata or {}
        rows = [
            (
                doc_id,
                i,
                chunk,
                json.dumps({**meta, "chunk_index": i, "doc_id": doc_id}, ensure_ascii=False),
                _pack(emb),
            )
            for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
        ]
        with self._lock:
            self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            self._conn.executemany(
                "INSERT INTO chunks (doc_id, chunk_index, document, metadata, embedding)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        where: dict | None = None,
        score_threshold: float | None = None,
    ) -> list[dict]:
        sql = "SELECT id, document, metadata, embedding FROM chunks"
        params: tuple = ()
        doc_filter = (where or {}).get("doc_id")
        if isinstance(doc_filter, dict) and "$in" in doc_filter:
            placeholders = ",".join("?" for _ in doc_filter["$in"])
            sql += f" WHERE doc_id IN ({placeholders})"
            params = tuple(doc_filter["$in"])
        elif isinstance(doc_filter, str):
            sql += " WHERE doc_id = ?"
            params = (doc_filter,)

        candidates: list[tuple[float, dict]] = []
        with self._lock:
            for row_id, document, meta_json, blob in self._conn.execute(sql, params):
                meta = json.loads(meta_json)
                if where and not _matches_where(meta, where):
                    continue
                emb = _unpack(blob)
                score = _cosine(embedding, emb)
                if score_threshold is not None and score < score_threshold:
                    continue
                candidates.append((score, {
                    "id": row_id,
                    "document": document,
                    "metadata": meta,
                    "score": score,
                }))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in candidates[:top_k]]

    def get(self, doc_id: str) -> dict | None:
        """Fetch a cold document by exact id (recall symbol channel)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT document, metadata FROM chunks WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
        if row is None:
            return None
        return {"document": row[0], "metadata": json.loads(row[1])}

    def delete(self, doc_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
