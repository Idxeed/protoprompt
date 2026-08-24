from __future__ import annotations


class InMemStore:
    def __init__(self) -> None:
        self._chunks: dict[str, list[dict]] = {}
        self._counter: int = 0

    def add(
        self,
        doc_id: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict | None = None,
    ) -> None:
        meta = metadata or {}
        entries = []
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            entries.append({
                "id": f"{doc_id}_chunk_{self._counter}",
                "document": chunk,
                "embedding": emb,
                "metadata": {**meta, "chunk_index": i, "doc_id": doc_id},
            })
            self._counter += 1
        if doc_id not in self._chunks:
            self._chunks[doc_id] = []
        self._chunks[doc_id].extend(entries)

    def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        where: dict | None = None,
        score_threshold: float | None = None,
    ) -> list[dict]:
        candidates: list[tuple[float, dict]] = []
        for entries in self._chunks.values():
            for entry in entries:
                if where and not _matches_where(entry.get("metadata", {}), where):
                    continue
                score = _cosine_similarity(embedding, entry["embedding"])
                if score_threshold is not None and score < score_threshold:
                    continue
                candidates.append((score, entry))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [{**e, "score": s} for s, e in candidates[:top_k]]

    def get(self, doc_id: str) -> dict | None:
        """Fetch entries of an exact doc (recall symbol channel)."""
        for entry in self._chunks.get(doc_id, ()):
            return {"document": entry["document"],
                    "metadata": dict(entry["metadata"])}
        return None

    def delete(self, doc_id: str) -> None:
        self._chunks.pop(doc_id, None)

    def count(self) -> int:
        return sum(len(v) for v in self._chunks.values())


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _matches_where(metadata: dict, where: dict) -> bool:
    """Evaluate ChromaDB-shaped filter against a metadata dict.

    Supports equality and ``$in`` per key. AND-combined across keys.
    """
    for key, condition in where.items():
        actual = metadata.get(key)
        if isinstance(condition, dict) and "$in" in condition:
            if actual not in condition["$in"]:
                return False
        else:
            if actual != condition:
                return False
    return True
