# Redis

Redis is used for bounded, ephemeral state—not as a second vector backend:

```bash
pip install "protoprompt[redis]"
```

PostgreSQL/pgvector remains the production vector retrieval recommendation.
Adding Redis Search would introduce another index lifecycle and subtly
different filtering semantics without improving the common deployment. Redis
instead handles three native strengths.

## TTL embedding cache

```python
from protoprompt import CachedLLMClient, MemoryScope
from protoprompt.integrations import RedisEmbeddingCache

cache = RedisEmbeddingCache(
    "redis://localhost:6379/0",
    ttl_seconds=3600,
    scope=MemoryScope(tenant="acme", user="alice"),
)
embeddings = CachedLLMClient(model_client, cache)
```

`CachedLLMClient` accepts sync and async cache contracts. Redis keys contain
only a scope/key hash—never input text or model prompts. Corrupt payloads are
deleted and treated as misses.

## Ephemeral sessions and profiles

`RedisSession` implements ordered `get_items(limit)`, `add_items`, `pop_item`,
and `clear_session`, matching the session semantics used by the OpenAI Agents
adapter. Reads and writes refresh TTL.

`RedisProfileStore` is tenant-pinned and implements optimistic locking with
Redis `WATCH`/`MULTI`/`EXEC`. It is appropriate for expiring profiles; use
`PostgresProfileStore` when the profile is a durable system of record.

```python
session = RedisSession(
    "case-42",
    scope=MemoryScope(tenant="acme", user="alice", thread="case-42"),
    url="redis://localhost:6379/0",
    ttl_seconds=86_400,
)
profiles = RedisProfileStore(
    client=session.client,
    tenant="acme",
    ttl_seconds=86_400,
)
```

Share the `Redis` client, not its connection pool. Externally supplied clients
remain host-owned; URL-created clients must be closed with `await close()`.

## Integration and reconnect test

```bash
docker compose -f docker-compose.redis.yml up -d --wait
export PROTOPROMPT_REDIS_URL="redis://localhost:56379/15"
pytest tests/integration/test_redis_integration.py -v
docker compose -f docker-compose.redis.yml down
```

The test forces a connection-pool disconnect, verifies transparent reconnect,
checks TTL, and races ten profile compare-and-swap operations. Configure
timeouts, health checks, retry policy, TLS, and ACL credentials in the host for
production deployments.
