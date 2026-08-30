# protoprompt 0.11.0

ProtoPrompt 0.11 adds one narrow, host-owned bridge from admitted durable
Ledger memory to a fully budgeted provider request. The release does not turn
memory into a hidden system prompt, does not auto-wire reference applications,
and does not claim an unlimited context window or universal retrieval quality.

## Highlights

- **Explicit Ledger-to-request composition** — experimental
  `LedgerContextComposer` connects `LedgerRecallPlanner` to
  `TokenBudgetedContextBuilder` only when both share one non-empty
  `MemoryScope` and the exact same `TokenCounter` instance.
- **Admission-only request data** — composition requires
  `LedgerRecallPolicy.admission_safe_default()` (or an explicit policy with
  `require_admission_audit=True`). Confirmed raw `unknown` records and
  migrated `legacy_unknown` records remain standalone compatibility data, but
  do not enter a composed request.
- **A fixed data boundary** — selected Ledger JSON is rendered only in a
  `user` message, preceded by a static content-free system guard. Raw memory
  never enters generated system context or `explain()` output. The pair is
  inserted before history and tool dependencies, so a call/output graph stays
  intact.
- **Exact end-to-end budget evidence** — the complete guard + JSON transport
  lane is reserved before optional RAG, session, and history. It has a
  content-free `ContextDataLaneReceipt`; the complete
  `ContextRequestReceipt.input_tokens` remains authoritative. A lane that is
  what makes an otherwise valid request overflow raises
  `TokenBudgetExceededError(..., "ledger_data")`.
- **Final lifecycle validation** — after any asynchronous context/RAG work and
  exact message accounting, the same Ledger selection is resolved again. A
  winning forget/retract/expiry/revision change fails closed with
  `StaleMemoryPlanError`.
- **Frozen v0.2 benchmark evidence** — a dependency-free semantic suite adds
  five contract cases / seventeen checks for strict raw exclusion, exact lane
  budget, tool adjacency, content-free injection-shaped data, and an
  event-gated forget race. Its fixture SHA-256 is
  `9bc849dd1d441b2c53d0bad558666b1dd22ad4cf4c8302d5ef5c005102f271c1`.

## Safe host flow

```python
from protoprompt import ContextInput, InMemStore, TokenBudgetedContextBuilder
from protoprompt.ledger.recall import (
    LedgerContextComposer,
    LedgerRecallPlanner,
    LedgerRecallPolicy,
)
from protoprompt.tokens import RegexTokenCounter

# `writer` is a scope-pinned MemoryWriter. Admit concrete-origin records via
# MemoryReviewGate before this point.
counter = RegexTokenCounter()
builder = TokenBudgetedContextBuilder(
    InMemStore(),
    embedding_client,
    counter=counter,
    max_tokens=4_096,
    scope=writer.scope,
)
planner = LedgerRecallPlanner(
    writer,
    policy=LedgerRecallPolicy.admission_safe_default(),
    counter=counter,
)
composer = LedgerContextComposer(builder, planner)

request = await composer.plan_messages(
    ContextInput(
        query="repair checkpoint recovery",
        system_prompt="Follow the host contract.",
        include_session=False,
    ),
    user_message="What should happen next?",
    ledger_token_budget=600,
)

# Send immediately through the host's provider client.
messages = request.render_messages()
receipt = request.receipt
audit = request.explain()  # content-free metadata only
```

If `StaleMemoryPlanError` is raised, create a fresh request before sending.
`LedgerComposedRequest` is transient: a later deletion cannot revoke data the
host has already sent to a provider.

## Upgrade notes

```bash
pip install --upgrade "protoprompt==0.11.0"
```

There is no new Ledger schema migration in 0.11.0. Existing standalone
`LedgerRecallPlanner.safe_default()` behavior remains compatible. To use the
new composer, deliberately choose `admission_safe_default()` and re-admit any
raw/legacy records that should qualify for this stricter request boundary.

The local Ollama PDF-RAG app is released in lockstep and remains a separate,
local reference app:

```bash
pip install "protoprompt[documents,fastapi,ollama]==0.11.0"
pip install "git+https://github.com/Idxeed/protoprompt.git@v0.11.0#subdirectory=apps/ollama-chat"
pp-ollama-chat
```

It does **not** auto-enable `LedgerContextComposer`; neither does `pp-agent`.
Hosts opt in explicitly, retain control of provider send/retention, and keep
their existing RAG/session/profile behavior unchanged.

## Release gate

The release workflow verifies version alignment, deterministic core and
reference-app tests, both frozen memory benchmark suites, strict RU/EN docs,
sdist/wheel metadata, and clean wheel import. The composition boundary is
additionally regression-tested for scope/counter identity, admission filtering,
payload-free explanations, exact budget accounting, tool-call/output
preservation, caller mutation, and a concurrent `forget()` race.
