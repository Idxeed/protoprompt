# Roadmap protoprompt

> Реализация кандидата `0.6.0` после публичного релиза `0.3.0`.
> Кандидат ещё не опубликован. Обновлён: 2026-08-28.
> Полная карта кандидатов и критерии интеграций находятся в
> [INTEGRATIONS.md](INTEGRATIONS.md).

## Видение

`protoprompt` остаётся небольшим независимым движком контекста и памяти.
Следующий цикл расширяет не число внутренних алгоритмов, а количество мест,
где существующие RAG, session memory, profile, token budget и provenance можно
использовать без переписывания приложения.

Главный пользовательский тест roadmap:

> Разработчик существующего бота или агента подключает protoprompt за один
> вечер, видит, что именно было вспомнено и почему, и может заменить локальное
> хранилище production-backend без изменения логики памяти.

## Текущее состояние

- `0.3.0` опубликован в PyPI, `0.6.0` собран как непубличный кандидат;
- core не имеет обязательных сторонних зависимостей;
- release gates `0.4.0`–`0.6.0` реализованы и локально проверены;
- CI покрывает Python 3.11–3.13, wheel, интеграционный контур и RU/EN docs;
- готовы RAG, session compression, profile engine, token budget, provenance,
  runtime bridges, production backends, providers, readers и cloud secrets;
- `pp-agent` остаётся экспериментальным потребителем core API.

Завершённый roadmap профиля, secrets и RAG сохранён в истории Git:
<https://github.com/Idxeed/protoprompt/blob/v0.3.0/ROADMAP.md>.

## Принципы очереди

1. Сначала стабильный контракт, потом adapters.
2. Один стандартный bridge ценнее пяти provider-specific wrappers.
3. Core остаётся dependency-free; всё внешнее — extras + lazy imports.
4. Каждый milestone заканчивается runnable use case, а не только API.
5. Интеграция без contract tests и владельца не считается поддерживаемой.
6. Alpha breaking change допустим только с явной выгодой и migration path.

## 0.3.x — стабилизация

Цель: собрать реальные сигналы после первого крупного публичного релиза.

- [ ] завести issue templates для bug/integration request;
- [ ] добавить compatibility smoke test минимальных версий extras;
- [ ] исправлять только release blockers и документацию;
- [ ] не добавлять новые абстракции в `0.3.x`;
- [ ] собрать минимум три внешних сценария использования или интервью.

Выход: известные дефекты `0.3` закрыты, API-разрез `0.4` подтверждён примерами.

## 0.4.0 — Integration Foundation

Цель: подготовить честные узкие контракты для внешних экосистем.

### F1. Разделение model capabilities

- [x] `ChatClientProtocol`;
- [x] `EmbeddingClientProtocol`;
- [x] совместимый composite `LLMClientProtocol`;
- [x] builders принимают минимально необходимую capability;
- [x] embedding-only clients больше не содержат fake `chat()`;
- [x] migration guide для пользовательских clients.

### F2. Scope и namespace

- [x] `MemoryScope(tenant, user, thread, kind)`;
- [x] единое отображение scope в metadata;
- [x] тесты изоляции и удаления;
- [x] host-controlled scope для model-facing adapters.

### F3. Adapter contract kit

- [x] reusable tests для chat, embeddings, vector/profile/secret stores;
- [x] проверки sync/async semantics;
- [x] проверки lazy import и missing-extra errors;
- [x] contributor guide «как добавить интеграцию».

### F4. Typed observability events

- [x] события context/retrieve/compress/profile/recall/evict/cache;
- [x] существующие hooks работают поверх нового слоя;
- [x] redaction policy по умолчанию;
- [x] trace/scope correlation без хранения содержимого.

**Release gate:** полный текущий test suite зелёный; публичные символы `0.3`
работают с deprecation path; docs RU/EN и wheel smoke обновлены.

## 0.4.1 — Connectivity

Цель: подключить protoprompt к трём главным входным экосистемам и показать
понятный продуктовый сценарий.

### C1. MCP

- [x] `protoprompt[mcp]`;
- [x] tools `remember/search/forget/profile/explain/budget_report`;
- [x] read-only resources profile/manifest/last-report;
- [x] stdio + Streamable HTTP;
- [x] scope injection со стороны host;
- [x] in-process tests официального MCP client/server.

### C2. OpenAI Agents SDK

- [x] `ProtoPromptSession`;
- [x] session input callback для budgeted context;
- [x] сохранение `pop_item`/clear semantics;
- [x] пример plain session vs protoprompt recall.

### C3. LangGraph

- [x] store adapter;
- [x] готовый `build_context` node;
- [x] sync/async graph tests;
- [x] пример thread memory + cross-thread profile.

### C4. Telegram memory bot

- [x] runnable aiogram 3 application;
- [x] SQLite default, OpenAI/Ollama switch;
- [x] `/memory`, `/why`, `/forget`;
- [x] synthetic long-dialog scenario;
- [x] воспроизводимое сравнение FIFO/LRU;
- [x] короткое видео/GIF для README и релизного поста.

**Release gate:** новый пользователь поднимает Telegram demo по инструкции в
чистом окружении; MCP Inspector видит tools/resources; оба framework adapters
проходят upstream-compatible contract tests.

## 0.4.2 — Production Backends

Цель: убрать локальную SQLite как обязательный предел production-сценариев.

### P1. PostgreSQL/pgvector

- [x] `PgVectorStore`;
- [x] async-first implementation;
- [x] metadata filters и score threshold;
- [x] explicit migrations/setup;
- [x] `PostgresProfileStore` с optimistic locking;
- [x] Docker integration tests.

### P2. OpenTelemetry

- [x] OTEL spans для typed events;
- [x] безопасные default attributes;
- [x] пример Langfuse/Jaeger collector;
- [x] latency/token/eviction dashboard recipe.

### P3. Redis

- [x] vector store либо решение оставить vector retrieval Postgres;
- [x] embedding cache с TTL;
- [x] ephemeral session/profile adapter;
- [x] concurrency и reconnect tests.

**Release gate:** Postgres survives restart/concurrent updates; migrations
отделены от startup; telemetry не экспортирует content/secrets по умолчанию.

## 0.5.0 — Providers and Frameworks

Цель: покрыть native APIs, которые generic OpenAI-compatible client не
представляет честно.

- [x] Anthropic chat client;
- [x] Google GenAI chat + embeddings;
- [x] Amazon Bedrock Converse client;
- [x] provider-aware token counters;
- [x] PydanticAI adapter;
- [x] LlamaIndex bridge;
- [x] Google ADK spike и решение о поддержке;
- [x] provider conformance matrix в документации.

Native adapter не принимается, если он лишь переименовывает параметры
существующего compatible client и не добавляет auth/capability/semantics.

## 0.6.0 — Data and Enterprise

Цель: подключить существующие данные и инфраструктурные сервисы.

- [x] text/Markdown/source readers;
- [x] optional PDF/DOCX/HTML readers;
- [x] LlamaIndex/Unstructured document converters;
- [x] Elasticsearch/OpenSearch adapter;
- [x] минимум два cloud secret stores: AWS Secrets Manager и GCP Secret Manager;
- [x] FastAPI service recipe;
- [x] Slack/Discord gate рассмотрен: example не добавлен без подтверждённого спроса.

URL/cloud readers проходят отдельный security review: SSRF, размер, MIME,
архивные бомбы и provenance источника.

## Research backlog — без обещания версии

- hybrid sparse+dense retrieval;
- reranker adapters Cohere/Voyage;
- Milvus, Weaviate, Pinecone, MongoDB Atlas;
- Haystack, CrewAI, Semantic Kernel, AutoGen;
- Dapr state store;
- OpenInference/Phoenix, Ragas, Promptfoo;
- multimodal context blocks;
- streaming model responses;
- background memory consolidation;
- memory checkpoints/rollback и quarantine для untrusted signals;
- portable benchmark suite для long-dialog и coding-agent memory.

Элемент переходит из backlog в milestone только по критериям из
[карты интеграций](INTEGRATIONS.md#15-пересмотр-приоритетов).

## Сквозные критерии готовности

Для каждого релиза:

- [x] core остаётся без обязательных third-party dependencies;
- [x] Python 3.11–3.13 проходит CI;
- [x] deterministic tests не требуют сети или cloud credentials;
- [x] live tests opt-in и имеют таймауты;
- [x] wheel/sdist, extras metadata и lazy imports проверены;
- [x] RU/EN docs собираются в strict mode;
- [x] migration и rollback описаны;
- [x] privacy/redaction defaults проверены тестами;
- [x] changelog и runnable example обновлены;
- [x] интеграция имеет maintainer/deprecation path.

Проверка release candidate `0.6.0` от 2026-08-28:

- exact CI extras (`chroma,qdrant,dev`) и deterministic suite: `394 passed` на
  Python 3.11, 3.12 и 3.13; внешние sockets запрещены через `pytest-socket`;
- coverage на Python 3.12: `87.8%`; agent CLI: `188 passed`;
- live PostgreSQL/pgvector, Redis, Elasticsearch 9, OpenSearch 3 и локальный
  Chroma: `10 passed`; AWS/GCP live contracts остаются opt-in и требуют
  тестовых cloud credentials;
- wheel/sdist прошли `twine check`, isolated zero-dependency import и проверку
  28 extras/66 optional requirements; sdist содержит deployment/docs assets;
- RU/EN MkDocs прошли strict build; восемь offline examples и FastAPI HTTP
  lifecycle выполнены; Kubernetes manifest принят API server minikube в
  `--dry-run=server`.

## Риски roadmap

| Риск | Контроль |
|---|---|
| framework APIs быстро меняются | узкие adapters, version matrix, contract tests |
| optional dependencies конфликтуют | изолированные extras, нет `all` extra |
| core превращается в orchestrator | жёсткие non-goals в `INTEGRATIONS.md` |
| слишком широкий `0.4` | foundation/connectivity/production релизы |
| provider wrappers не дают ценности | adapter только при уникальной семантике |
| telemetry раскрывает данные | deny-by-default export и redaction tests |
| scope ломает multi-tenancy | host-controlled namespace + isolation tests |
| roadmap снова устаревает | пересмотр после каждого minor release |

## Ближайший implementation slice

Первый PR после утверждения roadmap:

1. split `ChatClientProtocol` / `EmbeddingClientProtocol`;
2. migration существующих clients без удаления `LLMClientProtocol`;
3. adapter contract test kit;
4. минимальный MCP vertical slice: `memory_search` + `last-report` resource.

Это проверит новый архитектурный шов до массового добавления integrations.
