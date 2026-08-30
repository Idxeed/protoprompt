# protoprompt 0.9.0

ProtoPrompt 0.9 adds the first safe, bounded read path from durable Memory
Ledger records to an agent's current task. It is deliberately narrow:
experimental Ledger Recall is an opt-in JSON **data lane**, not a promise of
unlimited context, automatic memory admission, or a hidden system-prompt
insertion.

## Highlights

- **Bounded Ledger Recall** — `LedgerRecallPlanner` reads only `active`,
  host-confirmed, still-valid payloads through one scope-pinned
  `MemoryWriter`. It ranks locally with deterministic lexical relevance,
  confidence, and recency; it calls no LLM, embedding, vector, or network
  service. Whole records either fit into the canonical JSON data envelope's
  exact token and UTF-8-byte budgets or stay out.
- **Fresh lifecycle check before use** — a plan is bound to its planner,
  scope, policy, token-counter identity, selected record revision, kind, and
  content hash. `resolve()` renders and accounts for data outside SQLite's writer
  lock, then takes a short final lifecycle boundary and rechecks the active
  snapshot. If a record was forgotten, retracted, superseded, erased, expired,
  changed, or displaced outside the bounded reader, resolution fails closed
  with `StaleMemoryPlanError`; callers replan before sending a provider
  request.
- **Explainable and host-controlled** — `plan.explain()` is content-free while
  retaining policy, budget, active-read/candidate bounds, selection decisions,
  and counter identity for audits and developer UIs. The planner owns time via
  its injected host clock, so a model or tool cannot backdate a per-call read
  to bypass expiry.
- **No SQLite lock inversion** — custom token counters run outside the Ledger
  write transaction. A slow or re-entrant counter cannot stall `forget()` or
  leave the connection in a nested transaction. Internal SQLite transaction
  helpers now roll back even after a `BaseException` interruption.
- **Clear boundary for the next milestone** — v0.9 does not yet introduce an
  automatic admission policy, episode/checkpoint runtime, legacy
  `WorkingMemory` migration, or automatic `ContextPlan` composition. Those
  need their own contracts and measurements before they can become defaults.

## Upgrade

```bash
pip install --upgrade protoprompt
```

Create and explicitly confirm records through trusted host code, then plan and
resolve a data lane immediately before composing the provider request:

```python
from protoprompt.ledger import MemoryWriter, SqliteMemoryLedger
from protoprompt.ledger.recall import LedgerRecallPlanner, StaleMemoryPlanError
from protoprompt.scope import MemoryScope

ledger = SqliteMemoryLedger("memory-ledger.db")
ledger.setup()
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="local", user="alice", thread="chat-42"),
)
candidate = writer.propose(
    kind="preference",
    content="The user prefers answers in Russian.",
    source_ref="turn:42",
)
writer.confirm(candidate.record_id, expected_revision=candidate.revision)

planner = LedgerRecallPlanner(writer)
try:
    memory_data = planner.resolve(
        planner.plan(task="answer the user", token_budget=600, byte_budget=32_768)
    ).render_data()
except StaleMemoryPlanError:
    # A lifecycle write won; plan once more before the provider send.
    memory_data = planner.resolve(
        planner.plan(task="answer the user", token_budget=600, byte_budget=32_768)
    ).render_data()
```

Treat `memory_data` as untrusted data in your own request composition. Do not
give model/tool code a `MemoryWriter` or interpret durable record content as
system instructions.

For the local Ollama reference app, install compatible sources and keep the
server on loopback unless you add an authentication boundary:

```bash
pip install "protoprompt[documents,fastapi,ollama]==0.9.0"
pip install "git+https://github.com/Idxeed/protoprompt.git@v0.9.0#subdirectory=apps/ollama-chat"
pp-ollama-chat
```

## Release gate

The tag workflow checks tag/version alignment; deterministic library, agent,
and Ollama-app tests; Russian and English documentation; library
and reference-app distributions; and a clean-environment wheel import. It
publishes verified library artifacts to PyPI and creates the GitHub release
only after the checks pass.
