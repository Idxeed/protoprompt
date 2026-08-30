# PostgreSQL и pgvector

Поддержка PostgreSQL намеренно разделена по контрактам: `PgVectorStore` и
`PostgresProfileStore` используют async pool Psycopg 3, а экспериментальный
`PostgresMemoryLedger` — синхронный command backend для строгой границы
жизненного цикла, admission, recall и checkpoint Ledger. Все три используют
одну optional-зависимость:

```bash
pip install "protoprompt[postgres]"
```

## Экспериментальный PostgreSQL Memory Ledger

`PostgresMemoryLedger` — opt-in замена `SqliteMemoryLedger`, а не
автоматический upgrade существующей памяти приложения. Его публичная command
граница синхронная, потому что на ней синхронны `MemoryWriter`, admission и
bounded Ledger recall. Он отделён от async-адаптеров pgvector и profile; в
async request handler не вызывайте его блокирующие команды напрямую. Для
async-хоста держите его за контролируемой worker/thread boundary.

Схему Ledger нужно один раз создать из migration job в выделенной schema,
которой владеет только этот экземпляр Ledger:

```python
from protoprompt.ledger import PostgresMemoryLedger

ledger = PostgresMemoryLedger(
    "postgresql://ledger_migrator:secret@db/app",
    schema="app_memory_ledger",
)
print(ledger.dry_run_setup())  # только проверка, без DDL
ledger.setup()                 # явный идемпотентный fresh-v6 setup
ledger.close()
```

`schema=` — identifier, а не произвольный SQL. Адаптер отклоняет `public`,
системные schema PostgreSQL, имена `pg_` и небезопасные identifier. Но всё
равно используйте новую в остальном пустую schema: без application tables,
extensions, functions, types, operators, custom triggers и ручных
Ledger-indexes. Fresh setup отклонит загрязнённую schema; затем установленный
Ledger валидирует весь свой reserved layout и fail-closed останавливается, если
он partial или изменён неожиданно. Эта проверка охватывает форму
table/column/sequence, deterministic text collation, indexes/constraints,
атрибуты guard functions, triggers, RLS policies, DML rewrite rules и
inheritance/partitioning. Migration роли нужны права создать schema, tables,
indexes и guard functions/triggers. После setup runtime-роли можно выдать
только нужные ей узкие права.

Эти проверки — fail-closed граница целостности приложения, но не защита от
PostgreSQL superuser, owner schema или роли с произвольными DDL/DML. Считайте
schema Ledger и credentials migration/runtime-роли доверенными и выдавайте им
минимально необходимые права.

Text-колонки Ledger должны использовать deterministic PostgreSQL collation,
чтобы равенство scope и key оставалось точным. Обычные deterministic locale
поддерживаются; nondeterministic ICU collation приведёт к fail-closed отказу
setup и последующей валидации. Не отдавайте internal database connection
адаптера plugin-коду и не исполняйте через него произвольный SQL.

Этот backend принимает **только свежую Ledger schema v6**. Он не мигрирует
старую PostgreSQL Ledger schema, не импортирует SQLite Ledger file и не делает
destructive downgrade. Найденная v1–v5 или partial layout — сигнал оператору
остановиться, а не best-effort upgrade. Сохраните старую систему, при
необходимости export/re-ingest-ите данные через reviewed host flow и
переключайте traffic только после проверки.

При старте приложения открытие адаптера создаёт синхронное соединение с БД,
но не выполняет DDL. Создавайте writer только после завершённой migration job
и закрывайте ledger при shutdown процесса:

```python
from protoprompt.ledger import MemoryWriter, PostgresMemoryLedger
from protoprompt.scope import MemoryScope

ledger = PostgresMemoryLedger(dsn, schema="app_memory_ledger")
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="acme", user="u-42", thread="support"),
)

# ... используйте обычные trusted-host API admission и lifecycle Ledger ...

ledger.close()
```

### Сериализация записи, retry и capacity

Чтобы сохранить финальную проверку lifecycle и checkpoint при PostgreSQL MVCC,
каждая запись Ledger в одной выделенной schema получает один
transaction-scoped PostgreSQL advisory lock. Lock намеренно общий для schema,
а не для каждого scope: он ставит точную семантику выше преждевременной
параллелизации. Он покрывает lifecycle-команды, hard erase, изменения
checkpoint и их финальные snapshots active records.

Учитывайте это в capacity plan: конкурентные записи Ledger в одной schema
сериализуются. Timeout lock — пять секунд; lock/serialization/deadlock
contention возвращается как `LedgerConflictError`. Хост должен повторять всю
trusted-команду с тем же стабильным `event_id`, где команда поддерживает
идемпотентный retry, а не повторять произвольный SQL-фрагмент. Наблюдайте за
conflict rate и задержкой команд прежде, чем считать этот экспериментальный
backend высоконагруженным event store.

### Backup, recovery и границы удаления

`PostgresMemoryLedger.backup()` намеренно выдаёт `NotImplementedError`.
Backup PostgreSQL — ответственность оператора: используйте policy платформы
или `pg_dump` для выделенной schema, например:

```bash
pg_dump --format=custom --schema=app_memory_ledger "$DATABASE_URL" \
  --file=app_memory_ledger.dump
```

Шифруйте backup и задайте срок хранения согласно политике данных, проверяйте
restore в изолированной БД и согласуйте изменения schema с policy
replica/PITR. `forget()` и `erase()` меняют live Ledger rows и сохраняют те же
content-free receipts/tombstones, что описаны в [Memory Ledger](memory-ledger.md);
они не стирают исторические backup, WAL, replica или отдельную vector/FTS
projection. Не заявляйте physical erasure без документированной platform
retention и key-management process.

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
pytest tests/integration/test_postgres_memory_ledger.py -v
docker compose -f docker-compose.postgres.yml down
```

Compose volume сохраняется после перезапуска контейнера. Используйте `down -v`
только если действительно хотите удалить локальные тестовые данные. Перед
изменением production-схемы сделайте backup: startup приложения не выполняет
автоматический rollback или разрушительный downgrade.
