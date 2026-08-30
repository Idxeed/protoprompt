# Roadmap to 1.0 — ProtoPrompt

> Статус: `0.13.0` добавляет optional PostgreSQL-conformant Ledger v6 с тем же
> sync host contract, fresh dedicated schema и fail-closed storage validation;
> следующий этап — стабилизация и RC к `1.0.0`.
> Обновлён: 2026-08-30.
>
> Это не календарное обещание. Каждый minor-релиз выходит только после своих
> проверяемых критериев готовности.

## Наша категория

**ProtoPrompt — embeddable context runtime для агентов.** Он превращает
историю, документы, события агента и подтверждённые факты в объяснимый,
безопасный контекст в жёстком token budget.

Главное обещание `1.0`:

> **Retention independent of the active context window; bounded, reliable
> active context.**

Долгое хранение не означает, что модель «всегда всё помнит». В активный
prompt попадают только релевантные блоки, разрешённые политикой, с понятным
происхождением и стоимостью в конкретном бюджете.

## Граница продукта

| ProtoPrompt 1.0 владеет | ProtoPrompt 1.0 не пытается заменить |
|---|---|
| Жизненным циклом памяти, provenance, scope, trust и удалением | Agent loop, planner, tool execution и multi-agent orchestration |
| Выбором контекста и строгим token budget | Полный ingestion/RAG framework или каталог data connectors |
| Памятью фактов, решений, предпочтений, эпизодов и процедур | Универсальную graph/vector database |
| Объяснением: что, почему и за сколько токенов попало в prompt | Обязательный облачный control plane или SaaS UI |
| Тонкими bridges к фреймворкам | LangChain, LangGraph или LlamaIndex |

LangChain/LangGraph, LlamaIndex, OpenAI Agents SDK и PydanticAI — каналы
распространения, а не модель ядра. Новая интеграция до `1.0` принимается
только если проверяет memory/context contract, а не просто добавляет логотип.

## Точка старта: 0.6.1

В `0.6.1` уже есть необходимый фундамент:

- RAG, bounded document readers, session compression, profile engine и
  budgeted context;
- scope `tenant/user/thread`, content-safe telemetry и provenance;
- SQLite, PostgreSQL/pgvector, Redis, Chroma, Qdrant,
  Elasticsearch/OpenSearch;
- MCP, LangGraph, OpenAI Agents SDK, PydanticAI и LlamaIndex bridges;
- экспериментальная hot/cold agent memory с scoring, manifest и checkpoint
  export.

`0.6.1` исправил критические фундаментальные гарантии: final-request token
budget, provider-aware token counting, scoped profile identity и безопасную
проверку Responses tool graph. Он опубликован с проверенными sdist/wheel.

До `1.0` не хватает не очередного store или provider, а четырёх цельных
контрактов:

1. `ContextPlan`: воспроизводимое решение о составе prompt, а не только
   готовая строка и разрозненный report.
2. `MemoryRecord`: версия факта/решения/предпочтения с evidence, trust,
   актуальностью и связями вместо безличного vector chunk.
3. Memory lifecycle: candidate → validation → reconciliation →
   consolidation/expiry/forget/rollback без молчаливой перезаписи истории.
4. Agent memory runtime: опыт выполнения задач и playbooks как first-class
   память, а не побочный артефакт CLI.

## Инварианты 1.x

1. **Host-controlled scope.** Модель не выбирает tenant, user, thread,
   visibility или trust level.
2. **Provenance everywhere.** Каждый memory/context block имеет origin,
   scope и источник; content не экспортируется в telemetry по умолчанию.
3. **Никаких silent writes.** Данные из документа, tool output или LLM
   extraction untrusted, пока `MemoryPolicy` не подтвердит их.
4. **История важнее перезаписи.** Изменённый факт становится
   `superseded`/`retracted`, а не исчезает бесследно.
5. **Удаление полное и проверяемое.** Первичная запись, embedding, индекс и
   производные записи удаляются в соответствии с retention policy.
6. **Budget is a contract.** План учитывает system/developer messages,
   history, current user turn, tool payloads и output reserve; причина
   исключения доступна через API.
7. **Core остаётся лёгким.** Нет обязательных third-party dependencies;
   providers, storage и bridges остаются optional extras.
8. **Compatibility before elegance.** Существующие `ContextBuilder`,
   `Pipeline`, `ProfileManager` и bridges получают documented migration path.

## Целевая модель

```text
documents ───────────────┐
conversation/session ───┤
agent events/artifacts ─┼─> Memory Ledger → LedgerRecallPlanner
profile/confirmed facts ┘                         │
                                                  ├─> explicit LedgerContextComposer
current task + model budget ──────────────────────┘                 │
                                                                  v
                                                       immutable ContextPlan
                                                                  │
                                                                  v
                                                   messages + explanation + receipt
```

`Memory Ledger` — единственный durable source of truth. Working set, vector
index, summaries и framework-specific state — его projections/caches, а не
конкурирующие истины.

## 0.7.0 — Truth & Evaluation

**Цель:** сделать выбор контекста прозрачным и измеримым до изменения модели
памяти.

### Контракты

- `ContextBlockDecision`: включён, исключён, обрезан или зарезервирован;
  origin, reason code, token cost, provenance и score там, где это безопасно.
- `ContextPlan`: immutable снимок selection, renderer system prompt/message
  payload и JSON-safe `explain()` без raw prompt/document content.
- `ContextRequestReceipt`: точный final-message cost, output reserve,
  history/context/final-input totals для одного запроса.
- `ContextPolicy` и `ContextRequest` — следующий API-slice, когда legacy
  builder будет переведён на planner без feature flag.

### Работа

- [x] Исправить final-message budget, profile scope identity и provider token
  fallback в `0.6.1`.
- [x] Перевести budgeted builder на internal planner и дать additive
  `plan()` / `plan_messages()` без поломки `build()` / `build_messages()`.
- [x] Добавить `plan.explain()` и developer recipe для просмотра решения.
- [x] Ввести request-scoped receipt вместо reliance на mutable last report.
- [x] Перевести `pp-agent` на единый safe final-request path: immutable
  request plan/receipt, separate model-window budget, completion reserve и
  атомарное action-result continuation.
- [x] Выпустить ProtoPrompt Memory Benchmark v0.1: versioned offline fixtures,
  fixed baselines (sliding window, rolling summary, vector recall, `0.6`
  pipeline) и machine-readable report.
- [x] Подключить local Ollama/PDF app как hardened reference integration:
  final budgeting/receipt, provenance UI, append-only conversation archive с
  crash-safe ledger, scoped deletion и loopback-safe defaults.

### Release gate

- Одинаковый request и candidate snapshot дают одинаковый selection без LLM.
- Ни один deterministic plan не превышает input budget; output reserve есть в
  receipt.
- У каждого included/excluded block есть origin, token cost и reason code.
- Existing public API и contract tests проходят без behavioral regression.
- Benchmark полностью offline, seeded, versioned и воспроизводим; baseline
  заморожен до оптимизаций `0.8`.

## 0.8.0 — Memory Semantics & Lifecycle

**Цель:** превратить durable memory из набора summaries/vector chunks в
управляемую, версионируемую предметную модель.

### Каноническая запись

`MemoryRecord` содержит как минимум:

- `kind`: `fact`, `decision`, `preference`, `episode`, `procedure`;
- record id, schema version, owner/scope и host-controlled trust level;
- content или content reference, content hash и source/evidence references;
- confidence, timestamps, validity interval и retention policy;
- lifecycle state: `candidate`, `active`, `superseded`, `retracted`,
  `expired`, `quarantined`;
- typed relations: `supersedes`, `derived_from`, `supports`, `contradicts`.

### Работа

- [x] Введён первый изолированный experimental SQLite slice: content-free
  `MemoryEvent` append-only в normal operation (кроме controlled migration
  scrub и hard-erase redaction), локальная lifecycle projection `MemoryRecord`,
  pinned `MemoryWriter`, optimistic revision/idempotency и lifecycle `observed`,
  `asserted`, `confirmed`, `superseded`, `expired`, `retracted`, `forgotten`,
  `quarantined`; atomic scoped source revocation, hard-erase replay barriers и
  fail-closed explicit SQLite migrations с exact table/index/trigger checks и
  revalidation после write lock. Он не делает silent dual-write в legacy
  vector/profile/session API.
- Сделать projection полностью rebuildable из event history без возврата
  plaintext/source/evidence в append-only log.
- Ввести `MemoryWriter` и `MemoryPolicy`: extract → validate → reconcile →
  accept/quarantine/reject.
- Реализовать deterministic conflict handling: пользователь переехал,
  решение отменено, предпочтение изменилось, источник отозван.
- Добавить `forget(record_id)`, `forget_by_source(...)` и scoped deletion с
  проверяемым каскадом в indexes/projections.
- Сделать schema-versioned SQLite implementation и PostgreSQL/pgvector
  implementation; добавить export, dry-run migration, backup и rollback.
- Дать legacy `ProfileManager`, session summaries и `MemoryService` adapters к
  ledger без внезапной миграции данных пользователей.
- Ввести quarantine и safe rendering/data delimiters для PDF, tool output и
  LLM extraction; untrusted content не получает system priority.

### Release gate

- `superseded`, `retracted`, `expired` и `quarantined` records не попадают в
  default `ContextPlan`.
- Conflict suite детерминированно проходит для всех lifecycle transitions.
- Удаление очищает primary store, vector index и derived records.
- Scope isolation покрыта property/integration tests для SQLite и Postgres.
- Миграция `0.6 → 0.8` имеет dry-run, backup/export и rollback path.

## 0.9.0 — Agent Memory Runtime

**Цель:** сделать память полезной для выполнения и продолжения задач агента,
а не только для воспоминания диалога.

### Работа

- [x] В `0.9.0` добавить первый experimental read-path:
  `LedgerRecallPlanner` читает только active, host-confirmed records из
  pinned `MemoryWriter`, детерминированно ранжирует их локально и упаковывает
  целиком в отдельный JSON data lane с token/byte receipt. Перед возвратом
  context он заново проверяет record/revision и fail-closed после
  forget/retract/expiry/erase; не вызывает LLM/embeddings/vector API и не меняет legacy
  `WorkingMemory`/`ContextPlan`.
- Использовать `MemoryEvent` для goal, observation, tool result, artifact
  change, outcome и human feedback.
- Добавить `Episode`: цель → действия → результат → вывод, с evidence на
  tool calls/artifacts.
- Создавать `Procedure`/playbook только из успешных или host-confirmed
  эпизодов; не превращать неудачный model output в инструкцию.
- Перенести полезные части экспериментального `WorkingMemory` (scoring,
  hot/cold zones, manifests, checkpoints) в ledger + planner.
- [x] Добавить узкий sealed checkpoint/resume только для strict Ledger
  selection: v0.12 хранит opaque host reference и HMAC-sealed selection
  manifest, а после restart требует fresh plan с точным policy/counter/budget
  receipt и тем же selection. Это не checkpoint agent workflow/state.
- Добавить controlled consolidation и policy-driven composition для facts,
  episodes, procedures, RAG evidence и current task. Узкий admitted Ledger
  JSON → request bridge выпущен отдельно в `0.11.0`; широкая composition
  policy и скрытая вставка в system prompt не являются его следствием.
- Перевести bridges к LangGraph, OpenAI Agents SDK, PydanticAI и LlamaIndex на
  единый contract там, где это не ломает public semantics.
- Выпустить ProtoPrompt Memory Benchmark v1.0 как regression gate.

### Benchmark cases

- delayed recall через 100–1000 ходов;
- updates, retractions и contradictory facts;
- cross-thread preference и scope/tenant leakage;
- prompt injection из PDF/tool output в memory;
- resume многошаговой agent task после restart;
- качество и стоимость при фиксированных 1k/2k/4k budgets;
- latency planning/retrieval и полнота удаления.

### Release gate

- Reference agent завершает checkpointed task после restart, поднимая только
  релевантные facts/episodes/procedures.
- Benchmark offline, seeded и versioned; held-out cases/baselines frozen до
  tuning policies.
- На reference setup: не менее `+15` percentage points к sliding-window
  baseline по delayed recall при равном бюджете и без регрессии к `0.6`.
- Conflict suite показывает `≤2%` contradiction rate; scope/security suite:
  `0` cross-scope leaks и `0` untrusted records с system-priority.
- Planning overhead p95 не выше 50 ms на 10k local records без remote I/O и
  embedding call на зафиксированной reference configuration.

## 0.10.0 — Admission provenance boundary

**Цель:** сделать происхождение memory candidate проверяемым до того, как
concrete-origin запись станет active и попадёт в bounded recall.

### Выполнено

- [x] Добавлены закрытые `MemoryOrigin`, консервативная versioned
  `MemoryAdmissionPolicy`, scope/origin/policy-pinned `MemoryReviewGate` и
  narrow ingress: модельный transport передаёт только `content`, а host
  владеет scope, source, confidence, event ID и решением.
- [x] `review()` не пишет в storage; sealed in-process review применяет только
  исходный gate как `allow → active`, `quarantine → quarantined` или
  `reject → forget`. Stale/revoked/expired/forged/cross-gate reviews fail
  closed.
- [x] Schema v5 добавляет immutable origin/audit sidecars. Concrete active
  memory попадает в recall только после точной проверки парного `allow` audit
  и lifecycle event; SQLite guards защищают update, delete и
  `INSERT OR REPLACE`, а hard erase — единственный controlled cascade.
- [x] v4→v5 migration add-only: live payload получает лишь `legacy_unknown`,
  events не переписываются, audit не выдумывается. Compatibility recall для
  legacy active сохраняется; strict deployment должен quarantine/re-admit.
- [x] Зафиксированы regression cases для forged audit, write-lock expiry,
  restart recovery, cross-scope/gate reviews, hard erase и RPC transport shape.

### Неподвижная граница

Это не Python sandbox: arbitrary in-process plugin и прямой SQLite writer вне
модели угроз admission API. До `1.0` production adapter, который принимает
модельный input, должен оставаться JSON/RPC boundary; для более сильной
tamper-evidence понадобится внешний ключ/signing или process isolation.

## 0.11.0 — Trusted Ledger-to-request composition

**Цель:** дать host-у единственный узкий, проверяемый путь от admitted Ledger
recall к точному provider request, не превращая память в system instructions.

### Выполнено

- [x] Добавлен experimental `LedgerContextComposer`: builder и planner должны
  совпадать по непустому `MemoryScope` и экземпляру `TokenCounter`; composer
  принимает только strict `require_admission_audit=True` recall policy.
- [x] Raw `unknown` и мигрированный `legacy_unknown` исключаются из composed
  request. Concrete origins остаются за immutable admission audit Ledger.
- [x] Выбранный JSON попадает лишь в фиксированный `user` data message; перед
  ним находится content-free static system guard. Lane располагается после
  generated system context и до history/tool graph, без разрыва call/output.
- [x] Весь lane резервируется раньше optional RAG/session/history. Добавлены
  `ContextDataLaneReceipt`, content-free composition receipt и точный
  `ContextRequestReceipt`; shortage lane сообщает `ledger_data`, не маскируя
  independently oversized final turn/tool dependency.
- [x] После async context work выполняется финальный `resolve()` того же
  Ledger snapshot: forget/retract/expiry/revision race даёт
  `StaleMemoryPlanError`, а не устаревший provider payload.

### Неподвижная граница

Composer не пишет Ledger и не отправляет модельный request сам; переданный
builder может делать RAG/embedding work. Он не auto-wired в Ollama app, CLI,
`MemoryService`, profile/session/vector paths и не создаёт general-purpose
host-message API. Это не claim о prompt-injection immunity или universal
recall quality.

## 0.12.0 — Sealed Ledger selection checkpoints

**Цель:** безопасно пережить restart одного строго ограниченного Ledger recall
решения, не сериализуя контекст, agent state или чувствительные payload.

### Выполнено

- [x] Добавлены `LedgerRecallCheckpoint` и `LedgerRecallResume`: durable
  manifest содержит лишь opaque checkpoint/continuation references, policy и
  counter identity, budgets/receipt и private selection markers. Task, raw
  memory, provider messages, tool output и process-local plan в SQLite не
  пишутся.
- [x] `checkpoint()` доступен только для admission-audited strict policy и
  требует stable host-owned `checkpoint_secret`; HMAC manifest проверяется до
  любого resume. Public receipt и `explain()` не раскрывают secret, scope,
  task, record IDs или payload.
- [x] После restart `resume_checkpoint()` строит fresh plan со stored token/
  byte budgets и принимает его лишь при полном совпадении policy, counter,
  selection, revision/content hash и used receipt. Изменение или lifecycle
  stale state invalidates checkpoint и fail-closed.
- [x] Schema v6 добавляет immutable manifest/selection sidecars. Любой
  lifecycle transition выбранной записи, включая hard erase, в той же Ledger
  transaction инвалидирует checkpoint и удаляет private selection markers.
- [x] `plan_checkpoint_messages()` использует тот же bounded request path и
  final resolve, что v0.11; resume task привязан к `ContextInput.query`.
  Frozen offline v0.3 suite фиксирует restart, tamper, lifecycle и
  query-binding/composition cases (4 сценария / 13 checks).

### Неподвижная граница

Это sealed selection manifest, а не durable agent state: здесь нет lease,
exactly-once delivery, workflow engine, tool replay, provider continuation или
автоматического подключения к CLI/Ollama app. Внешний signer/process isolation
по-прежнему нужны, если модель угроз включает arbitrary in-process code или
прямой SQLite access.

## 0.13.0 — PostgreSQL Ledger v6 Conformance

**Цель:** перенести уже доказуемый host-owned Ledger contract на PostgreSQL,
не превращая storage adapter в новый agent framework или мнимый throughput
benchmark.

### Выполнено

- [x] Добавлен optional experimental `PostgresMemoryLedger` через
  `protoprompt[postgres]`. Его `MemoryWriter`-совместимая sync command surface
  сохраняет v6 lifecycle, admission, strict recall и sealed checkpoint
  semantics; constructor не выполняет DDL, а setup остаётся явным.
- [x] Поддерживается только fresh v6 в иначе пустой выделенной schema: нет
  silent SQLite import, migration старого PostgreSQL Ledger, destructive
  downgrade или ложного file-copy `backup()`. Backup/restore/PITR остаются у
  оператора PostgreSQL.
- [x] Для финальной lifecycle/checkpoint validation PostgreSQL write path
  сериализуется одним transaction-scoped advisory lock на schema; timeout 5 s
  возвращает retryable `LedgerConflictError` для повторения целой
  идемпотентной host-команды с тем же `event_id`.
- [x] Setup/operation validation fail-closed проверяет tables, columns,
  BIGSERIAL, deterministic text collation, indexes/constraints, guard
  functions/triggers, RLS/policies, DML rewrite rules,
  inheritance/partitioning и schema shadowing. Runtime ищет built-ins через
  `pg_catalog` первым; hard-erase GUC выключен вне единственного controlled
  path.
- [x] Общая public conformance suite запускается для SQLite и PostgreSQL;
  PostgreSQL release gate содержит пятнадцать live integration cases для
  parity, restart/contention, tamper/recovery и checkpoint boundary.

### Неподвижная граница

Это sync storage conformance, а не asynchronous repository, multi-tenant
authorization layer, PostgreSQL migration toolkit, performance claim или
защита от database owner/роли с arbitrary DDL/DML. Private database connection
не выдаётся plugin-коду; production роль и schema ownership остаются частью
операционной модели хоста.

## 1.0.0 release candidates — Stabilize, don't expand

После `0.9.0` начинается API freeze. Новая feature не входит в RC без
доказательства, что исправляет нарушение обещания `1.0`.

### Работа

- Зафиксировать stable public APIs: `ContextPlan`, `MemoryRecord`,
  `MemoryEvent`, `MemoryWriter`, `MemoryPolicy` и storage conformance contract.
- Явно отделить stable API от experimental/research: old `WorkingMemory`,
  policy experiments и non-core adapters не получают гарантию 1.x молча.
- Провести stress, concurrency и crash-recovery tests SQLite/Postgres.
- Сохранить PostgreSQL Ledger catalog/tamper conformance как обязательный
  release gate и подтвердить recovery на управляемом PostgreSQL deployment.
- Добавить fuzz/property tests для scope, lifecycle transitions, deletion и
  token packing.
- Провести security review: provenance spoofing, prompt injection, stale
  records, PII/redaction, authorization и data erasure.
- Проверить wheels/sdists, Python 3.11–3.13, lazy extras, RU/EN docs и
  compatibility matrix.
- Получить три independent reference installs: local Ollama/PDF, framework
  agent и multi-tenant Postgres.

### RC exit gate

- Нет unresolved P0/P1 в lifecycle, scope, deletion или budget.
- Migration guide от `0.6` проверен на fixtures/reference apps.
- Все benchmark/security regression gates зелёные два релизных цикла подряд.
- Composed Ledger lane проходит no-raw/legacy provenance, no-payload-in-
  `explain`, exact receipt reconciliation, tool dependency, explicit
  `ledger_data` boundary и stale lifecycle race regressions.
- Sealed checkpoint resume проходит restart, HMAC-tamper, policy/counter
  drift, query-binding, lifecycle invalidation и hard-erase sidecar-scrub
  regressions; он по-прежнему не объявляется agent/workflow checkpoint.
- Schema migrations имеют forward path и documented rollback; data loss не
  допускается как побочный эффект обычного upgrade.

## 1.0.0 — Stable Context Runtime

`1.0.0` выходит только тогда, когда можно честно обещать:

- lifecycle и meaning stable core types не ломаются в `1.x` без SemVer major;
- каждый ContextPlan соблюдает budget и объясняет selection;
- scope, provenance, trust и deletion имеют documented semantics;
- durable storage schema versioned, migratable и testable;
- core остаётся zero-dependency, adapters не определяют модель ядра;
- пользователь выбирает только нужный слой: context planning, durable memory
  или framework bridge.

## Сквозные метрики

| Направление | Target к 1.0 |
|---|---|
| Budget | 0 violations в deterministic suite |
| Explainability | 100% plan decisions имеют source, reason и token cost |
| Safety | 0 cross-scope leaks; default policy не recalls inactive/quarantined records |
| Deletion | 100% primary/index/derived deletion в integration suite |
| Memory quality | `+15 pp` vs sliding window на frozen delayed-recall setup |
| Contradictions | `≤2%` на frozen conflict suite |
| Runtime | p95 planning `≤50 ms` на 10k local records без remote I/O |
| Compatibility | Python 3.11–3.13, dependency-free core, documented 0.6 migration |
| Adoption | 3 independently reviewed reference integrations |

Метрики — regression contracts на зафиксированной reference configuration, а
не универсальные маркетинговые обещания для любой модели и базы данных.

## Что сознательно остаётся вне 1.0

- массовый каталог provider/vector DB/framework integrations;
- workflow/orchestration engine, tool runner и planning framework;
- hosted multi-tenant control plane;
- universal multimodal/graph RAG;
- self-modifying memory policies без host confirmation;
- заявления о «perfect recall» или «infinite context».

Такие вещи могут вернуться после `1.0` только как RFC, если усиливают
lifecycle, planning или evaluation, а не размывают категорию.
