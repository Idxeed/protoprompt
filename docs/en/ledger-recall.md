# Bounded ledger recall *(experimental)*

`protoprompt.ledger.recall` is the first read path from durable Ledger memory
to an agent's current task. It is intentionally a small, local component:

- it reads only the `active`, host-confirmed, still-valid, payload-present
  records from one pinned `MemoryWriter`;
- it ranks locally with deterministic lexical relevance, confidence, and
  recency — no LLM, embedding, vector query, network call, or legacy memory
  API;
- it packs whole records into a fixed token **and** UTF-8 byte budget, including
  the complete JSON data envelope; and
- it performs a final selected-record ID/revision validation immediately before
  returning context. A forgotten, retracted, expired, erased, or changed
  record makes resolution fail closed and requires a new plan.

This is not a claim of an unlimited context window. It is a bounded **memory
data lane**. The standalone planner keeps it separate; v0.11 adds an explicit
experimental `LedgerContextComposer` for a host that needs one exact provider
request.

v0.12 adds a deliberately narrow durable continuation boundary: a sealed
recall checkpoint can survive a process restart, but it is neither an agent
checkpoint nor an unlimited-memory mechanism. It preserves a verified recall
selection, not a provider conversation or workflow state.

For a concrete v5 ingress origin, the active reader verifies the matching
immutable `allow` audit before a record enters this lane. Records migrated
from pre-v5 schemas carry `legacy_unknown` and remain recallable only for
compatibility; strict deployments must quarantine and re-admit them before
enabling recall. Raw `unknown` writer records are likewise a trusted legacy
escape hatch, not provenance-reviewed model memory.

## Quick start

Create and admit records through the host-owned v0.10 admission boundary first:

```python
from protoprompt.ledger import (
    MemoryAdmissionAction,
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import LedgerRecallPlanner, StaleMemoryPlanError
from protoprompt.scope import MemoryScope

ledger = SqliteMemoryLedger("memory-ledger.db")
ledger.setup()
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="local", user="alice", thread="agent-42"),
)

gate = MemoryReviewGate(
    writer,
    origin=MemoryOrigin.DOCUMENT,
    policy=MemoryAdmissionPolicy(
        policy_id="artifact-facts-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.DOCUMENT,),
        minimum_confidence=0.8,
    ),
)
candidate = gate.ingress(
    kind=MemoryKind.FACT,
    source_ref="artifact:checkpoint-manifest",
    confidence=0.9,
).submit("Checkpoint recovery starts by reading the durable manifest.")
review = gate.review(candidate.record_id)
assert review.action is MemoryAdmissionAction.ALLOW
gate.confirm(review, event_id="admission:checkpoint-manifest:allow")

planner = LedgerRecallPlanner(writer)
plan = planner.plan(
    task="repair checkpoint recovery",
    token_budget=600,
    byte_budget=32_768,
)

try:
    memory_data = planner.resolve(plan).render_data()
except StaleMemoryPlanError:
    # A selected record changed or is no longer eligible. Replan before send.
    memory_data = planner.resolve(
        planner.plan(
            task="repair checkpoint recovery",
            token_budget=600,
            byte_budget=32_768,
        )
    ).render_data()
```

`memory_data` is a canonical JSON envelope such as:

```json
{
  "records": [
    {
      "content": "Checkpoint recovery starts by reading the durable manifest.",
      "kind": "fact"
    }
  ],
  "schema_version": 1,
  "type": "protoprompt.ledger-recall"
}
```

It deliberately contains neither record IDs, scope, content hashes, source
references, nor evidence references. The model receives reference data, not a
tool for mutating the Ledger.

## Trusted composition boundary

The standalone recall planner does **not** alter `WorkingMemory`,
`MemoryService`, legacy `ContextPlan`, or a model's system prompt, and it does
not call a provider. It remains the right choice when the host owns data
placement and final accounting itself.

For the narrow `admitted Ledger JSON → one provider request` case,
`LedgerContextComposer` is the host-owned explicit opt-in bridge. It is not
automatic Ledger, `pp-agent`, or `pp-ollama-chat` behaviour. Its
`TokenBudgetedContextBuilder` and `LedgerRecallPlanner` must share one non-empty
`MemoryScope` and the **same `TokenCounter` instance**. The planner must use
`LedgerRecallPolicy.admission_safe_default()` or a policy with
`require_admission_audit=True`.

The composer puts memory payload only in one `user` JSON message. A fixed
system guard without memory text precedes it; generated system context never
contains raw Ledger payload. The guard+JSON pair sits after generated system
context, when present, and before history/tool graph, so it never splits a
tool call/result dependency. The complete lane is mandatory, reserved before
optional RAG/session/history, and never silently truncated. Insufficient room
raises `TokenBudgetExceededError(..., "ledger_data")`.

```python
from protoprompt import ContextInput, InMemStore, TokenBudgetedContextBuilder
from protoprompt.ledger.recall import (
    LedgerContextComposer,
    LedgerRecallPlanner,
    LedgerRecallPolicy,
    StaleMemoryPlanError,
)
from protoprompt.tokens import RegexTokenCounter

# inside an async host handler
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

try:
    request = await composer.plan_messages(
        ContextInput(
            query="repair checkpoint recovery",
            system_prompt="Follow the host contract.",
            include_session=False,
        ),
        user_message="What should happen next?",
        ledger_token_budget=600,
    )
except StaleMemoryPlanError:
    # Memory lifecycle changed during async context/RAG work. Replan before send.
    raise

messages = request.render_messages()  # send immediately through your provider client
receipt = request.receipt              # exact full-message budget
audit = request.explain()              # content-free metadata
```

The composer itself does not write the Ledger or send a chat request. Its
supplied `TokenBudgetedContextBuilder` may perform asynchronous RAG/embedding
work, however; after that work the composer resolves the original selection
again and fails closed on a stale record, revision, or expiry.

The JSON serializer escapes `<`, `>`, and `&` in rendered content to avoid
visibly closing a downstream XML/HTML-like wrapper. That is defense in depth,
not a replacement for a trusted data boundary: memory text can still contain
untrusted instructions and must remain data. Do not treat contents of a durable
record as system instructions, and do not give a model the writer or lifecycle
methods as tools.

## Selection policy

`LedgerRecallPolicy.safe_default()` accepts only `fact`, `decision`, and
`preference` records with confidence at least `0.5`. `episode` and `procedure`
are excluded by default. An application may opt in only through an explicit
host policy with an evidence and risk contract appropriate to those richer
memory kinds.

This is the compatibility policy for standalone planning: it may still read
legacy `unknown` and `legacy_unknown` records. A composed provider request must
use `LedgerRecallPolicy.admission_safe_default()`. It enables
`require_admission_audit=True`, excluding both unreviewed provenances; concrete
origins also pass the Ledger's audited active-reader invariant.

```python
from protoprompt.ledger import MemoryKind
from protoprompt.ledger.recall import LedgerRecallPlanner, LedgerRecallPolicy

policy = LedgerRecallPolicy(
    policy_id="ops-episodes-v1",
    allowed_kinds=(MemoryKind.FACT, MemoryKind.DECISION, MemoryKind.EPISODE),
    minimum_confidence=0.8,
    active_read_limit=1000,
    candidate_limit=100,
    candidate_scan_byte_budget=1_048_576,
)
planner = LedgerRecallPlanner(writer, policy=policy)
```

The policy is an immutable local selection policy, not an admission policy. It
cannot confirm a candidate. v0.10 admission is a separate host-side
`MemoryReviewGate`: it pins origin and policy before text enters the Ledger,
then records a sealed `allow` / `quarantine` / `reject` decision. It never lets
model output auto-promote itself to active memory. See
[Memory Ledger admission](memory-ledger.md#admission-boundary-v010) for the
RPC-only trust boundary and recovery rules.

For a fixed task, host-controlled clock, active Ledger snapshot, policy, and
token counter, selection is deterministic. Ranking uses lexical overlap first,
then host-set confidence and recency as tie-breakers. The caller cannot supply
its own timestamp to backdate an expiry check; inject a fixed clock only in a
trusted test/replay harness.

The planner reads at most `active_read_limit` active local records (1,000 by
default). SQLite materializes payloads for that bounded active read; the
kind/confidence filter runs before lexical ranking and the candidate-content
scan budget, then at most `candidate_limit` eligible records (100 by default)
are considered. `active_record_count`, `active_read_limit_reached`,
`eligible_record_count`, and `candidate_limit_reached` make that bounded search
visible in the receipt. An active read exactly at its limit is marked as
potentially truncated rather than claiming a complete global search. The raw
candidate-content scan budget is applied only to eligible candidates; an
oversized candidate receives `scan_byte_budget` and does not stop smaller later
candidates from being considered.

## Budget receipts and freshness

`LedgerRecallPlan` contains no plaintext memory or task text. It stores only
private record/revision snapshots plus a content-free receipt:

```python
plan.explain()
# {
#   "policy_id": "ledger-recall-safe-v1",
#   "used_tokens": 118,
#   "used_bytes": 441,
#   "remaining_tokens": 482,
#   "remaining_bytes": 32327,
#   "selected_count": 2,
#   "decisions": [...],
# }
```

The receipt also carries the full content-free policy configuration and its
fingerprint, plus `counter_id`. The built-in counter is
`regex-token-counter-v1`; provide a versioned `counter_id` whenever an
application injects a custom counter, so a saved receipt states the accounting
contract it used.

`LedgerRecallPlan` itself remains an in-process, planner-bound capability, not
a portable checkpoint. It cannot be serialized, transferred to another
planner, or revived after restart. Persist `plan.explain()` for audit; use the
separate sealed-checkpoint API below only when the host needs its narrow,
restart-safe selection contract.

`used_tokens` is the result of the configured `TokenCounter` over the entire
prospective JSON envelope. `used_bytes` is the strict UTF-8 length of the same
envelope. The planner never truncates a record: one is selected whole or
excluded with `over_token_budget` or `over_byte_budget`, then smaller later
records can still be considered.

The full-envelope `used_tokens` value is authoritative. Per-record token costs
in the receipt are non-negative incremental allocation values, so they need
not add up exactly if an injected deterministic tokenizer produces a
non-monotonic count across two different JSON strings.

`resolve()` first reads, renders, and accounts the candidate data outside the
SQLite writer lock. It then takes a short exclusive Ledger lifecycle boundary
and re-reads the active snapshot to validate the selected IDs, revisions,
content hashes, and kinds immediately before returning context. This keeps an
injected token counter out of a database transaction; the final snapshot uses
host time both before and after the short boundary, so a record that expires
during accounting or lock waiting is rejected. If a concurrent
`forget()`/`retract()` wins before that final validation, or a selected record
was already expired, superseded, hard-erased, falls outside the bounded read,
changes revision, or has a changed content hash, it raises
`StaleMemoryPlanError` rather than returning stale text. Replan and resolve
again before each model send.

A composed request adds one short final boundary: after asynchronous context
planning and exact rendered-message accounting, the composer resolves the
original plan again. A changed selected memory becomes `StaleMemoryPlanError`,
not an old JSON payload sent to the provider.

`LedgerCompositionReceipt` binds the policy/fingerprint/counter and records
that final validation. `ContextPlan.data_lanes` contains content-free
`ContextDataLaneReceipt` metadata (record count, bytes, and transport tokens).
`ContextRequestReceipt.input_tokens` remains the only authoritative total for
the full provider request: the lane receipt explains a part of it but does not
replace exact whole-message accounting.

## Sealed recall checkpoints (v0.12)

A sealed checkpoint is an explicit, opt-in restart boundary for **one strict
Ledger selection**. Schema v6 stores an opaque `checkpoint_id`, an opaque
`continuation_ref`, the content-free policy/counter/budget receipt, and private
selection markers (record ID, revision, content hash, and kind). It does not
store task text, raw memory payload, provider messages, tool output, or raw
host/agent state. The continuation reference is only a host-owned opaque
handle; Ledger does not serialize what it points to.

`LedgerRecallPlanner.checkpoint()` is available only when
`require_admission_audit=True` (normally
`LedgerRecallPolicy.admission_safe_default()`) and the planner has a stable
host-owned `checkpoint_secret`. The 32–4096-byte secret seals the durable
manifest with HMAC-SHA256 and is never written to SQLite or a public receipt.
The same protected secret must be supplied to the fresh planner after restart.

```python
# writer, counter, and builder are host-owned and use one strict scope.
# host_secret is a protected, stable 32+ byte value, never stored in SQLite.
planner = LedgerRecallPlanner(
    writer,
    policy=LedgerRecallPolicy.admission_safe_default(),
    counter=counter,
    checkpoint_secret=host_secret,
)
plan = planner.plan(task="repair durable recovery", token_budget=600)
checkpoint = planner.checkpoint(
    plan,
    checkpoint_id="checkpoint-42",
    continuation_ref="continuation-42",
)

# After restart, recreate the writer/builder/planner for the same scope.
# The rebuilt builder and planner share `counter`; the planner gets host_secret.
resumed_planner = LedgerRecallPlanner(
    writer,
    policy=LedgerRecallPolicy.admission_safe_default(),
    counter=counter,
    checkpoint_secret=host_secret,
)
resume = resumed_planner.resume_checkpoint(
    checkpoint.checkpoint_id,
    task="repair durable recovery",
)
resumed_composer = LedgerContextComposer(builder, resumed_planner)
request = await resumed_composer.plan_checkpoint_messages(
    resume,
    ContextInput(query="repair durable recovery", include_session=False),
    user_message="Continue the recovery task.",
)
```

The fresh `task` is required because no task is persisted. It must match
`ContextInput.query` when `plan_checkpoint_messages()` is called; the private
`LedgerRecallResume` cannot be composed into an unrelated request. Stored
Ledger budgets remain authoritative—there is no resume-time budget override.

On resume, the planner verifies the HMAC, requires the original strict policy
ID/fingerprint and `counter_id`, makes a fresh plan using the stored budgets,
and requires the exact selection tuple and used token/byte receipts to match.
Policy or counter drift, a changed/expired/ineligible selection, or a bad seal
fails closed before a provider request is built. The ordinary final composer
validation still guards the interval between resume and send.

Any lifecycle change to a selected record invalidates its active checkpoint in
the same Ledger transaction and removes its private selection markers. A
resume whose fresh selection has changed does the same. `dry_run_setup()` and
`setup()` validate the v6 sidecar structure and its relational invariants, but
cannot validate HMAC authenticity: only a host process holding
`checkpoint_secret` can do that during resume.

This feature deliberately provides no lease, exactly-once delivery, workflow
engine, or automatic integration with `pp-agent`, `pp-ollama-chat`, legacy
memory, or provider sessions. The host explicitly creates, resumes, and
composes each checkpoint.

## Scope and deletion boundary

The planner receives a `MemoryWriter`, not a scope parameter, so it cannot
widen from Alice's writer to Bob's scope. Its active-memory reader path
enforces the Ledger lifecycle and exact host scope.

Planning itself has no writes. `LedgerRecallPlan` avoids copying plaintext,
but the short-lived `LedgerRecallContext` returned by `resolve()` necessarily
contains rendered data in process memory. A caller that retains or sends that
string is responsible for its own process/provider retention boundary. Ledger
`forget()` and `erase()` still remove the live Ledger payload as documented in
the [Memory Ledger guide](memory-ledger.md); they cannot retroactively erase a
context string already returned or sent elsewhere.

The same is true of `LedgerComposedRequest`: it is transient and should be sent
immediately. Its final validation applies only until `plan_messages()` returns;
a later `forget()` cannot retract JSON the host has already given a provider.

## Frozen dual-backend evidence protocol (v1.0)

The repository carries a narrow `v1.0` **evidence-protocol** fixture for the
strict Ledger read path. This is not a package `1.0.0` release claim and it
does not benchmark a model, embedding service, provider, latency, throughput,
or general retrieval quality.

The fixture runs 18 delayed-recall and lifecycle cases against fresh SQLite
and PostgreSQL Ledgers. It requires the same content-free semantic report from
both backends after normalizing only their backend identifier. Its fixed checks
cover tenant/user/thread scope isolation, strict admitted records,
whole-record token/UTF-8-byte budgets, plan/resolve receipts, lifecycle
exclusion, source-revocation scrubbing, and content-free `explain()` output.

Run the full verification only with both local durable backends available:

```bash
pip install -e ".[postgres,dev]"
export PROTOPROMPT_POSTGRES_DSN=postgresql://protoprompt:protoprompt@localhost:5432/protoprompt_test
python scripts/run_memory_benchmark.py --suite v1.0 --ledger-backend all --verify
```

The reported strict-Ledger `9/9` versus 20-record-tail `0/9` number is target
availability in this named, synthetic lexical fixture only: the query contains
the target terms and fillers do not. It must not be used as a model-answer,
external-framework, production-quality, or performance claim. See the
[benchmark protocol](https://github.com/Idxeed/protoprompt/blob/master/benchmarks/README.md) and its bound
`suite.json`, `expected.json`, and `manifest.json` for the precise method.
