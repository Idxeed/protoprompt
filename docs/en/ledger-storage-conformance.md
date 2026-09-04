# Ledger storage conformance (v1 candidate)

The durable Ledger keeps its private command backend private. It does **not**
offer a generic storage-plugin registry: admission, lifecycle, erasure, and
sealed-checkpoint internals are trusted host infrastructure, not an extension
surface for arbitrary code.

Instead, each built-in backend exposes a small, content-free
`LedgerStorageCapabilities` receipt. It identifies one shared semantic profile
and makes the real setup/backup differences visible before the v1 API freeze.

## Shared semantic profile

Contract `protoprompt.ledger.storage` version `1` uses the semantic profile
`strict_host_ledger_v1`. The named common checks cover:

- candidate confirmation with content-free events;
- audited admission and strict bounded recall;
- exact scope isolation and scoped forget;
- idempotent retries and conflicting event reuse;
- lifecycle, source revocation, and hard erase;
- explicit setup plus restart persistence; and
- sealed checkpoint restart/invalidation.

The storage contract deliberately distinguishes three versions:

| Value | Meaning |
| --- | --- |
| storage contract `1` | capability-receipt and named semantic-check format |
| record schema `1` | serialized `MemoryRecord` / event contract |
| target storage schema `7` | current durable SQLite/PostgreSQL Ledger layout |

## Built-in operational matrix

| Backend | Capability ID | Setup mode | Backup mode |
| --- | --- | --- | --- |
| `SqliteMemoryLedger` | `sqlite_v7` | `in_place_migration` | `file_copy` |
| `PostgresMemoryLedger` | `postgres_v7` | `fresh_v7_only` | `operator_managed` |

SQLite can perform its explicit Ledger v1→v7 migration beside legacy tables
and can make a file-copy backup through its documented API. PostgreSQL accepts
only a fresh dedicated v7 schema; its backup, restore, and retention policy
belong to the database operator.

The newer exact-scope payload-purge contract has its own named backend-neutral
runner. It is intentionally separate from the frozen common v1 check list;
it proves durable restart retry, scope isolation, and command-drift rejection
for `MemoryWriter.payload_readback()` / `purge_payloads()` without silently
changing what the older receipt claims.

## Inspect a local receipt

```python
from protoprompt.ledger import SqliteMemoryLedger

print(SqliteMemoryLedger.storage_capabilities().explain())
```

The static method is non-I/O: it does not construct a backend, run setup, open
a connection, reveal a database path, or inspect memory payloads.

## Verify the named semantic checks

SQLite:

```powershell
python -m pytest -q `
  tests/test_ledger_storage_conformance.py `
  tests/test_ledger_conformance_sqlite.py `
  tests/ledger_conformance/test_scope_payload_purge_conformance.py
```

PostgreSQL, with an operator-provided disposable test DSN:

```powershell
$env:PROTOPROMPT_POSTGRES_DSN = "postgresql://..."
python -m pytest -q tests/integration/test_postgres_memory_ledger.py -m integration
```

The PostgreSQL integration suite intentionally includes catalog, RLS/guard,
tamper, contention, and fresh-schema checks in addition to the shared profile.

## What conformance does not prove

A matching capability receipt and green common checks do not prove managed
PostgreSQL restore/PITR, filesystem or database access control, checkpoint
secret custody, physical deletion from backups/WAL/replicas, multi-region
availability, or latency/throughput parity. Those remain separate release and
deployment gates.
