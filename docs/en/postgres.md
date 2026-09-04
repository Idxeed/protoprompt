# PostgreSQL and pgvector

PostgreSQL support is deliberately split by contract: `PgVectorStore` and
`PostgresProfileStore` use Psycopg 3's async pool, while the experimental
`PostgresMemoryLedger` is a synchronous command backend for the Ledger's
strict lifecycle, admission, recall, and checkpoint boundary. All three use
the same optional dependency:

```bash
pip install "protoprompt[postgres]"
```

## Experimental PostgreSQL Memory Ledger

`PostgresMemoryLedger` is an opt-in replacement for `SqliteMemoryLedger`, not
an automatic upgrade of existing application memory. Its public command edge
is synchronous because `MemoryWriter`, admission, and bounded Ledger recall
are synchronous at that edge. It is separate from the async pgvector and
profile adapters; do not call its blocking commands directly from an async
request handler. Keep it behind a controlled worker/thread boundary when the
host is async.

Create the Ledger schema once from a migration job, using a dedicated schema
owned exclusively by this Ledger instance:

```python
from protoprompt.ledger import PostgresMemoryLedger

ledger = PostgresMemoryLedger(
    "postgresql://ledger_migrator:secret@db/app",
    schema="app_memory_ledger",
)
print(ledger.dry_run_setup())  # inspection only; no DDL
ledger.setup()                 # explicit, idempotent fresh-v7 setup
ledger.close()
```

`schema=` is an identifier, not arbitrary SQL. The adapter rejects `public`,
PostgreSQL system schemas, `pg_` names, and unsafe identifiers. Still, use a
new otherwise-empty schema: no application tables, extensions, functions,
types, operators, custom triggers, or manual Ledger indexes. Fresh setup
refuses a polluted schema; an installed Ledger then validates its complete
reserved layout and fails closed if it is partial or changes unexpectedly.
That validation includes table/column/sequence shape, deterministic text
collations, indexes/constraints, guard function attributes, triggers, RLS
policies, DML rewrite rules, and inheritance/partitioning. The migration role
needs the rights to create the schema, tables, indexes, and guard
functions/triggers. A runtime role can be granted only the narrow rights it
needs after setup.

These checks are a fail-closed application-integrity boundary, not a defense
against a PostgreSQL superuser, schema owner, or a principal with arbitrary
DDL/DML. Treat the Ledger schema and its migration/runtime credentials as
trusted and narrowly scoped.

The Ledger's text columns must use deterministic PostgreSQL collations so
scope and key equality remains exact. Normal deterministic database locales
are supported; a non-deterministic ICU collation makes setup and later
validation fail closed. Do not expose the adapter's internal database
connection to plugin code or run arbitrary SQL through it.

This backend accepts **only a fresh v7 Ledger schema**. It does not migrate an
old PostgreSQL Ledger schema, import an SQLite Ledger file, or perform a
destructive downgrade. A found v1–v6 or partial layout is an operator stop,
not a best-effort upgrade. Preserve the old system, export/re-ingest through a
reviewed host flow if needed, and cut traffic over only after validation.

At application startup, opening the adapter creates its synchronous database
connection but does not run DDL. Create the writer only after the migration
job has completed, and close the ledger during process shutdown:

```python
from protoprompt.ledger import MemoryWriter, PostgresMemoryLedger
from protoprompt.scope import MemoryScope

ledger = PostgresMemoryLedger(dsn, schema="app_memory_ledger")
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="acme", user="u-42", thread="support"),
)

# ... use the normal trusted-host Ledger admission and lifecycle APIs ...

ledger.close()
```

### Write serialization, retry, and capacity

To preserve the Ledger's final lifecycle and checkpoint validation under
PostgreSQL MVCC, every Ledger write in one dedicated schema obtains one
transaction-scoped PostgreSQL advisory lock. The lock is intentionally
schema-wide rather than per-scope: it favors exact semantics over premature
write parallelism. It covers lifecycle commands, hard erasure, checkpoint
changes, and their final active-record snapshots.

Plan capacity accordingly: concurrent Ledger writes in that schema serialize.
The lock timeout is five seconds; lock/serialization/deadlock contention is
reported as `LedgerConflictError`. The host should retry the complete trusted
command with the same stable `event_id` where that command supports idempotent
retry, rather than retrying an arbitrary SQL fragment. Monitor conflict rate
and command latency before treating this experimental backend as a
high-throughput event store.

### Backup, recovery, and deletion scope

`PostgresMemoryLedger.backup()` deliberately raises `NotImplementedError`.
A PostgreSQL backup is an operator responsibility; use your platform policy or
`pg_dump` for the dedicated schema, for example:

```bash
pg_dump --format=custom --schema=app_memory_ledger "$DATABASE_URL" \
  --file=app_memory_ledger.dump
```

Encrypt and retain backups according to the data policy, test restoration into
an isolated database, and coordinate schema changes with replica/PITR policy.
`forget()` and `erase()` change the live Ledger rows and preserve the same
content-free receipts/tombstones described in the [Memory Ledger](memory-ledger.md)
guide; they do not erase historical database backups, WAL, replicas, or a
separate vector/FTS projection. Do not claim physical erasure without a
documented platform retention and key-management process.

## Explicit setup

Constructing or opening a store never runs DDL. Run setup from a migration job
with a database role allowed to create the schema. `create_extension=True`
also requires permission to install the pgvector extension:

```python
from protoprompt.integrations import PgVectorStore

store = PgVectorStore(
    "postgresql://app:secret@db/protoprompt",
    dimensions=1536,
)
await store.setup(create_extension=True, create_hnsw_index=True)
await store.close()
```

At application startup, only open the pool:

```python
store = PgVectorStore(dsn, dimensions=1536)
await store.open()
```

`add` replaces a complete document in one transaction. `query` supports the
same equality and `$in` metadata filters as core stores, optional cosine
similarity thresholds, and parameterized JSONB values. Embedding dimensions
and finite values are validated before network I/O.

## Profiles and concurrency

`PostgresProfileStore` is async and isolates the same `user_id` by a
host-pinned tenant. It implements atomic compare-and-swap for
`ProfileManager` optimistic locking:

```python
from protoprompt import ProfileManager
from protoprompt.integrations import PostgresProfileStore

profiles = PostgresProfileStore(dsn, tenant="acme")
await profiles.setup()  # migration job only
manager = ProfileManager(profiles)
```

Pass an already-open `AsyncConnectionPool` with `pool=` when vector and profile
stores should share lifecycle. External pools are never opened or closed by
the adapters.

## Windows event loop

On Windows, psycopg's async implementation requires a selector event loop, not
the default proactor loop. Pass a selector `loop_factory` to `asyncio.Runner`:

```python
import asyncio
import selectors

with asyncio.Runner(
    loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
) as runner:
    runner.run(main())
```

The integration suite selects this loop only on Windows.

## Local integration test

```bash
docker compose -f docker-compose.postgres.yml up -d --wait
export PROTOPROMPT_POSTGRES_DSN="postgresql://protoprompt:protoprompt@localhost:55432/protoprompt_test"
pytest tests/integration/test_postgres_integration.py -v
pytest tests/integration/test_postgres_memory_ledger.py -v
docker compose -f docker-compose.postgres.yml down
```

The compose volume is retained across container restarts. Use `down -v` only
when you deliberately want to delete local test data. Back up production data
before schema changes; application startup does not attempt automatic rollback
or destructive downgrade.
