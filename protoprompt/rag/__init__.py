"""protoprompt.rag — document ingestion and retrieval.

The RAG layer has two halves:

- **load**: :class:`DocumentIndexer` chunks a document, embeds the chunks,
  and indexes them into a vector store (tagged ``kind="document"``);
- **read**: :class:`Retriever` embeds a query and pulls the best chunks,
  optionally scoped by ``doc_ids``, filtered by ``score_threshold``, and
  re-ranked.

    from protoprompt.rag import DocumentIndexer, Retriever

    indexer = DocumentIndexer(store, llm)
    await indexer.index("handbook", "Paris is the capital of France.")

    retriever = Retriever(store, llm)
    chunks = await retriever.retrieve("What is the capital of France?")
"""

from protoprompt.rag.chunker import (
    ChunkerProtocol,
    FixedSizeChunker,
    ParagraphChunker,
    TokenChunker,
)
from protoprompt.rag.indexer import DEFAULT_DOCUMENT_KIND, DocumentIndexer
from protoprompt.rag.reranker import (
    LLMReranker,
    NoOpReranker,
    RerankerProtocol,
)
from protoprompt.rag.retriever import Retriever
from protoprompt.rag.types import Document, RetrievedChunk

__all__ = [
    "Document",
    "RetrievedChunk",
    "ChunkerProtocol",
    "FixedSizeChunker",
    "ParagraphChunker",
    "TokenChunker",
    "DocumentIndexer",
    "DEFAULT_DOCUMENT_KIND",
    "RerankerProtocol",
    "NoOpReranker",
    "LLMReranker",
    "Retriever",
]
