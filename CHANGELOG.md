# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.11.0] - 2026-08-30

### Added
- Experimental `LedgerContextComposer` in `protoprompt.ledger.recall`: one
  host-owned opt-in bridge from a scope-pinned, admitted Ledger recall plan to
  an immutable, exactly budgeted provider request. It emits a transient
  `LedgerComposedRequest` plus a content-free `LedgerCompositionReceipt`.
- Additive `ContextDataLaneReceipt` / `ContextPlan.data_lanes` metadata for
  mandatory host request lanes. The exact whole-request
  `ContextRequestReceipt.input_tokens` remains authoritative.
- `LedgerRecallPolicy.admission_safe_default()` and the explicit
  `require_admission_audit` policy flag. The composed path excludes raw
  `unknown` and migrated `legacy_unknown` records while preserving standalone
  v0.9 recall compatibility.
- Frozen offline Memory Benchmark v0.2 for Ledger request composition: strict
  provenance exclusion, complete lane budget, tool dependency ordering,
  payload-free explanation, and an event-gated stale-forget race.

### Changed
- `TokenBudgetedContextBuilder` internally reserves an immutable host data
  prefix before optional context/history allocation. The ordinary public
  `plan_messages()` API keeps its existing message shape.
- A composed lane is rendered as a fixed content-free system guard followed by
  a user-role JSON data message after generated system context and before
  history. It never contributes raw Ledger text to the generated system prompt.
- Budget diagnostics now report `ledger_data` only when the mandatory lane is
  what makes an otherwise valid request overflow; independently oversized
  final input and tool dependencies retain their actionable sections.

### Security
- Composer construction binds exact `MemoryScope` and `TokenCounter` identity
  and rejects recall policies without admission evidence. Raw Ledger payload is
  absent from public receipts and `explain()` output.
- Final Ledger lifecycle validation runs after asynchronous context work and
  exact provider-message accounting. A selected record that is forgotten,
  retracted, expired, erased, or changed before that boundary fails closed with
  `StaleMemoryPlanError`.
- The composer is not auto-wired into `pp-agent`, `pp-ollama-chat`, legacy
  `MemoryService`, profile/session/vector paths, or a general arbitrary-host-
  message API.

## [0.10.0] - 2026-08-30

### Added
- Experimental host-owned admission boundary for the SQLite Memory Ledger:
  `MemoryReviewGate`, conservative versioned `MemoryAdmissionPolicy`, sealed
  in-process `MemoryReview`, fixed-origin narrow ingress, closed
  `MemoryOrigin` categories, and content-free `MemoryAdmissionAudit` receipts.
  Gate decisions map explicitly to allow/active, quarantine/quarantined, or
  reject/forget; model text never receives a lifecycle or trust parameter.
- Ledger schema v5 sidecars for immutable origin provenance and review audits,
  including exact-table/index validation, behavioral SQLite immutability
  guards, controlled hard-erase cascade, dry-run migration, and a read-only
  v4→v5 backfill of live payload rows as `legacy_unknown`.
- Targeted recall validation for concrete v5 origins: an active record must
  carry a fully bound `allow` audit whose event record, revision, reason,
  origin, and command fingerprint match before it enters the bounded recall
  lane.

### Changed
- `MemoryRecord` and explicit Ledger exports now include additive origin and
  admission-audit metadata. Raw `MemoryWriter` candidate APIs remain a
  trusted-host compatibility/cleanup escape hatch with `unknown` origin.
- Raw `MemoryWriter.confirm()` rejects candidates created through a concrete
  v5 ingress. They must be confirmed through the matching sealed review gate.
- Admission action timestamps preserve a writer's deterministic test clock,
  while an allow decision also rechecks the Ledger's UTC time inside the final
  SQLite write boundary before activating an expired record.

### Security
- The admission API documents an RPC-only model boundary: only `{content}`
  crosses from model to a trusted host adapter. Gate, writer, review, ingress,
  and Ledger objects are never a Python sandbox or an arbitrary-plugin API.
- Origin and audit sidecars reject direct updates and deletes. Hard erase is
  their sole controlled cascade path and restores the exact guards before its
  transaction commits. Invalid, orphaned, mismatched, or event-forged audit
  rows fail closed during setup/dry-run and before recall.
- Pre-v5 active memories remain recallable as `legacy_unknown` for compatibility
  and have no invented audit. Strict deployments must quarantine and re-admit
  them through a concrete origin before enabling recall.

## [0.9.0] - 2026-08-30

### Added
- Experimental bounded Ledger Recall: `LedgerRecallPlanner` reads only active,
  host-confirmed, still-valid payloads from one scope-pinned `MemoryWriter`;
  ranks locally with deterministic lexical relevance, confidence and recency;
  and packs whole records into a canonical JSON data lane with exact token and
  UTF-8-byte receipts. It performs no LLM, embedding, vector, network, or
  legacy-memory call and does not silently compose data into `ContextPlan` or
  a system prompt.
- Content-free `LedgerRecallPlan.explain()` receipts record the immutable
  policy configuration/fingerprint, token-counter identity, candidate and
  active-read bounds, budget decisions, and selected count without exposing
  task text, record IDs, scope, provenance, or memory content.
- Planner-bound authenticated selection snapshots and a host-controlled clock.
  A plan cannot be replayed through another planner, scope, counter or policy;
  a model/tool caller cannot supply a backdated per-call timestamp.
- Repo-owned launch material and bilingual docs for the new recall data lane,
  including an illustrated Ledger lifecycle asset.

### Changed
- Recall resolution now renders and performs injectable token accounting outside
  SQLite's writer lock, then takes a short final `BEGIN IMMEDIATE` lifecycle
  validation boundary. A concurrent forget/retract that wins makes resolution
  fail closed with `StaleMemoryPlanError` instead of returning stale data.

### Security
- The final recall validation re-checks selected record identity, revision,
  content hash, kind, active lifecycle status and time validity before context
  returns. `TokenCounter` implementations cannot hold or re-enter the Ledger
  write transaction.
- SQLite read and write transaction helpers now roll back on `BaseException`,
  preventing an interrupted internal operation from leaving a connection in an
  open transaction.

## [0.8.0] - 2026-08-30

### Added
- Experimental opt-in SQLite Memory Ledger foundation: host-pinned
  `MemoryWriter`, schema-versioned explicit setup/dry-run/backup, immutable
  content-free lifecycle receipts and a local lifecycle projection, optimistic
  revisions and idempotency keys.
  Candidate memories require an explicit host confirmation before they are
  recallable; scope-local supersede, quarantine, expiry, retraction and
  payload-aware forget/erase operations are available without changing legacy
  vector, profile or session behavior. `forget()` emits a revision-advancing
  `forgotten` receipt; `forget_by_source()` atomically revokes a scoped opaque
  source and blocks its re-ingestion; hard erase redacts dependent event links
  and retains only opaque replay barriers.
- Memory Ledger schema v4 guards: fail-closed exact table/index compatibility,
  reserved object names, rejection of unowned SQLite triggers on ledger tables,
  case-insensitive SQLite-name handling, migration-time scrubbing of legacy
  creation-event fingerprints, and verified append-only event triggers.
- Snapshot-consistent reads for recall, record lookup, and export. Lifecycle
  writes acquire the SQLite write lock before revalidating the schema boundary,
  so a concurrent DDL change cannot slip a payload-copying trigger or a
  restrictive index between validation and mutation.

### Changed
- The ledger documentation now states its strict shared-database boundary:
  external indexes and triggers on ledger tables are unsupported and fail
  closed; `forget()` and `erase()` clear live ledger rows rather than claiming
  physical deletion from SQLite media or backups.

### Security
- Ledger event history is append-only in normal operation. Controlled setup
  migrations may scrub legacy experimental fingerprints, and hard erase may
  redact references from another record's event solely to remove an erased ID.

## [0.7.0] - 2026-08-30

### Added
- Additive `ContextPlan`, `ContextBlockDecision`, and
  `ContextRequestReceipt` contracts for the budgeted builder. `plan()` returns
  an immutable context-only decision snapshot; `plan_messages()` returns an
  immutable, deep-copied provider request plus exact request accounting.
- Content-free, JSON-safe `ContextPlan.explain()` metadata for developer UIs,
  audit trails, and deterministic context-selection regression tests.
- Versioned, network-free Memory Benchmark v0.1 with frozen fixtures,
  deterministic baselines, a `v0.6.1` reference, SQLite cold-reopen and
  scope probes, and a CI verification gate for semantic outcomes.
- `pp-agent` now builds normal, planning, and compaction calls through
  `TokenBudgetedContextBuilder.plan_messages()`. It has separate request and
  memory budgets, forwards the completion reserve to the provider, and keeps
  text-action/tool-result continuations together as mandatory final input.
- Local-only `pp-ollama-chat` reference integration: PDF RAG, append-only
  conversation archive with a crash-safe pending ledger, explicit deletion of
  transcript/vector/file projections, SSE streaming, and a visible
  `ContextPlan` receipt. Each generated Ollama request receives matching
  `num_ctx` and `num_predict` controls.

### Changed
- `TokenBudgetedContextBuilder.build()` now attaches its context-only plan as
  `ContextOutput.plan`. `build_messages()` retains its existing list return
  type and renders the equivalent `plan_messages()` projection.
- README and roadmap now state the focused product direction: reliable agent
  memory under a fixed, explainable context budget.
- `pp-agent` falls back to non-streaming chat when a cached client wraps a
  backend without a real streaming capability instead of failing at runtime.
- The web reference app states the difference between durable retention and
  bounded active context; its deterministic token counter is documented as a
  planner estimate rather than a universal provider tokenizer.

### Security
- Ignore local reference-app data, runtime key/token/pid files, SQLite WAL
  sidecars, and `node_modules` to prevent accidental commits of credentials,
  user documents, vector indexes, or dependency trees.
- Snapshot mutable `ContextInput` selections before asynchronous retrieval, so
  an in-flight caller cannot replace a validated prompt, document filter, or
  session identifier mid-request. Opaque RAG provenance also handles every
  Python string identifier without crashing on an unpaired surrogate.
- Ollama web UI defaults to loopback and refuses a remote Ollama endpoint
  unless `OLLAMA_CHAT_ALLOW_REMOTE=1` is set; remote mode explicitly warns that
  messages and PDF content leave the machine.
- Local PDF ingestion now constrains pypdf compressed-stream expansion before
  extraction; raw PDF/chat bodies are rejected before framework parsing; and
  the POSIX reference-app data directory, SQLite files, and new uploads are
  created with owner-only permissions.

## [0.6.1] - 2026-08-30

### Fixed
- `TokenBudgetedContextBuilder.build_messages()` now reserves the final user
  turn and optional output capacity before retrieval. Its final accounting
  covers rendered system context, retained history, provider message framing,
  and the mandatory turn under one input budget.
- The OpenAI Agents session callback follows the same request-level ceiling;
  an oversized mandatory Agents item is rejected instead of bypassing the
  configured budget.
- Retained Chat Completions and Agents/Responses tool-call histories preserve
  complete call/output graphs (including hosted MCP approvals, interleaved
  program-owned children, streamed shell/tool-search outputs, anonymous
  server-side tool-search pairs, and linked reasoning) or drop them
  atomically, never returning a dangling tool result
  or request through the budgeted API. Input-only Responses controls are not
  replayed as optional history.
- A final tool output reserves its required trailing history graph (including
  ordered anonymous server-side tool-search dependencies) and raises rather
  than dropping that graph when the complete protocol dependency cannot fit.
- Built-in and provider-aware token counters account for structured content,
  tool calls, and other provider-relevant message fields instead of silently
  counting `content` alone.
- `ProviderTokenCounter("openai")` now loads the correct optional tiktoken
  adapter and retains its deterministic fallback when tiktoken is unavailable
  or does not know a model name.
- `InMemoryProfileStore`, `SqliteProfileStore`, and their async variants can
  persist a logical profile id behind an isolated `MemoryScope` physical key,
  while public `UserProfile.user_id` remains logical.

### Security
- Scoped profile reads reject a legacy unscoped record that merely collides
  with a derived scoped key, preventing that record from being exposed through
  the scoped profile API.
- Scoped profile mutators also reject that collision before a reset, delete,
  write, or compare-and-swap operation can overwrite the legacy record.
- `MemoryService` rejects a `ProfileManager` whose host-owned scope is absent
  or differs from the service scope, before any profile read or write.

### Migration
- Existing unscoped profiles intentionally remain unscoped. When enabling
  `MemoryScope` for profiles, copy only the records you explicitly authorize
  into the destination scope; the runtime will not adopt an unscoped profile
  implicitly.
- A non-empty profile `MemoryScope` now requires a store with native
  `supports_profile_scopes=True`. Custom, Redis, and Postgres profile stores
  must add that explicit capability before they can serve scoped profiles.

## [0.6.0] - 2026-08-28

### Added
- Integration foundation:
  - independent `ChatClientProtocol` and `EmbeddingClientProtocol` plus
    backwards-compatible `LLMClientProtocol` and `CompositeLLMClient`;
  - host-controlled `MemoryScope` with tenant/user/thread isolation across
    builders, pipelines, retrieval, persistence, and adapters;
  - dependency-free adapter contract kit in `protoprompt.testing`;
  - typed, trace-correlated observability events with recursive default
    redaction.
- Connectivity adapters and examples for MCP 2.x (stdio and Streamable HTTP),
  OpenAI Agents sessions, LangGraph stores/nodes, and an aiogram Telegram bot
  with deterministic long-dialog comparison and a reproducible demo GIF.
- Production backends:
  - async `PgVectorStore` and optimistic-locking `PostgresProfileStore` with
    explicit schema setup and Docker integration tests;
  - Redis embedding cache, ephemeral session, and profile store with TTL,
    reconnect, and concurrency coverage;
  - OpenTelemetry event sink/runtime plus OTLP/Jaeger recipes.
- Native provider clients for Anthropic Messages, Google GenAI/Vertex chat and
  embeddings, and Amazon Bedrock Converse, including exact opt-in provider token
  counting.
- Native PydanticAI history processing and LlamaIndex memory block; documented
  Google ADK spike and support decision.
- Data and enterprise integrations:
  - bounded local text/Markdown/source/HTML/PDF/DOCX readers with trusted
    provenance and LlamaIndex/Unstructured converters;
  - async Elasticsearch 9 and OpenSearch 3 vector stores with explicit mappings,
    exact metadata filters, stable cosine thresholds, and live-test compose;
  - scoped AWS Secrets Manager and GCP Secret Manager stores with opaque resource
    names, TTL envelopes, provider-native encryption, and opt-in live contracts;
  - authenticated FastAPI memory service recipe, Docker image, and single-replica
    Kubernetes/minikube manifest.
- RU/EN integration guides, migration/rollback notes, maintainer policy, runnable
  examples, and optional dependency extras for every new adapter.

### Changed
- Updated the supported ChromaDB range to `>=1.5,<2`. The public
  `ChromaStore` contract is unchanged, while the extra now installs supported
  binary dependencies on Python 3.13 instead of building legacy
  `chroma-hnswlib` from source.
- Qdrant collection inspection now uses `get_collection()` with current client
  releases. A dimension mismatch fails with an explicit migration error instead
  of silently recreating and destroying the collection.
- Builders and pipelines accept only the model capability they use while old
  full clients remain valid without modification.
- Scope metadata is deterministic and conflict-checked; an omitted/empty scope
  preserves the 0.3 physical storage layout for staged migration.
- Embedding-only clients no longer expose fake `chat()` methods.
- `SecretAccess.execute()` remains the model-facing credential boundary; cloud
  store names no longer expose plaintext tenant or key identifiers.

### Fixed
- PostgreSQL batch inserts now call psycopg 3 `executemany()` through an async
  cursor instead of the connection object; Windows live tests use the selector
  event loop required by psycopg.
- Elasticsearch/OpenSearch adapters now await decorator-wrapped async SDK
  methods, instead of mistaking their coroutine objects for successful results.
- Source distributions now include deployment recipes, integration governance,
  migration guides, and Telegram release media.

### Security
- Typed telemetry denies prompt, document, profile, token, and secret content by
  default.
- Local readers reject URL/URI input, root escapes, unsupported/binary files,
  DOCX external relationships, oversized archives/streams/pages/text, and
  encrypted PDFs without an explicit password.
- FastAPI requires host-provided authorization and scope resolution, rejects
  model-controlled tenant fields, and verifies the service's pinned scope.

### Compatibility
- No public 0.3 symbols were removed. See the RU/EN 0.4 migration guide for the
  capability and scope transition. Every server/cloud adapter documents a
  versioned-index/store rollback path.

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
