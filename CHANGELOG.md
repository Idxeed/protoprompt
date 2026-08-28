# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-08-28

### Added
- RAG engine (`protoprompt.rag`):
  - `DocumentIndexer` — chunk → embed → index in one call, tagged
    `kind="document"`.
  - Pluggable chunkers: `FixedSizeChunker`, `ParagraphChunker`,
    `TokenChunker`.
  - `Retriever` — vector search with `score_threshold`, optional
    `doc_ids` scope, "search all" mode, and `RetrievedChunk` provenance.
  - Re-ranking: `RerankerProtocol` with `NoOpReranker` (default) and
    `LLMReranker` (model-driven ordering with safe fallback).
  - `ContextInput` gains `score_threshold`; `doc_ids=None` now means
    "search the whole store"; `ContextOutput.rag_chunks` carries
    structured provenance alongside `rag_blocks`.
  - `Pipeline` session memory is now tagged `kind="session"` so RAG
    search-all does not mix it with documents.
- Cross-session profile engine (`protoprompt.profile`):
  - `ProfileProtocol` sources — `LLMProfileSource` (one retry + rule
    fallback), `RuleProfileSource` (zero-LLM heuristics),
    `CompositeProfileSource`.
  - Incremental model: `Signal`, `FactOp`, `ProfileDelta`, typed
    `Traits`/`Preferences`, and a versioned `UserProfile` with an open
    `facts` map and `updated_at`/`version`/`source` bookkeeping.
  - `ProfileManager` (load → extract → merge → persist) with
    `update`/`get`/`reset`/`delete`.
  - `InMemoryProfileStore` and `SqliteProfileStore`, plus async helpers
    (`AsyncInMemoryProfileStore`, `as_async_profile`).
  - Defensive JSON codec (`parse_profile_json`, `coerce_profile`,
    `normalize_enum`) handling fenced/embedded JSON and RU→EN enum
    labels; canonical schema in `profile/schema.json`.
  - `render()` and localized section headers (`protoprompt.i18n`).
- Scoped, encrypted secret storage (`protoprompt.secrets`, `[secrets]`):
  - `KeyProvider` with `KeyringKeyProvider` (OS keychain + fallback),
    `EnvKeyProvider`, `FileKeyProvider`.
  - `EncryptedSqliteSecretStore` — per-entry Fernet encryption, scope
    isolation (`user:project`), TTL, key rotation.
  - `SecretAccess.execute()` — scope-pinned agent access through immutable,
    host-registered operations, so credentials stay outside model-visible
    tool results.
- Shared significance scoring (`protoprompt.memory`): `MemoryScorer` and
  `ScorerWeights` extracted from the agent package so the profile engine
  can reuse them without importing `protoprompt.agent`.

### Changed
- `ContextInput` gains `profile` (structured `UserProfile`) and
  `language`; both context builders now localize the profile/session
  section headers via `protoprompt.i18n`.
- Profile persistence uses optimistic version checks, preventing concurrent
  managers from silently overwriting one another.
- Secret-key rotation records a recoverable pending state and preserves the
  original Fernet timestamp/TTL across re-encryption.

### Deprecated
- `ProfileBuilder` — superseded by `ProfileManager` with
  `LLMProfileSource` (or `RuleProfileSource`). Still functional, emits a
  `DeprecationWarning`.
- `SecretAccess.grant()` for agent-facing use. Trusted hosts may migrate to
  registered `execute()` operations without exposing plaintext to the model.

### Fixed
- Include the canonical profile JSON schema in wheels and source archives.
- Count the fully assembled context, including section headers and separators,
  so `TokenBudgetedContextBuilder` enforces its advertised hard ceiling.
- Treat `doc_ids=[]` as an empty retrieval scope; `None` remains search-all.
- Reject mismatched profile signal owners and embedding-count mismatches.

## [0.2.0] - 2026-08-24

### Added
- `build_messages()` on both context builders: returns an OpenAI-style
  message list (system prompt + history + user message) ready to send.
  The budgeted builder trims history oldest-first into the remaining
  token budget; the newest user message is always kept. New
  `BudgetReport.history_kept`/`history_tokens` fields report the trim.
- Async store support: new `AsyncStoreProtocol`, drop-in
  `AsyncInMemStore`, and an `AsyncStoreWrapper`/`as_async()` helper that
  offloads sync stores to worker threads. Builders and `Pipeline`
  accept sync and async stores interchangeably.
- `SqliteStore` — persistent, zero-dependency vector store in the core
  package.
- Embedding cache: `EmbeddingCache` protocol, LRU
  `InMemoryEmbeddingCache`, and a `CachedLLMClient` decorator that
  batches only cache misses while preserving input order.
- Observability hooks: `ContextHooks` (section used / block dropped /
  build done) and `PipelineHooks` (skip/before/after compress). Hook
  exceptions are logged and swallowed.
- `protoprompt.integrations` package with lazily-imported adapters:
  - `OpenAIClient` (`[openai]`) — official SDK, gateway-friendly.
  - `OllamaClient` (`[ollama]`) — native `/api/chat`, `/api/embed`.
  - `HttpxLLMClient` (`[http]`) — any OpenAI-compatible REST endpoint,
    mockable via `httpx.MockTransport`.
  - `QdrantStore` (`[qdrant]`) — server, embedded local, or in-memory.
  - `SentenceTransformersClient` (`[local]`) and `FastEmbedClient`
    (`[fastembed]`) for API-free embeddings.
- Runnable recipes under `examples/`: offline session-memory demo,
  Ollama RAG, budgeted OpenAI chat, local embeddings.

### Changed
- `ContextInput.doc_ids` now accepts strings as well as ints.
- Store results may include a similarity `score` key (Sqlite/Qdrant).

### Fixed
- Chroma integration tests now skip cleanly when the `chroma` extra is
  not installed (`pytest.importorskip`) instead of erroring.

## [0.1.0] - 2026-07-10

### Added
- `protoprompt.tokens` subpackage with pluggable `TokenCounter` protocol
  and a regex-based default implementation. Optional `tiktoken` adapter.
- `LLMSummaryStrategy` for LLM-based session compression with
  `HeuristicStrategy` fallback on failure.
- `TokenBudgetedContextBuilder` with priority-based greedy token allocation
  and `BudgetReport` observability.
- Real ChromaDB integration tests gated on the `chroma` extra.
- MkDocs Material documentation under `docs/`.
- GitHub Actions CI workflow across Python 3.11-3.13.

### Changed
- `ContextBuilder` now embeds the query once and reuses it for RAG and
  session retrieval (was: embedded twice).
- `InMemStore` supports filtering on any metadata field, not only `doc_id`.
- `StoreProtocol.query` accepts an optional `score_threshold`.
- `Pipeline.compress_and_store` writes to a `_new` doc_id first, then deletes
  the old one, to avoid data loss on crash between steps.
- `ProfileBuilder` logs the original exception instead of silently returning
  an empty profile.
- `HeuristicStrategy` honours a `min_messages` threshold.

### Fixed
- Public re-exports in `protoprompt.profile`, `protoprompt.session`,
  `protoprompt.store` so IDEs and type checkers see the full API.
