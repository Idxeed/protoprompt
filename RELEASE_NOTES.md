# protoprompt 0.7.0

This release turns the project’s core claim into a testable contract:
long-lived memory may be retained indefinitely, but every model request is a
fresh, explainable, bounded decision.

## Highlights

- **Explainable context plans** — `ContextPlan`, `ContextBlockDecision`, and
  `ContextRequestReceipt` expose which context was selected, omitted, or
  truncated without serializing source prompt/document content into telemetry.
  `TokenBudgetedContextBuilder.plan_messages()` returns an immutable provider
  request and exact final-request accounting.
- **Agent parity** — `pp-agent` now uses that same final-request planner for
  normal turns, planning, and compaction. Completion reserve, model-window
  budget, and action/result protocol groups stay explicit and safe.
- **Offline memory benchmark** — a versioned deterministic suite compares
  tail-window, rolling-summary, vector-recall, and frozen 0.6.1 reference
  behavior without a network, model, or API key.
- **Local Ollama reference app** — `pp-ollama-chat` combines PDF RAG, an
  append-only durable conversation archive, visible request receipts, and
  precise deletion of transcript/vector/file projections. It is source-only
  for this release and is installed from `apps/ollama-chat`.
- **Hardened local ingestion** — PDF parsing applies compressed-stream limits
  before expansion; raw multipart and chat bodies are bounded before framework
  parsing; default storage is loopback-local with owner-only POSIX paths.

## Upgrade

```bash
pip install --upgrade protoprompt
```

Existing `build()` and `build_messages()` call patterns remain supported.
Hosts that need an audit-safe model request can move to the additive planning
surface:

```python
plan = await builder.plan_messages(
    request,
    history=history,
    final_messages=[{"role": "user", "content": question}],
    output_reserve=1_024,
)
messages = plan.render_messages()
receipt = plan.receipt
```

For the local Ollama reference app, install compatible local sources and keep
the server on loopback unless you add an authentication boundary:

```bash
pip install "protoprompt[documents,fastapi,ollama]==0.7.0"
pip install "git+https://github.com/Idxeed/protoprompt.git@v0.7.0#subdirectory=apps/ollama-chat"
pp-ollama-chat
```

The full transcript remains local, while the active request is bounded. The
app therefore provides durable recall, not a promise of perfect recall or an
unbounded model context window.

## Release gate

The tag workflow checks the tag/version pair, deterministic library, agent,
benchmark, and Ollama-app tests; builds Russian and English docs; validates
library and reference-app wheels; imports the wheels from a clean environment;
then publishes the library artifacts and creates the GitHub release from these
notes.
