# protoprompt 0.12.0

ProtoPrompt 0.12 adds a deliberately narrow, host-owned restart boundary for
one strict Ledger recall selection. It does not serialize agent state, provider
messages, task text, tool state, or memory payload; it is not a workflow
engine, lease, or exactly-once mechanism.

## Highlights

- **Sealed strict-selection checkpoint** — experimental
  `LedgerRecallPlanner.checkpoint()` and `resume_checkpoint()` persist an
  opaque checkpoint/continuation reference, policy/counter/budget receipt, and
  private selected-record markers. Durable storage deliberately excludes raw
  task text, memory payload, provider messages, tool output, and a
  process-local plan.
- **Host-held integrity boundary** — checkpointing requires a stable,
  host-owned 32–4096-byte `checkpoint_secret`. An HMAC-SHA256 seal is verified
  before a fresh plan is exposed; the secret itself is never written to SQLite
  or a public receipt.
- **Exact fresh re-plan** — resume requires the original strict
  admission-audited policy/fingerprint and compatible counter identity. It
  re-plans with the stored token/byte budgets and accepts only an exact match
  of selected records, revisions/content hashes, and token/byte receipt.
- **Query-bound bounded composition** —
  `LedgerContextComposer.plan_checkpoint_messages()` accepts no Ledger budget
  override. Its `ContextInput.query` must match the task used for fresh
  revalidation, then it follows the bounded v0.11 data-lane path and final
  lifecycle validation.
- **Lifecycle-aware deletion** — every lifecycle change to a selected record,
  including hard erase, invalidates the checkpoint and removes its private
  selection markers in the same Ledger transaction.
- **Schema v6 and frozen evidence** — v5 → v6 is additive and adds immutable
  manifest/selection sidecars. Frozen offline Memory Benchmark v0.3 contains
  four cases / thirteen checks for restart, tamper rejection, lifecycle
  invalidation, and query-bound composition. Its canonical fixture SHA-256 is
  `38ab32e37d7f736152710108d0df8a60e9782ef0c94e1cc6e2be7a6a1cb4b1b6`.

## Safe host flow

```python
from protoprompt import ContextInput, InMemStore, TokenBudgetedContextBuilder
from protoprompt.ledger.recall import (
    LedgerContextComposer,
    LedgerRecallPlanner,
    LedgerRecallPolicy,
)
from protoprompt.tokens import RegexTokenCounter

# `writer` remains scope-pinned. Load this 32–4096-byte secret from protected
# host configuration; do not derive it from the task or put it in SQLite.
host_secret = load_checkpoint_secret()
counter = RegexTokenCounter()
builder = TokenBudgetedContextBuilder(
    InMemStore(), embedding_client, counter=counter, max_tokens=4_096,
    scope=writer.scope,
)
planner = LedgerRecallPlanner(
    writer,
    policy=LedgerRecallPolicy.admission_safe_default(),
    counter=counter,
    checkpoint_secret=host_secret,
)
composer = LedgerContextComposer(builder, planner)

plan = planner.plan(task="repair durable recovery", token_budget=600)
checkpoint = planner.checkpoint(
    plan, checkpoint_id="recovery-42", continuation_ref="host-job-42"
)

# After a process restart, construct a new planner with the same protected
# secret and strict policy/counter contract, then ask it to fresh-revalidate.
resumed_planner = LedgerRecallPlanner(
    writer,
    policy=LedgerRecallPolicy.admission_safe_default(),
    counter=counter,
    checkpoint_secret=host_secret,
)
resumed_composer = LedgerContextComposer(builder, resumed_planner)
resume = resumed_planner.resume_checkpoint(
    checkpoint.checkpoint_id, task="repair durable recovery"
)
request = await resumed_composer.plan_checkpoint_messages(
    resume,
    ContextInput(query="repair durable recovery", include_session=False),
    user_message="Continue with the verified context.",
)

# Send immediately through the host's provider client after this final check.
messages = request.render_messages()
receipt = request.receipt
```

If checkpoint resume or final composition raises a checkpoint/stale-plan error,
create a fresh plan and checkpoint instead of widening budgets or reusing the
old receipt. `LedgerComposedRequest` is transient: a later deletion cannot
retract data that the host has already sent to a provider.

## Upgrade notes

```bash
pip install --upgrade "protoprompt==0.12.0"
```

Back up a Ledger database before upgrade, run `dry_run_setup()`, then let the
normal `setup()` path migrate v5 to v6. The migration is additive and does not
invent checkpoints for old plans. Older binaries reject v6 rather than
downgrading it in place. Setup validates sidecar structure and relational
invariants, but HMAC authenticity can be checked only by a host holding the
`checkpoint_secret` during resume.

The local Ollama PDF-RAG app is released in lockstep and remains a separate,
local reference app:

```bash
pip install "protoprompt[documents,fastapi,ollama]==0.12.0"
pip install "git+https://github.com/Idxeed/protoprompt.git@v0.12.0#subdirectory=apps/ollama-chat"
pp-ollama-chat
```

Checkpoints are opt-in and are not auto-wired into `pp-agent`,
`pp-ollama-chat`, legacy memory, provider sessions, RAG, or profile/session
paths. They do not claim model quality, latency, retrieval quality,
prompt-injection immunity, agent recovery, or unlimited memory.

## Release gate

The release workflow verifies version alignment, deterministic core and
reference-app tests, frozen v0.1/v0.2/v0.3 memory suites, strict RU/EN docs,
sdist/wheel metadata, and clean wheel import. The sealed path is additionally
tested across restart, invalid/missing secret, HMAC tamper, policy/counter/scope
drift, task/query mismatch, selected-record lifecycle changes, hard erase,
corrupt sidecars, and a final composition race.
