# Граница API ProtoPrompt v1

В ProtoPrompt `0.20` появляется `protoprompt.api` — узкая точка импорта,
которая замораживается для линии `1.x`. До `1.0` её статус — **v1 candidate**.
В `1.0.0` эта же граница станет SemVer-stable ядром после прохождения остальных
release gate.

Старые импорты продолжают работать, но не получают неявно более сильную
гарантию совместимости `1.x`.

```python
from protoprompt.api import (
    MemoryKind,
    MemoryScope,
    MemoryWriter,
    SqliteMemoryLedger,
)

ledger = SqliteMemoryLedger("memory.db")
ledger.setup()
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="acme", user="alice", thread="support"),
)
candidate = writer.assert_candidate(
    kind=MemoryKind.FACT,
    content="Согласованный тариф поддержки — enterprise.",
    source_ref="host:crm:account-42",
)
active = writer.confirm(candidate.record_id, expected_revision=candidate.revision)
```

## Что замораживается

Установочный `api_contract_v1.json` — исполняемое release evidence, а не
автоматическая выгрузка всех имён. Он фиксирует:

- точный список экспортов `protoprompt.api`;
- публичные поля и read-методы `ContextPlan`, `MemoryRecord`, `MemoryEvent`,
  receipts и relations;
- публичные lifecycle/read/export/scoped-erasure методы `MemoryWriter`;
- `MemoryPolicy.safe_default()`, fingerprint и content-free explanation;
- значения enum для lifecycle, trust, provenance, relations и встроенных
  storage modes;
- operational setup/close boundary встроенных SQLite/PostgreSQL Ledger и
  sealed storage-conformance receipt.

`ContextPlan`, `MemoryRecord`, `MemoryEvent`, audit и erasure receipts —
result-типы. Контрактом служат их документированные публичные поля и методы;
приложение не должно вызывать их конструкторы напрямую. Private-поля с `_` в
начале в контракт не входят.

Для `MemoryPolicy` кандидатно-стабильной точкой является
`MemoryPolicy.safe_default()`. Прямая композиция собственных admission и recall
policy остаётся experimental, пока их policy language не получат отдельный
freeze.

## Что остаётся experimental

Следующие поверхности не становятся стабильными только потому, что старые
импорты продолжают работать:

| Поверхность | Статус |
|---|---|
| custom policies и review workflow в `protoprompt.ledger.admission` | experimental |
| planner, composer и checkpoints в `protoprompt.ledger.recall` | experimental |
| task-resume форматы и planner | experimental |
| lineage / working-memory research в `protoprompt.agent` | experimental |
| provider/framework integrations | optional adapters с отдельной совместимостью |
| `apps/*` | reference applications и demos |
| сторонние Ledger backends | не поддерживаются; public backend plugin protocol отсутствует |

Встроенный PostgreSQL class входит в candidate API, но его backup mode
`operator_managed` остаётся явной обязанностью оператора. API freeze не
утверждает, что уже доказаны managed backup/restore, PITR, replicas, WAL
retention или physical-media erasure.

## Политика совместимости

До `1.0` изменение candidate boundary требует явной записи в changelog и
намеренного обновления contract tests и hash манифеста. Начиная с `1.0.0`, в
рамках `1.x` документированные имена и смыслы сохраняются, кроме необходимого
fail-closed исправления безопасности. Допускаются additive методы и optional
поля, если существующие вызовы продолжают работать.

Exceptions, validation failures, scope isolation, trust transitions и deletion
semantics — часть поведения, а не implementation detail. Private methods,
внутренности БД, точный текст исключений, `repr` и прямые конструкторы
result-only типов не замораживаются.

## Проверка контракта

Манифест входит в wheel и sdist. Его чтение не открывает storage, не загружает
credentials, не вызывает сеть и не импортирует optional provider SDK:

```python
from protoprompt.api import V1_API_MANIFEST_SHA256, v1_api_manifest

manifest = v1_api_manifest()
assert manifest["status"] == "v1_candidate"
print(V1_API_MANIFEST_SHA256)
print([item["name"] for item in manifest["exports"]])
```

CI проверяет точный порядок экспортов, identity реализаций, публичные поля
dataclass, публичные members, значения enum, experimental exclusions, digest
манифеста, состав пакетов и импорт без optional dependencies.
