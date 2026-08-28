# protoprompt 0.3.0

The context-platform release: production-ready RAG primitives, durable user
profiles, scoped encrypted secrets, and an experimental coding-agent CLI.

This is the first PyPI release since `0.1.0`, so it also includes the async
stores, integrations, token-budget improvements, SQLite backend, embedding
cache, and observability hooks developed for `0.2.0`.

## Highlights

- **RAG engine** — document chunking and indexing, scoped top-k retrieval,
  similarity thresholds, structured provenance, and optional LLM reranking.
- **Cross-session user profiles** — rule-based, LLM-based, or composite
  extraction; typed profile deltas; deterministic merge; SQLite persistence;
  and optimistic version checks for concurrent updates.
- **Encrypted secret vault** — per-entry Fernet encryption, scope isolation,
  TTL, recoverable key rotation, and host-controlled operations that keep raw
  credentials outside model-visible results.
- **Hard token budgets** — the final assembled context, including headers and
  separators, is guaranteed not to exceed the configured limit.
- **Persistent and async storage** — zero-dependency `SqliteStore`, async store
  protocols, and thread-offloaded wrappers for blocking backends.
- **LLM and vector integrations** — OpenAI, Ollama, OpenAI-compatible HTTP,
  Qdrant, sentence-transformers, and FastEmbed adapters.
- **Memory and observability** — session compression, significance scoring,
  embedding cache, and non-fatal context/pipeline hooks.
- **Experimental `pp-agent` CLI** — resumable sessions, hot/cold working
  memory, plan mode, project manifests, and permission-gated tools.

## Install

```bash
pip install --upgrade protoprompt

# Pick only the integrations you need
pip install "protoprompt[openai,tiktoken]"
pip install "protoprompt[ollama]"
pip install "protoprompt[chroma]"
pip install "protoprompt[qdrant]"
pip install "protoprompt[local]"
pip install "protoprompt[fastembed]"
pip install "protoprompt[secrets]"
```

Python 3.11–3.13 is covered by the CI matrix. The core package still has no
mandatory third-party dependencies.

## Upgrade notes

- `ContextInput.doc_ids=None` searches all indexed documents; `doc_ids=[]`
  intentionally searches none.
- `ContextOutput.rag_chunks` now exposes document id, chunk index, score, and
  metadata alongside the backwards-compatible `rag_blocks` list.
- `ProfileBuilder` is deprecated in favor of `ProfileManager` with
  `LLMProfileSource`, `RuleProfileSource`, or `CompositeProfileSource`.
- Agent-facing code should use registered `SecretAccess.execute()` operations.
  Direct `SecretAccess.grant()` access remains available for trusted hosts but
  is deprecated for model-facing tools.
- No public symbols were removed in this release.

## Verification

- Core suite: 272 passed; one optional Chroma test skipped when the extra is
  unavailable.
- Agent CLI suite: 188 passed; 10 live-backend integration tests deselected.
- Strict Russian and English documentation builds pass.
- Wheel smoke test verifies the installed package outside the source checkout,
  including the packaged profile JSON schema.
- GitHub Actions covers Python 3.11, 3.12, and 3.13 and deploys the docs site.

## Links

- Documentation: https://idxeed.github.io/protoprompt/
- Source: https://github.com/Idxeed/protoprompt
- Changelog: https://github.com/Idxeed/protoprompt/blob/master/CHANGELOG.md
- Issues: https://github.com/Idxeed/protoprompt/issues
