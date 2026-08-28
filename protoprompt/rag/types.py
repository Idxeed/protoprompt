"""Data types for the RAG (retrieval) layer.

A :class:`Document` is what you ingest; a :class:`RetrievedChunk` is what
you get back — the same text plus its provenance (which document, which
chunk, how similar), so a UI can show *where* each block came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Document:
    """A single document to chunk and index."""

    doc_id: str
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class RetrievedChunk:
    """One chunk returned by retrieval, with provenance attached."""

    doc_id: str
    index: int
    text: str
    score: float
    metadata: dict = field(default_factory=dict)
