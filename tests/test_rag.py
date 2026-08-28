from __future__ import annotations

import pytest

from protoprompt import ContextBuilder, ContextInput
from protoprompt.rag import (
    DocumentIndexer,
    FixedSizeChunker,
    LLMReranker,
    NoOpReranker,
    ParagraphChunker,
    Retriever,
    TokenChunker,
)
from protoprompt.rag.reranker import _parse_indices
from protoprompt.rag.types import RetrievedChunk
from protoprompt.store.memory import InMemStore

from _mocks import MockLLM


# ── chunkers ─────────────────────────────────────────────────────


def test_fixed_size_chunker_with_overlap():
    c = FixedSizeChunker(chunk_size=10, overlap=2)
    assert c.split("abcdefghijklmnop") == ["abcdefghij", "ijklmnop"]


def test_fixed_size_chunker_single_chunk():
    c = FixedSizeChunker(chunk_size=100)
    assert c.split("short") == ["short"]


def test_fixed_size_chunker_rejects_bad_overlap():
    with pytest.raises(ValueError):
        FixedSizeChunker(chunk_size=10, overlap=10)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chunk_size": 0, "overlap": 0},
        {"chunk_size": 10, "overlap": -1},
    ],
)
def test_fixed_size_chunker_rejects_non_positive_ranges(kwargs):
    with pytest.raises(ValueError):
        FixedSizeChunker(**kwargs)


def test_paragraph_chunker_splits_on_blank_lines():
    c = ParagraphChunker(max_chars=50)
    assert c.split("para one\n\npara two") == ["para one", "para two"]


def test_token_chunker_respects_budget():
    c = TokenChunker(chunk_tokens=3)
    assert c.split("one two three four five six") == ["one two three", "four five six"]


def test_token_chunker_rejects_overlap_larger_than_budget():
    with pytest.raises(ValueError):
        TokenChunker(chunk_tokens=3, overlap_words=3)


# ── reranker ─────────────────────────────────────────────────────


def test_parse_indices():
    assert _parse_indices("3, 1, 2", 4) == [3, 1, 2]
    assert _parse_indices("best is 0 then 2", 4) == [0, 2]
    assert _parse_indices("no numbers", 4) == []


@pytest.mark.asyncio
async def test_noop_reranker_keeps_order():
    chunks = [RetrievedChunk("d", i, f"c{i}", 1.0) for i in range(3)]
    assert await NoOpReranker().rerank("q", chunks) == chunks


class _RankLLM:
    def __init__(self, answer: str = "2, 0, 1", fail: bool = False) -> None:
        self._answer = answer
        self._fail = fail

    async def chat(self, messages, model="", **options):
        if self._fail:
            raise RuntimeError("boom")
        return self._answer

    async def embed(self, texts, model=""):
        return [[0.0] for _ in texts]


@pytest.mark.asyncio
async def test_llm_reranker_reorders():
    chunks = [RetrievedChunk("d", i, f"c{i}", 1.0) for i in range(3)]
    out = await LLMReranker(_RankLLM("2, 0, 1")).rerank("q", chunks)
    assert [c.index for c in out] == [2, 0, 1]


@pytest.mark.asyncio
async def test_llm_reranker_falls_back_on_bad_output():
    chunks = [RetrievedChunk("d", i, f"c{i}", 1.0) for i in range(3)]
    out = await LLMReranker(_RankLLM("garbage")).rerank("q", chunks)
    assert [c.index for c in out] == [0, 1, 2]


@pytest.mark.asyncio
async def test_llm_reranker_falls_back_on_chat_failure():
    chunks = [RetrievedChunk("d", i, f"c{i}", 1.0) for i in range(3)]
    out = await LLMReranker(_RankLLM(fail=True)).rerank("q", chunks)
    assert [c.index for c in out] == [0, 1, 2]


# ── indexer + retriever ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_and_retrieve_with_provenance():
    store = InMemStore()
    llm = MockLLM(embed_dim=16)
    indexer = DocumentIndexer(store, llm, chunker=FixedSizeChunker(chunk_size=12, overlap=0))
    n = await indexer.index("doc1", "hello world this is a test document")
    assert n > 0

    retriever = Retriever(store, llm)
    chunks = await retriever.retrieve("hello", top_k=10)
    assert chunks
    for c in chunks:
        assert c.doc_id == "doc1"
        assert c.metadata["kind"] == "document"
        assert isinstance(c.index, int)


@pytest.mark.asyncio
async def test_retrieve_filters_by_doc_ids():
    store = InMemStore()
    llm = MockLLM(embed_dim=16)
    indexer = DocumentIndexer(store, llm, chunker=FixedSizeChunker(chunk_size=20, overlap=0))
    await indexer.index("a", "apple is a red fruit and very sweet")
    await indexer.index("b", "banana is a yellow fruit")

    retriever = Retriever(store, llm)
    chunks = await retriever.retrieve("apple", top_k=10, doc_ids=["a"])
    assert chunks
    assert all(c.doc_id == "a" for c in chunks)


@pytest.mark.asyncio
async def test_retrieve_empty_doc_scope_returns_nothing():
    store = InMemStore()
    llm = MockLLM(embed_dim=16)
    await DocumentIndexer(store, llm).index("a", "a document")
    store.add("session_c1", ["private session"], [[1.0] * 16], {"kind": "session"})

    chunks = await Retriever(store, llm).retrieve("anything", doc_ids=[])

    assert chunks == []


@pytest.mark.asyncio
async def test_retrieve_search_all_excludes_session_kind():
    store = InMemStore()
    llm = MockLLM(embed_dim=16)
    indexer = DocumentIndexer(store, llm, chunker=FixedSizeChunker(chunk_size=40, overlap=0))
    await indexer.index("kb", "Paris is the capital of France and is very big")

    embs = await llm.embed(["the user asked about Paris today"])
    store.add(
        "session_c1", ["the user asked about Paris today"], embs,
        {"kind": "session", "chat_id": "c1"},
    )

    retriever = Retriever(store, llm)
    chunks = await retriever.retrieve("Paris", top_k=10)
    assert chunks
    assert all(c.metadata.get("kind") == "document" for c in chunks)


@pytest.mark.asyncio
async def test_retrieve_score_threshold_and_provenance():
    store = InMemStore()
    store.add("kb", ["alpha"], [[1.0, 0.0]], {"kind": "document"})

    retriever = Retriever(store, MockLLM(embed_dim=2))
    got = await retriever.retrieve_embedded([1.0, 0.0], top_k=5)
    assert len(got) == 1
    assert got[0].doc_id == "kb"
    assert got[0].index == 0
    assert got[0].text == "alpha"
    assert got[0].score == pytest.approx(1.0)

    # threshold above the only score → nothing
    assert await retriever.retrieve_embedded(
        [1.0, 0.0], top_k=5, score_threshold=1.1
    ) == []


@pytest.mark.asyncio
async def test_context_builder_returns_structured_rag_chunks():
    store = InMemStore()
    llm = MockLLM(embed_dim=16)
    indexer = DocumentIndexer(
        store, llm, chunker=FixedSizeChunker(chunk_size=40, overlap=0)
    )
    await indexer.index("kb", "Paris is the capital of France")

    builder = ContextBuilder(store, llm)
    out = await builder.build(ContextInput(query="Paris", system_prompt="sys"))

    assert out.rag_chunks
    assert out.rag_chunks[0].doc_id == "kb"
    assert out.rag_blocks == [c.text for c in out.rag_chunks]


@pytest.mark.asyncio
async def test_indexer_rejects_embedding_count_mismatch():
    class ShortEmbeddingLLM(MockLLM):
        async def embed(self, texts, model=""):
            return []

    with pytest.raises(ValueError, match="vectors for"):
        await DocumentIndexer(InMemStore(), ShortEmbeddingLLM()).index("d", "text")
