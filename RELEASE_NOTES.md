# protoprompt 0.10.0

ProtoPrompt 0.10 adds the admission boundary that turns the experimental
Memory Ledger from “host-confirmed storage” into a safer, evidence-backed
memory ingress. It is intentionally focused: no hidden prompt injection, no
new framework dependency, and no automatic migration of old memories into
trusted facts.

## Highlights

- **Host-owned admission** — `MemoryReviewGate` binds one Ledger writer to a
  fixed scope, origin, policy, and actor. A narrow ingress accepts only text;
  scope, origin, kind, confidence, source/evidence IDs, lifecycle state, and
  idempotency keys remain host-owned.
- **A conservative default** — `MemoryAdmissionPolicy()` quarantines every
  origin. `safe_default()` allows only high-confidence `host_assertion`
  records. Hosts opt into document, user, tool, or model origins explicitly.
- **Durable, content-free evidence** — each gate decision stores an immutable
  `MemoryAdmissionAudit` paired with the lifecycle event. Concrete-origin
  active records are revalidated against their audit before bounded Ledger
  Recall can return them.
- **Lifecycle-safe decisions** — `review()` is pure and produces a sealed
  in-process capability. `allow` activates, `quarantine` isolates, and
  `reject` forgets the local payload. Stale, forged, cross-gate, revoked,
  expired, and post-hard-erase reviews fail closed.
- **Schema v5 migration** — explicit `dry_run_setup()` / `setup()` adds
  provenance and review sidecars without rewriting existing events. Live v4
  payload rows become `legacy_unknown`; no origin or audit is invented.
- **Hardening at the storage boundary** — sidecars are write-once in SQLite.
  Hard erase is the single controlled cascade that removes their rows and
  restores the guards before commit. Mismatched, orphaned, or event-forged
  audit rows fail closed.

## Safe host flow

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
from protoprompt.scope import MemoryScope

ledger = SqliteMemoryLedger("memory-ledger.db")
ledger.setup()
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="local", user="alice", thread="chat-42"),
)
gate = MemoryReviewGate(
    writer,
    origin=MemoryOrigin.DOCUMENT,
    policy=MemoryAdmissionPolicy(
        policy_id="document-facts-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.DOCUMENT,),
        minimum_confidence=0.8,
    ),
)

ingress = gate.ingress(
    kind=MemoryKind.FACT,
    source_ref="pdf:handbook:page-4",
    confidence=0.9,
)
candidate = ingress.submit("Support requests are answered in Russian.")
review = gate.review(candidate.record_id)
assert review.action is MemoryAdmissionAction.ALLOW
active = gate.confirm(review, event_id="admission:handbook-p4:allow")
```

The model-facing transport is only a JSON/RPC schema equivalent to
`{"content": "..."}`. Do not pass a gate, writer, Ledger, review, or ingress
object to arbitrary in-process code. Those objects are host capabilities, not
a Python authorization sandbox.

## Upgrade notes

```bash
pip install --upgrade "protoprompt==0.10.0"
```

Run `ledger.dry_run_setup()`, make a backup, then run `ledger.setup()` in an
explicit migration job. The forward-only v5 migration retains existing events
unchanged. Pre-v5 active records remain recallable as `legacy_unknown` for
compatibility, but were never reviewed by v0.10; a strict deployment must
quarantine and re-ingest/review them before enabling recall.

Older v0.9 code rejects schema v5. Roll back traffic only by restoring a
pre-upgrade backup into a separate database; never destructively downgrade a
shared Ledger file in place. `erase()` removes live Ledger rows and audit
sidecars but cannot erase historical backups, WAL/journal files, text already
sent to a model, or external projections without their own deletion contract.

`writer.propose()` / `writer.assert_candidate()` and raw lifecycle cleanup
remain available only as trusted-host compatibility APIs. They produce
`unknown` provenance and no admission audit. In particular,
`writer.confirm()` now rejects a candidate created with a concrete v5 origin;
apply the matching gate review instead.

`MemoryReview` is process-local. Persist the candidate `record_id` and your
action `event_id`; after a restart, use `writer.events(record_id)` and
`writer.admission_audits(record_id)` to resolve a completed decision rather
than trying to replay an old review. A concrete-origin admission lifecycle
event without its matching audit is a corruption/atomicity alarm: stop rather
than retrying. A hard-erased record and its old event IDs remain terminal.

## Reference Ollama app

The local PDF-RAG Ollama app remains loopback-only by default and is released
from this repository alongside the core package:

```bash
pip install "protoprompt[documents,fastapi,ollama]==0.10.0"
pip install "git+https://github.com/Idxeed/protoprompt.git@v0.10.0#subdirectory=apps/ollama-chat"
pp-ollama-chat
```

## Release gate

The release workflow verifies tag/version alignment, deterministic core and
reference-app tests, migration fixtures, the supported Python 3.11–3.13
compatibility range (with the release build on Python 3.12), Russian and
English documentation, wheel imports, and publication order. The
storage admission contract is additionally covered by forged-audit,
cross-scope, stale-review, write-lock expiry, restart, hard-erase, and v4
migration regression tests.
