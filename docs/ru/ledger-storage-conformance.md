# Storage conformance Ledger (кандидат v1)

Durable Ledger сохраняет private command backend private. Здесь **нет**
generic storage-plugin registry: admission, lifecycle, erasure и
sealed-checkpoint internals — это trusted host infrastructure, а не extension
surface для произвольного кода.

Вместо этого каждый built-in backend возвращает небольшой content-free receipt
`LedgerStorageCapabilities`. Он фиксирует общий semantic profile и делает
реальные различия setup/backup явными до v1 API freeze.

## Общий semantic profile

Contract `protoprompt.ledger.storage` версии `1` использует semantic profile
`strict_host_ledger_v1`. Его named common checks покрывают:

- candidate confirmation и content-free events;
- audited admission и strict bounded recall;
- exact scope isolation и scoped forget;
- idempotent retries и conflicting event reuse;
- lifecycle, source revocation и hard erase;
- explicit setup и restart persistence; и
- sealed checkpoint restart/invalidation.

Storage contract намеренно различает три версии:

| Значение | Смысл |
| --- | --- |
| storage contract `1` | формат capability receipt и named semantic checks |
| record schema `1` | serialized contract `MemoryRecord` / events |
| target storage schema `7` | текущая durable SQLite/PostgreSQL Ledger layout |

## Матрица built-in backend-ов

| Backend | Capability ID | Setup mode | Backup mode |
| --- | --- | --- | --- |
| `SqliteMemoryLedger` | `sqlite_v7` | `in_place_migration` | `file_copy` |
| `PostgresMemoryLedger` | `postgres_v7` | `fresh_v7_only` | `operator_managed` |

SQLite может выполнить explicit Ledger migration v1→v7 рядом с legacy tables
и сделать file-copy backup через documented API. PostgreSQL принимает только
fresh dedicated v7 schema; его backup, restore и retention policy остаются у
database operator.

Новый контракт точной очистки payload имеет отдельный named backend-neutral
runner. Он намеренно отделён от замороженного common v1 check list: доказывает
durable retry после restart, scope isolation и rejection command drift для
`MemoryWriter.payload_readback()` / `purge_payloads()`, не меняя молча смысл
старого receipt.

## Посмотреть локальный receipt

```python
from protoprompt.ledger import SqliteMemoryLedger

print(SqliteMemoryLedger.storage_capabilities().explain())
```

Static method non-I/O: он не создаёт backend, не выполняет setup, не
открывает connection, не раскрывает database path и не читает memory payloads.

## Проверить named semantic checks

SQLite:

```powershell
python -m pytest -q `
  tests/test_ledger_storage_conformance.py `
  tests/test_ledger_conformance_sqlite.py `
  tests/ledger_conformance/test_scope_payload_purge_conformance.py
```

PostgreSQL с operator-provided disposable test DSN:

```powershell
$env:PROTOPROMPT_POSTGRES_DSN = "postgresql://..."
python -m pytest -q tests/integration/test_postgres_memory_ledger.py -m integration
```

PostgreSQL integration suite намеренно содержит catalog, RLS/guard, tamper,
contention и fresh-schema проверки помимо общего profile.

## Чего conformance не доказывает

Matching capability receipt и green common checks не доказывают managed
PostgreSQL restore/PITR, filesystem/database access control, custody
checkpoint secrets, physical deletion из backups/WAL/replicas, multi-region
availability или latency/throughput parity. Это отдельные release и deployment
gates.
