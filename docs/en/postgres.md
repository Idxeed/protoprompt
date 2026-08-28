# PostgreSQL and pgvector

The production adapter uses Psycopg 3's async pool and pgvector cosine search:

```bash
pip install "protoprompt[postgres]"
```

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
docker compose -f docker-compose.postgres.yml down
```

The compose volume is retained across container restarts. Use `down -v` only
when you deliberately want to delete local test data. Back up production data
before schema changes; application startup does not attempt automatic rollback
or destructive downgrade.
