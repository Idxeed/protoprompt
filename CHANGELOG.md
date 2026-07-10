# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
