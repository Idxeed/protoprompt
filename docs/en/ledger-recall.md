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
data lane**. The final provider request must still be composed and checked by
the application's trusted request planner.

## Quick start

Create and explicitly confirm records through the host-owned Ledger API first:

```python
from protoprompt.ledger import MemoryWriter, SqliteMemoryLedger
from protoprompt.ledger.recall import LedgerRecallPlanner, StaleMemoryPlanError
from protoprompt.scope import MemoryScope

ledger = SqliteMemoryLedger("memory-ledger.db")
ledger.setup()
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="local", user="alice", thread="agent-42"),
)

candidate = writer.propose(
    kind="fact",
    content="Checkpoint recovery starts by reading the durable manifest.",
    source_ref="artifact:checkpoint-manifest",
    confidence=0.9,
)
writer.confirm(candidate.record_id, expected_revision=candidate.revision)

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

The recall planner does **not** alter `WorkingMemory`, `MemoryService`, legacy
`ContextPlan`, or a model's system prompt. It also does not call a provider.

Keep the JSON in a caller-owned data lane. If an integration must serialize it
into a text-only request, the application needs a trusted outer instruction
that describes the data boundary, and it must re-run final request accounting
with `TokenBudgetedContextBuilder.plan_messages()` / `ContextRequestReceipt`.
Do not treat contents of a durable record as system instructions, and do not
give a model the writer or lifecycle methods as tools.

The JSON serializer escapes `<`, `>`, and `&` in rendered content to avoid
visibly closing a downstream XML/HTML-like wrapper. That is defense in depth,
not a replacement for a trusted data boundary: memory text can still contain
untrusted instructions and must remain data.

## Selection policy

`LedgerRecallPolicy.safe_default()` accepts only `fact`, `decision`, and
`preference` records with confidence at least `0.5`. `episode` and `procedure`
are excluded until an application deliberately opts in, because their
provenance/admission contract is still being built.

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
cannot confirm a candidate. A future review gate will be a separate host-side
component; it will never let model output auto-promote itself to active memory.

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

The plan itself is an in-process, planner-bound capability, not a portable
checkpoint: persist `plan.explain()` for audit, then create a fresh plan after a
restart or handoff.

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
