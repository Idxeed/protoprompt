# Bounded recall из ledger *(experimental)*

`protoprompt.ledger.recall` — первый read-путь от долговременной Ledger-памяти
к текущей задаче агента. Компонент намеренно небольшой и локальный:

- читает только `active`, подтверждённые host-ом, ещё валидные записи с
  payload из одного pinned `MemoryWriter`;
- ранжирует локально и детерминированно по лексическому соответствию,
  confidence и свежести — без LLM, embeddings, vector query, сети и legacy API;
- упаковывает записи целиком в фиксированные token **и** UTF-8 byte budgets,
  включая полный JSON envelope;
- выполняет финальную проверку выбранных record ID и ревизий непосредственно
  перед возвратом контекста. Забытая, отозванная, истёкшая, удалённая или
  изменённая запись делает resolution fail-closed: нужен новый план.

Это не обещание бесконечного контекстного окна. Это ограниченный **memory data
lane**. Standalone planner оставляет его отдельными данными; для host-а, которому
нужен точный provider request, v0.11 добавляет явный experimental
`LedgerContextComposer`.

Для concrete v5 ingress origin active reader проверяет парный immutable
`allow` audit до попадания record в этот lane. Записи, мигрированные из схемы
до v5, получают `legacy_unknown` и остаются recallable только ради
совместимости; strict deployment должен quarantine-ить и re-admit-ить их до
включения recall. Raw `unknown` writer records тоже являются trusted legacy
escape hatch, а не provenance-reviewed memory от модели.

## Быстрый старт

Сначала создайте и проведите запись через host-owned v0.10 admission boundary:

```python
from protoprompt.ledger import (
    MemoryAdmissionAction,
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import LedgerRecallPlanner, StaleMemoryPlanError
from protoprompt.scope import MemoryScope

ledger = SqliteMemoryLedger("memory-ledger.db")
ledger.setup()
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="local", user="alice", thread="agent-42"),
)

gate = MemoryReviewGate(
    writer,
    origin=MemoryOrigin.DOCUMENT,
    policy=MemoryAdmissionPolicy(
        policy_id="artifact-facts-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.DOCUMENT,),
        minimum_confidence=0.8,
    ),
)
candidate = gate.ingress(
    kind=MemoryKind.FACT,
    source_ref="artifact:checkpoint-manifest",
    confidence=0.9,
).submit("Восстановление checkpoint начинается с durable manifest.")
review = gate.review(candidate.record_id)
assert review.action is MemoryAdmissionAction.ALLOW
gate.confirm(review, event_id="admission:checkpoint-manifest:allow")

planner = LedgerRecallPlanner(writer)
plan = planner.plan(
    task="починить восстановление checkpoint",
    token_budget=600,
    byte_budget=32_768,
)

try:
    memory_data = planner.resolve(plan).render_data()
except StaleMemoryPlanError:
    # Выбранная запись изменилась или больше не допустима. Планируем заново.
    memory_data = planner.resolve(
        planner.plan(
            task="починить восстановление checkpoint",
            token_budget=600,
            byte_budget=32_768,
        )
    ).render_data()
```

`memory_data` — канонический JSON envelope:

```json
{
  "records": [
    {
      "content": "Восстановление checkpoint начинается с durable manifest.",
      "kind": "fact"
    }
  ],
  "schema_version": 1,
  "type": "protoprompt.ledger-recall"
}
```

В нём намеренно нет record ID, scope, content hash, source reference и
evidence reference. Модель получает справочные данные, а не инструмент для
изменения Ledger.

## Граница trusted composition

Сам `LedgerRecallPlanner` **не** изменяет `WorkingMemory`, `MemoryService`,
legacy `ContextPlan` или system prompt модели и не вызывает провайдера. Это
по-прежнему корректный выбор, когда host сам владеет placement данных и
окончательным accounting.

Для узкого случая «admitted Ledger JSON → один готовый provider request» есть
`LedgerContextComposer`. Это host-owned, явный opt-in bridge — не автоматическое
поведение Ledger, `pp-agent` или `pp-ollama-chat`. Он требует у
`TokenBudgetedContextBuilder` и `LedgerRecallPlanner` один и тот же непустой
`MemoryScope` и **тот же экземпляр** `TokenCounter`; planner обязан использовать
`LedgerRecallPolicy.admission_safe_default()` или policy с
`require_admission_audit=True`.

Composer помещает payload памяти только в один `user` JSON message. Перед ним
стоит фиксированный system guard без текста памяти; generated system context
не содержит raw Ledger payload. Пара guard+JSON располагается после generated
system context (если он есть) и до history/tool graph, поэтому не разрывает
tool call/result. Полный lane обязателен: он резервируется до optional
RAG/session/history и не обрезается молча. Нехватка места вызывает
`TokenBudgetExceededError(..., "ledger_data")`.

```python
from protoprompt import ContextInput, InMemStore, TokenBudgetedContextBuilder
from protoprompt.ledger.recall import (
    LedgerContextComposer,
    LedgerRecallPlanner,
    LedgerRecallPolicy,
    StaleMemoryPlanError,
)
from protoprompt.tokens import RegexTokenCounter

# async host handler
counter = RegexTokenCounter()
builder = TokenBudgetedContextBuilder(
    InMemStore(),
    embedding_client,
    counter=counter,
    max_tokens=4_096,
    scope=writer.scope,
)
planner = LedgerRecallPlanner(
    writer,
    policy=LedgerRecallPolicy.admission_safe_default(),
    counter=counter,
)
composer = LedgerContextComposer(builder, planner)

try:
    request = await composer.plan_messages(
        ContextInput(
            query="починить восстановление checkpoint",
            system_prompt="Следуй контракту хоста.",
            include_session=False,
        ),
        user_message="Что делать дальше?",
        ledger_token_budget=600,
    )
except StaleMemoryPlanError:
    # Lifecycle memory изменился во время async context/RAG work. Планируем заново.
    raise

messages = request.render_messages()  # немедленно отправьте своему provider client
receipt = request.receipt              # точный budget всего сообщения
audit = request.explain()              # content-free metadata
```

Composer сам не пишет Ledger и не отправляет chat request. Однако переданный
`TokenBudgetedContextBuilder` может асинхронно выполнять RAG/embedding work;
после этой работы composer ещё раз вызывает `resolve()` и fail-closed при
stale record/revision/expiry.

При сериализации `<`, `>` и `&` в memory content экранируются, чтобы запись не
могла видимо закрыть внешний XML/HTML-подобный wrapper. Это лишь defense in
depth, а не замена trusted data boundary: текст памяти может содержать
недоверенные инструкции и должен оставаться данными. Содержимое durable record
нельзя считать system instructions, а модели нельзя выдавать writer или
lifecycle methods как tools.

## Политика выбора

`LedgerRecallPolicy.safe_default()` допускает только `fact`, `decision` и
`preference` с confidence не ниже `0.5`. `episode` и `procedure` по умолчанию
исключены. Приложение может opt-in только через явную host policy с evidence
и risk contract, подходящими для этих более богатых memory kinds.

Это compatibility-policy standalone planner: она всё ещё может читать legacy
`unknown` и `legacy_unknown` records. Для composed provider request используйте
`LedgerRecallPolicy.admission_safe_default()`. Она включает
`require_admission_audit=True`, поэтому оба provenance без review-а исключаются;
concrete origins дополнительно проходят audited active-reader invariant Ledger.

```python
from protoprompt.ledger import MemoryKind
from protoprompt.ledger.recall import LedgerRecallPlanner, LedgerRecallPolicy

policy = LedgerRecallPolicy(
    policy_id="ops-episodes-v1",
    allowed_kinds=(MemoryKind.FACT, MemoryKind.DECISION, MemoryKind.EPISODE),
    minimum_confidence=0.8,
    active_read_limit=1000,
    candidate_limit=100,
    candidate_scan_byte_budget=1_048_576,
)
planner = LedgerRecallPlanner(writer, policy=policy)
```

Это immutable local selection policy, а не admission policy. Она не может
подтвердить candidate. Admission v0.10 — отдельный host-side
`MemoryReviewGate`: он фиксирует origin и policy до входа текста в Ledger, а
затем записывает sealed `allow` / `quarantine` / `reject` decision. Он никогда
не позволит model output автоматически превратиться в active memory. Граница
RPC-only и recovery описаны в [Memory Ledger admission](memory-ledger.md#admission-boundary-v010).

При фиксированных task, host-controlled clock, active Ledger snapshot, policy
и token counter выбор воспроизводим. Ранжирование использует сначала
лексическое совпадение, затем host-set confidence и свежесть для tie-break.
Вызывающий код не может передать собственное время и задним числом обойти
expiry; фиксированный clock допустим только в trusted test/replay harness.

Planner читает не более `active_read_limit` локальных active records (по
умолчанию 1 000). SQLite materializes payloads для этого bounded active read;
фильтр kind/confidence применяется до lexical ranking и candidate-content scan
budget, затем рассматривается не более `candidate_limit` допустимых records
(по умолчанию 100). `active_record_count`, `active_read_limit_reached`,
`eligible_record_count` и `candidate_limit_reached` делают эту границу видимой
в receipt. Active read, точно дошедший до limit, помечается как потенциально
обрезанный вместо ложного заявления о полном глобальном поиске. Budget сырых
байтов кандидатов применяется только к eligible candidates: слишком большая
запись получает `scan_byte_budget` и не останавливает рассмотрение более
маленьких поздних записей.

## Budget receipt и свежесть

`LedgerRecallPlan` не содержит plaintext памяти или task text. Он хранит лишь
private snapshots record/revision и content-free receipt:

```python
plan.explain()
# {
#   "policy_id": "ledger-recall-safe-v1",
#   "used_tokens": 118,
#   "used_bytes": 441,
#   "remaining_tokens": 482,
#   "remaining_bytes": 32327,
#   "selected_count": 2,
#   "decisions": [...],
# }
```

Receipt также несёт полную content-free конфигурацию policy и её fingerprint,
а также `counter_id`. У встроенного счётчика это `regex-token-counter-v1`; при
своём счётчике приложение должно передать versioned `counter_id`, чтобы
сохранённый receipt явно указывал контракт accounting.

Сам `LedgerRecallPlan` — in-process capability, привязанный к экземпляру
planner, а не переносимый checkpoint: для аудита сохраняйте `plan.explain()`,
а после restart или handoff создавайте свежий plan.

`used_tokens` — результат выбранного `TokenCounter` на полном prospective JSON
envelope. `used_bytes` — strict UTF-8 длина того же envelope. Planner никогда
не обрезает запись: она попадает целиком или получает `over_token_budget` /
`over_byte_budget`; после этого всё равно рассматриваются более маленькие
поздние записи.

Авторитетен полный `used_tokens` envelope. Per-record token cost в receipt —
неотрицательная incremental allocation величина, поэтому их сумма может не
совпасть точно, если инъецированный детерминированный tokenizer даёт
немонотонный count для двух разных JSON strings.

`resolve()` сначала читает, рендерит и учитывает candidate data вне SQLite
writer lock. Затем он берёт короткую exclusive Ledger lifecycle boundary и
повторно читает active snapshot, проверяя ID, ревизии, content hash и kind
выбранных records непосредственно перед возвратом контекста. Поэтому
инъецированный token counter не выполняется внутри database transaction.
Свежий host time проверяется и до, и после короткой boundary: запись, у
которой срок истёк во время accounting или ожидания lock, также отклоняется.
Если concurrent `forget()`/`retract()` успел раньше этой
финальной проверки, либо
запись уже была expired, superseded, hard-erased, не попала в bounded read,
сменила ревизию или content hash, возникает `StaleMemoryPlanError`, а не
возвращается устаревший текст. Перед каждым model send нужно заново
спланировать и разрешить данные.

У composed request есть ещё один короткий final boundary: после асинхронного
context planning и точной проверки отрендеренных messages composer повторно
`resolve()`-ит исходный plan. Изменение выбранной памяти превращается в
`StaleMemoryPlanError`, а не в отправку старого JSON.

`LedgerCompositionReceipt` связывает policy/fingerprint/counter и факт этой
final validation. `ContextPlan.data_lanes` содержит content-free
`ContextDataLaneReceipt` (количество records, bytes и transport tokens).
`ContextRequestReceipt.input_tokens` остаётся единственным authoritative total
всего provider request: lane receipt объясняет его часть, но не заменяет exact
count для полного message list.

## Scope и граница удаления

Planner получает `MemoryWriter`, а не параметр scope, поэтому не может
расширить память Alice до scope Bob. Его active-memory reader path проверяет
lifecycle Ledger и точный host scope.

Планирование не делает записей. `LedgerRecallPlan` не копирует plaintext, но
короткоживущий `LedgerRecallContext`, возвращённый `resolve()`, неизбежно
содержит отрендеренные данные в памяти процесса. Приложение, которое сохраняет
или отправляет эту строку, отвечает за собственную process/provider retention
boundary. `forget()` и `erase()` по-прежнему удаляют live Ledger payload в
пределах, описанных в [Memory Ledger guide](memory-ledger.md), но не могут
ретроспективно стереть context string, уже возвращённую или отправленную
куда-либо приложением.

То же относится к `LedgerComposedRequest`: он transient и должен быть отправлен
сразу. Его final validation действует до возврата из `plan_messages()`; поздний
`forget()` не может отозвать JSON, который host уже передал provider-у.
