"""Retrieval: embed the query and pull the best chunks from the store."""

from __future__ import annotations

from typing import Any

from protoprompt.llm import LLMClientProtocol
from protoprompt.rag.indexer import DEFAULT_DOCUMENT_KIND
from protoprompt.rag.reranker import NoOpReranker, RerankerProtocol
from protoprompt.rag.types import RetrievedChunk
from protoprompt.store.protocol import StoreProtocol, await_if_needed


class Retriever:
    """Vector search with optional scope, threshold, and reranking.

    Args:
        store: any ``StoreProtocol`` (sync or async).
        llm: embedding-capable client.
        reranker: refines the order after the vector pass
            (default :class:`NoOpReranker`).
        embedding_model: model name passed to :meth:`LLMClientProtocol.embed`.
        kind_field: metadata field used to separate RAG documents from
            other content (e.g. session memory). Set to ``None`` to query
            the whole store unfiltered.
        document_kind: value of ``kind_field`` for RAG documents.
    """

    def __init__(
        self,
        store: StoreProtocol,
        llm: LLMClientProtocol,
        *,
        reranker: RerankerProtocol | None = None,
        embedding_model: str = "nomic-embed-text",
        kind_field: str | None = "kind",
        document_kind: str = DEFAULT_DOCUMENT_KIND,
    ) -> None:
        self._store = store
        self._llm = llm
        self._reranker: RerankerProtocol = reranker or NoOpReranker()
        self._embedding_model = embedding_model
        self._kind_field = kind_field
        self._document_kind = document_kind

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        doc_ids: list[str | int] | None = None,
        score_threshold: float | None = None,
    ) -> list[RetrievedChunk]:
        """Embed ``query`` and retrieve the best-matching chunks."""
        embedding = (await self._llm.embed([query], model=self._embedding_model))[0]
        return await self.retrieve_embedded(
            embedding,
            query_text=query,
            top_k=top_k,
            doc_ids=doc_ids,
            score_threshold=score_threshold,
        )

    async def retrieve_embedded(
        self,
        embedding: list[float],
        *,
        query_text: str = "",
        top_k: int = 5,
        doc_ids: list[str | int] | None = None,
        score_threshold: float | None = None,
    ) -> list[RetrievedChunk]:
        """Retrieve using a precomputed ``embedding`` (avoids re-embedding)."""
        # ``None`` means search all documents; an explicit empty scope means
        # search nothing. Treating [] as an unfiltered query can leak session
        # memory into the RAG channel.
        if doc_ids == []:
            return []
        where = self._build_where(doc_ids)
        hits = await await_if_needed(
            self._store.query(
                embedding,
                top_k=top_k,
                where=where,
                score_threshold=score_threshold,
            )
        )
        chunks = [self._to_chunk(hit) for hit in hits]
        if self._reranker is not None and len(chunks) > 1 and query_text:
            chunks = await self._reranker.rerank(query_text, chunks)
        return chunks

    def _build_where(self, doc_ids: list[str | int] | None) -> dict | None:
        if doc_ids is not None:
            ids = [str(d) for d in doc_ids]
            return {"doc_id": {"$in": ids}} if len(ids) > 1 else {"doc_id": ids[0]}
        if self._kind_field is not None:
            return {self._kind_field: self._document_kind}
        return None

    @staticmethod
    def _to_chunk(hit: dict) -> RetrievedChunk:
        meta = hit.get("metadata") or {}
        score = hit.get("score")
        if score is None and "distance" in hit:
            score = 1.0 - float(hit["distance"])  # cosine distance → similarity
        return RetrievedChunk(
            doc_id=str(meta.get("doc_id", "")),
            index=int(meta.get("chunk_index", 0)),
            text=hit.get("document", ""),
            score=float(score) if score is not None else 0.0,
            metadata=dict(meta),
        )
