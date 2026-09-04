# Контракт политики памяти (кандидат v1)

`MemoryPolicy` — небольшой явный контракт, который связывает два разных
решения о durable memory:

1. `MemoryAdmissionPolicy` решает, можно ли подтвердить конкретный candidate
   из host-owned ingress.
2. `LedgerRecallPolicy` решает, как выбирать уже active записи в один
   ограниченный recall lane.

Это намеренно не алиасы. Admission не делает recall, а recall не подтверждает
candidate. `MemoryPolicy` даёт хосту один versioned, content-free объект для
review и явной передачи в обе границы.

Это additive **кандидат на API freeze v1**, а не workflow engine, policy
модели, автоматический ingestion или утверждение, что все существующие Ledger
адаптеры уже stable.

## Безопасный default

```python
from protoprompt.ledger import MemoryPolicy

policy = MemoryPolicy.safe_default()
assert policy.admission.allowed_origins == ("host_assertion",)
assert policy.recall.require_admission_audit is True
print(policy.explain())  # content-free receipt политики и fingerprint
```

Default допускает только прошедшие review `host_assertion` facts, decisions и
preferences с confidence не ниже `0.75`. Он сам не создаёт запись и не
подтверждает user/document/tool/model text.

## Явная интеграция хоста

Хост по-прежнему владеет scope, ingress, review и request boundary. Передавай
matching components явно и не отдавай wrapper модели или браузерному клиенту:

```python
from protoprompt import MemoryScope
from protoprompt.ledger import (
    MemoryPolicy,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import LedgerRecallPlanner

ledger = SqliteMemoryLedger("ledger.db")
ledger.setup()
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="local", user="alice", thread="chat-42"),
)
policy = MemoryPolicy.safe_default()

gate = MemoryReviewGate(
    writer,
    origin="host_assertion",
    policy=policy.admission,
)
planner = LedgerRecallPlanner(writer, policy=policy.recall)
```

Для custom policy обе components строятся намеренно. Recall component должна
быть не слабее admission; иначе `MemoryPolicy(...)` завершается ошибкой при
создании.

## Проверяемая связь

`MemoryPolicy` отклоняет пару, если нарушено хотя бы одно условие:

- recall требует immutable admission-audit evidence;
- recall задаёт concrete origins, а не legacy compatibility lane с
  неограниченными origin, и исключает `unknown`/`legacy_unknown`;
- origins и kinds recall являются подмножествами origins и kinds admission;
- minimum confidence recall не ниже minimum confidence admission.

Значит, запись, выбранная через combined contract, могла пройти paired
admission rule. Это API-shaping safety invariant, а не Python sandbox и не
система авторизации.

## Receipt и versioning

`policy.explain()` возвращает свежие JSON-safe metadata: schema wrapper-а,
identity/version политики, оба nested component receipt и deterministic
content-free fingerprint. Там нет memory text, scope, record ID, task text,
secrets или provider messages.

`schema_version`, `policy_id` и `policy_version` принадлежат wrapper-у.
Вложенные admission/recall policies сохраняют свои compatibility versions.
Сохраняй exact reviewed policy receipt рядом с конфигурацией deployment, а не
считай одинаковые человеческие имена доказательством одинаковой семантики.

## Границы

- Existing standalone `MemoryAdmissionPolicy` и `LedgerRecallPolicy` остаются
  поддерживаемыми experimental compatibility API. Они не превращаются молча в
  `MemoryPolicy`.
- `MemoryPolicy` не меняет `MemoryWriter`, не auto-wire-ит legacy vector или
  transcript storage, не решает prompt injection, не разбирает conflicts и не
  отправляет provider request.
- Хост всё ещё обязан защищать Ledger/database, назначение scope, review
  authority и checkpoint secrets. См. отдельные ограничения в руководствах
  [Memory Ledger](memory-ledger.md) и [bounded Ledger recall](ledger-recall.md).
