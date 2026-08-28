# PostgreSQL и pgvector

Production-адаптер использует async pool Psycopg 3 и cosine search pgvector:

```bash
pip install "protoprompt[postgres]"
```

## Явный setup

Конструктор и открытие store никогда не выполняют DDL. Запускайте setup из
отдельной migration job под ролью, которой разрешено создать schema.
`create_extension=True` также требует права установить расширение pgvector:

```python
from protoprompt.integrations import PgVectorStore

store = PgVectorStore(
    "postgresql://app:secret@db/protoprompt",
    dimensions=1536,
)
await store.setup(create_extension=True, create_hnsw_index=True)
await store.close()
```

При старте приложения открывается только pool:

```python
store = PgVectorStore(dsn, dimensions=1536)
await store.open()
```

`add` заменяет документ целиком в одной транзакции. `query` поддерживает те же
equality- и `$in`-фильтры metadata, что core stores, необязательный порог cosine
similarity и параметризованные JSONB-значения. Размерность и конечность чисел
embedding проверяются до сетевого запроса.

## Профили и конкуренция

Async `PostgresProfileStore` разделяет одинаковый `user_id` по закреплённому
хостом tenant и реализует атомарный compare-and-swap для optimistic locking в
`ProfileManager`:

```python
from protoprompt import ProfileManager
from protoprompt.integrations import PostgresProfileStore

profiles = PostgresProfileStore(dsn, tenant="acme")
await profiles.setup()  # только migration job
manager = ProfileManager(profiles)
```

Передайте уже открытый `AsyncConnectionPool` через `pool=`, если vector и
profile stores должны делить lifecycle. Внешний pool адаптеры не открывают и
не закрывают.

## Event loop в Windows

В Windows async-реализации psycopg нужен selector event loop, а не стандартный
proactor loop. Передайте selector `loop_factory` в `asyncio.Runner`:

```python
import asyncio
import selectors

with asyncio.Runner(
    loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
) as runner:
    runner.run(main())
```

Integration suite выбирает этот loop только в Windows.

## Локальный integration test

```bash
docker compose -f docker-compose.postgres.yml up -d --wait
export PROTOPROMPT_POSTGRES_DSN="postgresql://protoprompt:protoprompt@localhost:55432/protoprompt_test"
pytest tests/integration/test_postgres_integration.py -v
docker compose -f docker-compose.postgres.yml down
```

Compose volume сохраняется после перезапуска контейнера. Используйте `down -v`
только если действительно хотите удалить локальные тестовые данные. Перед
изменением production-схемы сделайте backup: startup приложения не выполняет
автоматический rollback или разрушительный downgrade.
