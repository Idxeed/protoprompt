"""Qdrant vector store adapter.

Works against a Qdrant server (``url=``), local persistent storage
(``path=``) or throwaway in-memory mode (default). Point IDs are
deterministic UUIDv5 of ``doc_id:chunk_index``, so re-adding the same
document upserts instead of duplicating.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "protoprompt/qdrant")


class QdrantStore:
    """``StoreProtocol`` implementation on top of ``qdrant-client``.

    Args:
        collection_name: Qdrant collection; created on first use.
        dim: embedding dimensionality. When given, the collection is
            ensured up front; otherwise it is created lazily from the
            first ``add`` call.
        url: server address, e.g. ``http://localhost:6333``.
        path: local persistent directory (embedded Qdrant mode).
    """

    def __init__(
        self,
        collection_name: str = "protoprompt",
        dim: int | None = None,
        url: str | None = None,
        path: str | None = None,
        api_key: str | None = None,
        prefer_grpc: bool = False,
    ) -> None:
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:
            raise ImportError(
                "QdrantStore requires 'qdrant-client'. "
                "Install with: pip install 'protoprompt[qdrant]'"
            ) from exc

        if url is not None:
            self._client = QdrantClient(
                url=url, api_key=api_key, prefer_grpc=prefer_grpc
            )
        elif path is not None:
            self._client = QdrantClient(path=path)
        else:
            self._client = QdrantClient(":memory:")

        self._collection = collection_name
        self._dim = dim
        if dim is not None:
            self._ensure_collection(dim)

    def _ensure_collection(self, dim: int) -> None:
        from qdrant_client import models

        existing = {c.name: c for c in self._client.get_collections().collections}
        if self._collection in existing:
            current = existing[self._collection].config.params.vectors
            current_dim = current.size if hasattr(current, "size") else None
            if current_dim != dim:
                logger.warning(
                    "Qdrant collection %r has dim %s, expected %s; recreating",
                    self._collection, current_dim, dim,
                )
                self._client.recreate_collection(
                    collection_name=self._collection,
                    vectors_config=models.VectorParams(
                        size=dim, distance=models.Distance.COSINE
                    ),
                )
            return
        self._client.create_collection(
            collection_name=self._collection,
            vectors_config=models.VectorParams(
                size=dim, distance=models.Distance.COSINE
            ),
        )

    def add(
        self,
        doc_id: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict | None = None,
    ) -> None:
        from qdrant_client import models

        if embeddings:
            self._ensure_collection(len(embeddings[0]))

        meta = metadata or {}
        points = [
            models.PointStruct(
                id=int(uuid.uuid5(_NAMESPACE, f"{doc_id}:{i}").int >> 64),
                vector=emb,
                payload={
                    **meta,
                    "chunk_index": i,
                    "doc_id": doc_id,
                    "document": chunk,
                },
            )
            for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
        ]
        if points:
            self._client.upsert(collection_name=self._collection, points=points)

    def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        where: dict | None = None,
        score_threshold: float | None = None,
    ) -> list[dict]:
        from qdrant_client import models

        must: list[Any] = []
        for key, condition in (where or {}).items():
            if isinstance(condition, dict) and "$in" in condition:
                must.append(models.FieldCondition(
                    key=key, match=models.MatchAny(any=list(condition["$in"]))
                ))
            else:
                must.append(models.FieldCondition(
                    key=key, match=models.MatchValue(value=condition)
                ))

        response = self._client.query_points(
            collection_name=self._collection,
            query=list(embedding),
            limit=top_k,
            query_filter=models.Filter(must=must) if must else None,
            with_payload=True,
        )

        output: list[dict] = []
        for point in response.points:
            score = point.score if point.score is not None else 0.0
            if score_threshold is not None and score < score_threshold:
                continue
            payload = dict(point.payload or {})
            document = payload.pop("document", "")
            output.append({
                "id": str(point.id),
                "document": document,
                "metadata": payload,
                "score": score,
            })
        return output

    def delete(self, doc_id: str) -> None:
        from qdrant_client import models

        self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(filter=models.Filter(must=[
                models.FieldCondition(
                    key="doc_id", match=models.MatchValue(value=doc_id)
                )
            ])),
        )

    def count(self) -> int:
        return self._client.count(collection_name=self._collection, exact=True).count
