# Roadmap to 1.0 — ProtoPrompt

> Статус: путь от опубликованного `0.6.1` к `1.0.0`.
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
agent events/artifacts ─┼─> Memory Ledger ─┐
profile/confirmed facts ┘                  │
                                           ├─> ContextPolicy
current task + model budget ───────────────┘          │
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
- [ ] Перевести `pp-agent` на единый safe final-request path.
- [ ] Выпустить ProtoPrompt Memory Benchmark v0.1: versioned offline fixtures,
  fixed baselines (sliding window, rolling summary, vector recall, `0.6`
  pipeline) и machine-readable report.
- [ ] Подключить local Ollama/PDF app как hardened reference integration:
  final budgeting, provenance UI, scoped deletion и loopback-safe defaults.

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

- Ввести append-only `MemoryEvent` и materialized `MemoryRecord` view:
  observed, asserted, confirmed, superseded, expired, retracted.
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

- Использовать `MemoryEvent` для goal, observation, tool result, artifact
  change, outcome и human feedback.
- Добавить `Episode`: цель → действия → результат → вывод, с evidence на
  tool calls/artifacts.
- Создавать `Procedure`/playbook только из успешных или host-confirmed
  эпизодов; не превращать неудачный model output в инструкцию.
- Перенести полезные части экспериментального `WorkingMemory` (scoring,
  hot/cold zones, manifests, checkpoints) в ledger + planner.
- Добавить controlled consolidation, checkpoints/resume и policy-driven recall
  facts, episodes, procedures, RAG evidence и current task через ContextPlan.
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

## 1.0.0 release candidates — Stabilize, don't expand

После `0.9.0` начинается API freeze. Новая feature не входит в RC без
доказательства, что исправляет нарушение обещания `1.0`.

### Работа

- Зафиксировать stable public APIs: `ContextPlan`, `MemoryRecord`,
  `MemoryEvent`, `MemoryWriter`, `MemoryPolicy` и storage conformance contract.
- Явно отделить stable API от experimental/research: old `WorkingMemory`,
  policy experiments и non-core adapters не получают гарантию 1.x молча.
- Провести stress, concurrency и crash-recovery tests SQLite/Postgres.
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
