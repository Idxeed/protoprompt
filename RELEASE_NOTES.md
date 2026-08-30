# protoprompt 0.13.0

ProtoPrompt 0.13 adds an experimental PostgreSQL implementation of the v6
Memory Ledger. It preserves the existing synchronous, host-owned
`MemoryWriter` contract for lifecycle, admission, strict recall, and sealed
checkpoints; it does not claim unlimited memory, a workflow engine, or a
throughput benchmark.

## Highlights

- **Optional PostgreSQL Ledger** — `PostgresMemoryLedger` is available through
  `protoprompt[postgres]`. Importing the core remains dependency-free; Psycopg
  is loaded only when this backend is constructed.
- **Fresh-v6 deployment only** — PostgreSQL Ledger uses an otherwise-empty,
  dedicated schema and explicit `dry_run_setup()` / `setup()`. It does not
  migrate an old PostgreSQL layout, import a SQLite Ledger file, downgrade a
  schema, or pretend that a PostgreSQL backup is a file copy.
- **Same host semantics under MVCC** — writes in one Ledger schema take a
  transaction-scoped advisory lock. The five-second lock/serialization/
  deadlock boundary reports `LedgerConflictError`; retry the complete
  idempotent host command with the same `event_id`, not a SQL fragment.
- **Fail-closed storage conformance** — setup and operational checks reject
  drift in relation shape, BIGSERIAL ownership/configuration, deterministic
  text collation, indexes/constraints, guard functions/triggers, RLS/policies,
  DML rewrite rules, inheritance/partitioning, and schema-callable objects.
  Runtime resolves built-ins from `pg_catalog` first; the controlled hard-erase
  switch is forced off for ordinary writes.
- **Shared proof, not a parallel API** — `MemoryWriter` now accepts an
  internal nominal Ledger backend marker. A public conformance suite exercises
  the same host-facing semantics on SQLite and PostgreSQL without widening the
  public command surface.

## Safe deployment

Provision from a migration job, then construct the scope-pinned writer only
after explicit setup has completed:

```python
from protoprompt.ledger import MemoryWriter, PostgresMemoryLedger
from protoprompt.scope import MemoryScope

dsn = "postgresql://ledger_migrator:secret@db/app"
ledger = PostgresMemoryLedger(dsn, schema="app_memory_ledger")

print(ledger.dry_run_setup())  # inspection only; no DDL
ledger.setup()                 # idempotent fresh-v6 setup

writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="acme", user="u-42", thread="support"),
)

# Use the normal trusted-host Ledger admission/lifecycle APIs.
ledger.close()
```

Use a new schema containing no application tables, extensions, functions,
types, operators, custom triggers, or manual Ledger indexes. The deployment
role needs DDL privileges only for setup; grant runtime roles only the narrow
schema/table rights they need. Text collations must be deterministic. See the
[PostgreSQL guide](docs/en/postgres.md) for the operational contract.

`PostgresMemoryLedger.backup()` intentionally raises `NotImplementedError`.
Use `pg_dump`, managed backup/PITR, tested restores, and retention/key policy
for the dedicated schema. `forget()` and `erase()` affect live Ledger rows;
they do not remove historical backups, WAL, replicas, or external projections.

## Upgrade notes

```bash
pip install --upgrade "protoprompt[postgres]==0.13.0"
```

Existing SQLite Ledger users do not need a migration for this release. The
PostgreSQL backend starts from a fresh v6 schema only; preserve the old system
and export/re-ingest through a reviewed host flow if a move is required. Do
not point it at a legacy PostgreSQL Ledger and expect automatic conversion.

The local Ollama PDF-RAG reference app is released in lockstep and remains a
separate local application:

```bash
pip install "protoprompt[documents,fastapi,ollama]==0.13.0"
pip install "git+https://github.com/Idxeed/protoprompt.git@v0.13.0#subdirectory=apps/ollama-chat"
pp-ollama-chat
```

## Evidence and release gate

The release gate runs the backend-neutral Ledger conformance helpers for
SQLite and a live PostgreSQL 17 suite with fifteen integration cases. The
suite covers explicit setup/fresh-schema rejection, lifecycle/admission/strict
recall/checkpoint parity, idempotency, restart, two-connection contention,
guard repair, catalog tampering, RLS/policies/rules, inheritance, sequence
ownership, deterministic collation where the server provides an adversarial
fixture, and hard-erase GUC containment. The reference local run executes
fourteen cases and conditionally skips the collation-negative case when the
server has no non-deterministic collation installed.

It also verifies deterministic core/reference-app tests, frozen v0.1/v0.2/
v0.3 semantic benchmarks, strict RU/EN documentation, distributions, and a
clean wheel import of `PostgresMemoryLedger` without `psycopg` installed.

This is an application-integrity boundary, not a defense against a PostgreSQL
superuser, schema owner, role with arbitrary DDL/DML, or arbitrary code given
the private database connection. Keep those credentials and connection access
inside the trusted host boundary.
