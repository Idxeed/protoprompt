# Experimental memory ledger

The v0.8 ledger foundation turns a durable memory from an unstructured vector
chunk into a scoped record with an explicit lifecycle. It is deliberately
**opt-in** and experimental: it does not change `MemoryService`, profiles,
session compression, vector recall, or `ContextPlan` until a host explicitly
installs a later adapter.

That separation is intentional. A PDF, tool result, transcript, or model
extraction must never become a trusted system-priority fact simply because it
was persisted.

![Memory Ledger lifecycle: candidate, trusted confirmation, active recall, lifecycle exit](assets/memory-ledger-lifecycle.svg)

## Quick start

Schema setup is an operator action, never an import-time side effect:

```python
from protoprompt.ledger import MemoryWriter, SqliteMemoryLedger
from protoprompt.scope import MemoryScope

ledger = SqliteMemoryLedger("memory-ledger.db")
print(ledger.dry_run_setup())  # no writes
ledger.setup()                 # explicit, idempotent ledger schema setup

writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="local", user="alice", thread="chat-42"),
)

candidate = writer.propose(
    kind="preference",
    content="The user prefers answers in Russian.",
    source_ref="turn:42",          # host-minted opaque ID, not raw text
    evidence_refs=("turn:42:line:1",),
)

# A trusted host policy/reviewer decides this step. Keep the writer and raw
# ledger out of untrusted plugin or model-tool code.
active = writer.confirm(
    candidate.record_id,
    expected_revision=candidate.revision,
)

assert writer.list_active() == [active]
```

`MemoryWriter` is constructed with one non-empty, host-owned `MemoryScope`.
Its mutation methods do not take a tenant, user, thread, lifecycle state, or
trust level. The lower-level `SqliteMemoryLedger` requires that same exact
scope on every operation. This is an API-shaping guard inside a trusted host
process, not a Python sandbox or authorization boundary: do not expose either
object to untrusted code.

## Lifecycle and recall

| State | How it gets there | Default recall |
|---|---|---|
| `candidate` | `propose()` / `assert_candidate()` | never |
| `active` | explicit `confirm()` | only while valid and payload-present |
| `superseded` | explicit `supersede(old, replacement)` | never |
| `retracted` | `retract()` or `forget()` | never |
| `expired` | `expire()` / `expire_due()` | never |
| `quarantined` | `quarantine()` | never |

Every lifecycle transition takes `expected_revision`; stale commands fail
instead of silently overwriting a newer decision. `forget()` records a
`forgotten` event and advances the revision even when a record was already
`retracted`, so payload removal invalidates stale cached projections while the
original `retracted_at` remains the time it first left recall.

A caller may retain an `event_id` across a retry. It is idempotent while the
original command remains addressable; a completed `forget()` retry returns its
stored erasure receipt. Replaying a candidate after its payload was forgotten,
or any command after a hard erase, is deliberately rejected rather than
resurrecting data. Reusing an ID for different input always raises a conflict.

Replacing a fact is explicit and deterministic:

```python
new = writer.confirm(new_candidate.record_id, expected_revision=1)
old = writer.supersede(
    old_active.record_id,
    replacement_record_id=new.record_id,
    expected_revision=old_active.revision,
    expected_replacement_revision=new.revision,
)
```

The replacement receives a typed `supersedes` relation in the same scope. No
cross-scope relation or implicit “latest wins” heuristic exists.

## Retention and deletion

The operational event history contains no plaintext memory, source ID,
evidence ID, or content fingerprint. Plaintext and opaque provenance live in a
separate local payload row; v4 setup explicitly scrubs the legacy creation
fingerprints written by earlier experimental schemas.

- `retract()` immediately excludes a record from recall but keeps that local
  payload for review.
- `forget()` moves it to `retracted`, deletes its local plaintext,
  source/evidence payload, source lookup entries, and relations, and redacts
  its stored content fingerprint. A content-free `forgotten` event and
  lifecycle receipt remain.
- `forget_by_source("pdf:opaque-id")` is atomic for all currently linked
  records in the writer's exact scope. It also keeps a scoped opaque source
  tombstone, so that source cannot be ingested again in that scope; the same
  source ID remains independent in another scope.
- `erase()` is the explicit irreversible local escape hatch: it removes the
  record, payload, relations, and its event receipts. It also redacts links to
  that record from events owned by other records. It retains scoped opaque
  replay tombstones and its hard-erase receipt so an in-flight retry cannot
  resurrect the erased record ID; use a fresh record ID for a new memory.

This first slice has no external vector/FTS projection adapter. Therefore it
does not claim that `forget()` or `erase()` remove data from a separate vector
database; an adapter must provide durable deletion acknowledgement before
exposing that guarantee. Both operations affect live ledger rows, while SQLite
cannot promise erasure from historical backups, WAL/journal files, or physical
media. Use encryption/key destruction and a documented backup-retention policy
when that property matters.

## Safety boundaries

- Content is bounded to 16,000 characters; opaque IDs and references are
  bounded and cannot contain whitespace or raw multi-line text.
- `source_ref` and `evidence_refs` are host-minted provenance IDs. Do not put a
  filename, URL with secrets, prompt, or document body in them.
- `content_hash` is a scope-separated operational fingerprint, not a
  cryptographic audit or password primitive. It is redacted after `forget()`.
  The SQLite event log is not tamper-evident.
- `export()` excludes plaintext by default; use `include_content=True` only in
  an explicit, protected export flow.

## Migration and rollback

Use a dedicated ledger SQLite file by default. A ledger can share a file with
the tested legacy `SqliteStore` because it never imports `chunks`, profiles, or
session summaries automatically. Its table, explicit-index, and event-trigger
names are reserved rather than globally namespaced. There are no schema
extension points: `dry_run_setup()` and `setup()` accept only the exact
ledger-owned table/index definitions and reject external indexes or triggers
that target ledger tables, instead of adopting, overwriting, or silently
running them.

1. Run `dry_run_setup()` and back up the database with `ledger.backup(path)`.
2. Call `setup()` in an explicit migration job.
3. Keep legacy readers authoritative while evaluating a separate opt-in
   adapter or importer.
4. Roll back by stopping writes to the ledger and returning traffic to the
   old components; do not destructively downgrade a shared database.

Profile/session/vector importers and stable request composition remain future
work. The ledger and its experimental recall lane intentionally preserve all
existing public behavior until those migration contracts are separately proven.
