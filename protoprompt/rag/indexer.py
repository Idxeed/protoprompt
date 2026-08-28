"""Document ingestion: chunk → embed → store.

:class:`DocumentIndexer` is the missing "load" side of RAG. It turns a
document into indexed chunks in one call, tagging them with a ``kind``
metadata marker (default ``document``) so retrieval can later tell RAG
documents apart from session memory.
"""

from __future__ import annotations

from typing import Any

from protoprompt.llm import LLMClientProtocol
from protoprompt.rag.chunker import ChunkerProtocol, FixedSizeChunker
from protoprompt.rag.types import Document
from protoprompt.store.protocol import StoreProtocol, await_if_needed

DEFAULT_DOCUMENT_KIND = "document"


class DocumentIndexer:
    """Chunk, embed, and persist documents into a vector store.

    Args:
        store: any ``StoreProtocol`` (sync or async).
        llm: embedding-capable client.
        chunker: splitting strategy (default :class:`FixedSizeChunker`).
        embedding_model: model name passed to :meth:`LLMClientProtocol.embed`.
        kind: the metadata marker applied to every indexed chunk.
    """

    def __init__(
        self,
        store: StoreProtocol,
        llm: LLMClientProtocol,
        *,
        chunker: ChunkerProtocol | None = None,
        embedding_model: str = "nomic-embed-text",
        kind: str = DEFAULT_DOCUMENT_KIND,
    ) -> None:
        self._store = store
        self._llm = llm
        self._chunker = chunker or FixedSizeChunker()
        self._embedding_model = embedding_model
        self._kind = kind

    async def index(
        self,
        doc_id: str,
        text: str,
        metadata: dict | None = None,
    ) -> int:
        """Chunk and index a single document; returns the chunk count."""
        chunks = self._chunker.split(text)
        if not chunks:
            return 0
        embeddings = await self._llm.embed(chunks, model=self._embedding_model)
        if len(embeddings) != len(chunks):
            raise ValueError(
                "embedding client returned "
                f"{len(embeddings)} vectors for {len(chunks)} chunks"
            )
        meta = dict(metadata or {})
        meta["kind"] = self._kind
        await await_if_needed(self._store.add(doc_id, chunks, embeddings, meta))
        return len(chunks)

    async def index_documents(self, documents: list[Document]) -> dict[str, int]:
        """Index several documents; returns ``{doc_id: chunk_count}``."""
        result: dict[str, int] = {}
        for doc in documents:
            result[doc.doc_id] = await self.index(
                doc.doc_id, doc.text, doc.metadata
            )
        return result
