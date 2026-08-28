"""Retrieval: embed the query and pull the best chunks from the store."""

from __future__ import annotations

from time import perf_counter
from typing import Any

from protoprompt.events import EventDispatcher, EventSink, RetrieveEvent, dispatch, elapsed_ms, new_trace_id, scope_id
from protoprompt.llm import EmbeddingClientProtocol
from protoprompt.rag.indexer import DEFAULT_DOCUMENT_KIND
from protoprompt.rag.reranker import NoOpReranker, RerankerProtocol
from protoprompt.rag.types import RetrievedChunk
from protoprompt.scope import LOGICAL_DOC_ID_KEY, MemoryScope, scoped_doc_id
from protoprompt.store.protocol import StoreProtocol, await_if_needed


class Retriever:
    """Vector search with optional scope, threshold, and reranking.

    Args:
        store: any ``StoreProtocol`` (sync or async).
        llm: embedding-capable client.
        reranker: refines the order after the vector pass
            (default :class:`NoOpReranker`).
        embedding_model: model name passed to :meth:`EmbeddingClientProtocol.embed`.
        kind_field: metadata field used to separate RAG documents from
            other content (e.g. session memory). Set to ``None`` to query
            the whole store unfiltered.
        document_kind: value of ``kind_field`` for RAG documents.
        scope: optional host-controlled tenant/user/thread namespace.
    """

    def __init__(
        self,
        store: StoreProtocol,
        llm: EmbeddingClientProtocol,
        *,
        reranker: RerankerProtocol | None = None,
        embedding_model: str = "nomic-embed-text",
        kind_field: str | None = "kind",
        document_kind: str = DEFAULT_DOCUMENT_KIND,
        scope: MemoryScope | None = None,
        event_sink: EventSink | EventDispatcher | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._reranker: RerankerProtocol = reranker or NoOpReranker()
        self._embedding_model = embedding_model
        self._kind_field = kind_field
        self._document_kind = document_kind
        self._scope = scope
        self._event_sink = event_sink

    @property
    def scope(self) -> MemoryScope | None:
        """The host-controlled scope pinned to this retriever."""
        return self._scope

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        doc_ids: list[str | int] | None = None,
        score_threshold: float | None = None,
        trace_id: str | None = None,
    ) -> list[RetrievedChunk]:
        """Embed ``query`` and retrieve the best-matching chunks."""
        embedding = (await self._llm.embed([query], model=self._embedding_model))[0]
        return await self.retrieve_embedded(
            embedding,
            query_text=query,
            top_k=top_k,
            doc_ids=doc_ids,
            score_threshold=score_threshold,
            trace_id=trace_id,
        )

    async def retrieve_embedded(
        self,
        embedding: list[float],
        *,
        query_text: str = "",
        top_k: int = 5,
        doc_ids: list[str | int] | None = None,
        score_threshold: float | None = None,
        trace_id: str | None = None,
    ) -> list[RetrievedChunk]:
        """Retrieve using a precomputed ``embedding`` (avoids re-embedding)."""
        started_at = perf_counter()
        operation_trace_id = trace_id or new_trace_id()
        # ``None`` means search all documents; an explicit empty scope means
        # search nothing. Treating [] as an unfiltered query can leak session
        # memory into the RAG channel.
        if doc_ids == []:
            self._emit_retrieve(
                operation_trace_id,
                started_at,
                top_k=top_k,
                hit_count=0,
                doc_ids=doc_ids,
                score_threshold=score_threshold,
            )
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
        self._emit_retrieve(
            operation_trace_id,
            started_at,
            top_k=top_k,
            hit_count=len(chunks),
            doc_ids=doc_ids,
            score_threshold=score_threshold,
        )
        return chunks

    def _emit_retrieve(
        self,
        trace_id: str,
        started_at: float,
        *,
        top_k: int,
        hit_count: int,
        doc_ids: list[str | int] | None,
        score_threshold: float | None,
    ) -> None:
        dispatch(self._event_sink, RetrieveEvent(
            action="completed",
            trace_id=trace_id,
            scope_id=scope_id(self._scope),
            duration_ms=elapsed_ms(started_at),
            attributes={
                "channel": "rag",
                "top_k": top_k,
                "hit_count": hit_count,
                "doc_filter_count": None if doc_ids is None else len(doc_ids),
                "threshold_applied": score_threshold is not None,
            },
        ))

    def _build_where(self, doc_ids: list[str | int] | None) -> dict | None:
        where: dict = {}
        if doc_ids is not None:
            ids = [scoped_doc_id(d, self._scope) for d in doc_ids]
            where["doc_id"] = {"$in": ids} if len(ids) > 1 else ids[0]
        elif self._kind_field is not None:
            where[self._kind_field] = self._document_kind
        if self._scope is not None:
            where = self._scope.merge_where(where)
        return where or None

    @staticmethod
    def _to_chunk(hit: dict) -> RetrievedChunk:
        meta = dict(hit.get("metadata") or {})
        logical_doc_id = meta.get(LOGICAL_DOC_ID_KEY, meta.get("doc_id", ""))
        if LOGICAL_DOC_ID_KEY in meta:
            meta["doc_id"] = logical_doc_id
        score = hit.get("score")
        if score is None and "distance" in hit:
            score = 1.0 - float(hit["distance"])  # cosine distance → similarity
        return RetrievedChunk(
            doc_id=str(logical_doc_id),
            index=int(meta.get("chunk_index", 0)),
            text=hit.get("document", ""),
            score=float(score) if score is not None else 0.0,
            metadata=meta,
        )
