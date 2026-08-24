# protoprompt 0.2.0

The integrations release: bring your own model, engine, or store.
Core stays zero-dependency; every adapter is one optional extra away.

## What's new

- **`build_messages()`** — both builders now return an OpenAI-style
  message list ready to send: system prompt + history + user message.
  The token-budgeted builder trims history oldest-first into whatever
  budget remains after RAG/session/profile assembly.
- **Async stores** — `AsyncStoreProtocol`, drop-in `AsyncInMemStore`,
  and `as_async()` wrapping any sync store with worker-thread dispatch,
  so ChromaDB/Qdrant/SQLite never stall the event loop. Builders and
  `Pipeline` accept both flavours transparently.
- **`SqliteStore`** — persistent vector store in the core package,
  standard library only.
- **Embedding cache** — `CachedLLMClient` + LRU `InMemoryEmbeddingCache`;
  partial cache misses still batch into a single upstream call.
- **Observability hooks** — `ContextHooks` and `PipelineHooks` for
  section/block/compression events; failures are logged, never fatal.
- **Integrations** (`protoprompt.integrations`, lazily imported):
  - `OpenAIClient` — official SDK, works with gateways via `base_url`.
  - `OllamaClient` — native `/api/chat` + `/api/embed`.
  - `HttpxLLMClient` — any OpenAI-compatible REST endpoint
    (LM Studio, vLLM, llama.cpp server), mockable in tests.
  - `QdrantStore` — server, embedded local, or in-memory modes.
  - `SentenceTransformersClient`, `FastEmbedClient` — API-free local
    embeddings.

## Install

```bash
pip install protoprompt                       # core, zero deps
pip install "protoprompt[ollama]"             # native Ollama client
pip install "protoprompt[openai]"             # OpenAI SDK
pip install "protoprompt[qdrant]"             # Qdrant store
```

Runnable recipes: see [`examples/`](examples/) — including a fully
offline demo (`python examples/session_memory.py`).

## Compatibility

- Python 3.11–3.13+ (CI matrix); tested locally on 3.14
- Optional extras: `chroma`, `tiktoken`, `http`, `ollama`, `openai`,
  `qdrant`, `fastembed`, `local`
- Public API additions only; no breaking changes since 0.1.0

## Verification

- 89 unit tests passing (+ integration tests gated on extras)
- Integration clients covered offline via `httpx.MockTransport`
- Chroma tests skip cleanly when the extra is absent

## Links

- Source: https://github.com/Idxeed/protoprompt
- Docs: https://idxeed.github.io/protoprompt/
- Issues: https://github.com/Idxeed/protoprompt/issues
- Changelog: https://github.com/Idxeed/protoprompt/blob/main/CHANGELOG.md
