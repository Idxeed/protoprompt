# RAG retrieval

RAG (retrieval-augmented generation) finds the most relevant pieces of
your documents by meaning and feeds them into the prompt. In
`protoprompt` it is a dedicated layer, `protoprompt.rag`, with two halves:

- **load** — split a document into chunks and index them;
- **read** — find the best chunks for a query (with provenance).

## Loading: DocumentIndexer

Chunking and embedding used to be manual. Now it is one call:

```python
from protoprompt.rag import DocumentIndexer, FixedSizeChunker

indexer = DocumentIndexer(store, llm, chunker=FixedSizeChunker(512))
await indexer.index("handbook", "Paris is the capital of France. ...")
```

`DocumentIndexer` splits the text into chunks, embeds them, and stores
them, tagging each chunk `kind="document"` so search-all never mixes
documents with session memory.

### Chunkers

| Chunker             | Strategy                                         |
|---------------------|--------------------------------------------------|
| `FixedSizeChunker`  | Fixed-length character windows with overlap      |
| `ParagraphChunker`  | Blank-line split, over-long paragraphs re-cut    |
| `TokenChunker`      | Accumulate words up to a token budget (needs a `TokenCounter`) |

All implement `ChunkerProtocol.split(text) -> list[str]`.

## Reading: Retriever

```python
from protoprompt.rag import Retriever

retriever = Retriever(store, llm)
chunks = await retriever.retrieve(
    "What is the capital of France?",
    top_k=5,
    doc_ids=["handbook"],      # or None = whole store
    score_threshold=0.5,       # drop weak matches
)
```

Returns `RetrievedChunk` — the text **plus** provenance: `doc_id`,
`index`, `score`. A UI can show where each block came from.

- `doc_ids=[...]` — search only those documents;
- `doc_ids=None` — search the whole store (only `kind="document"`);
- `score_threshold` — drop chunks below the similarity threshold.

### Re-ranking

Vector top-k is a cheap first pass. `RerankerProtocol` refines the order:

- `NoOpReranker` (default) — keeps the vector order, zero cost;
- `LLMReranker` — asks the model to order the candidates; on failure it
  silently returns the original order.

```python
from protoprompt.rag import Retriever, LLMReranker

retriever = Retriever(store, llm, reranker=LLMReranker(llm))
```

## In the context builder

`ContextBuilder` uses `Retriever` internally — `ContextInput` just gained
new fields:

```python
from protoprompt import ContextBuilder, ContextInput

out = await builder.build(ContextInput(
    query="What is the capital of France?",
    doc_ids=["handbook"],        # None = whole store
    score_threshold=0.5,
))
print(out.rag_chunks)            # [RetrievedChunk(...), ...] with provenance
```

`out.rag_blocks` remains for compatibility (list of plain texts).
