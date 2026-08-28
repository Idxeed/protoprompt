# Redis

Redis используется для ограниченного ephemeral state, а не как второй
векторный backend:

```bash
pip install "protoprompt[redis]"
```

Рекомендованным production vector retrieval остаётся PostgreSQL/pgvector.
Redis Search добавил бы отдельный lifecycle индекса и немного другую семантику
фильтров без пользы для типового deployment. Здесь Redis решает три нативные
для него задачи.

## Embedding cache с TTL

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

`CachedLLMClient` принимает sync- и async-контракты cache. Redis keys содержат
только hash scope/key — исходный текст и prompt туда не попадают. Повреждённый
payload удаляется и считается cache miss.

## Ephemeral sessions и profiles

`RedisSession` реализует упорядоченные `get_items(limit)`, `add_items`,
`pop_item` и `clear_session`, совпадая с session semantics адаптера OpenAI
Agents. Чтение и запись обновляют TTL.

`RedisProfileStore` закреплён за tenant и использует optimistic locking через
Redis `WATCH`/`MULTI`/`EXEC`. Он подходит для истекающих профилей; если профиль
— долговечный источник истины, используйте `PostgresProfileStore`.

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

Делите сам клиент `Redis`, а не его connection pool. Внешний client остаётся
под управлением хоста; созданный из URL закройте через `await close()`.

## Integration и reconnect test

```bash
docker compose -f docker-compose.redis.yml up -d --wait
export PROTOPROMPT_REDIS_URL="redis://localhost:56379/15"
pytest tests/integration/test_redis_integration.py -v
docker compose -f docker-compose.redis.yml down
```

Тест принудительно разрывает соединения pool, проверяет прозрачный reconnect и
TTL, затем сталкивает десять profile compare-and-swap операций. В production
настройте в хосте timeouts, health checks, retry policy, TLS и ACL credentials.
