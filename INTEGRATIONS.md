# Карта интеграций protoprompt

> Статус: рабочий архитектурный документ. Последняя проверка внешних API:
> 2026-08-28. План реализации находится в [ROADMAP.md](ROADMAP.md).

## 1. Цель

`protoprompt` должен быть переносимым движком контекста и памяти, который можно
подключить к существующему приложению без замены его agent runtime, модели,
базы данных или observability-стека.

Интеграция считается полезной, если она открывает новый прикладной сценарий,
подключает проект к существующей аудитории или стандарту, даёт production
backend, делает решения памяти наблюдаемыми либо существенно уменьшает glue-код.
Количество логотипов само по себе целью не является.

## 2. Что уже поддерживается

| Область | Реализации | Состояние |
|---|---|---|
| LLM | OpenAI, Ollama, OpenAI-compatible HTTP | готово в `0.3` |
| Embeddings | OpenAI, Ollama, sentence-transformers, FastEmbed | готово в `0.3` |
| Vector store | in-memory, SQLite, ChromaDB, Qdrant | готово в `0.3` |
| Profile store | in-memory, SQLite | готово в `0.3` |
| Secret store | encrypted SQLite + OS keyring/env/file keys | готово в `0.3` |
| Token counting | regex approximation, tiktoken | готово в `0.3` |
| Observability | `ContextHooks` / `PipelineHooks` | готово в `0.3` |
| Agent shell | экспериментальный `pp-agent` | готово в `0.3` |
| Connectivity | MCP, OpenAI Agents, LangGraph, Telegram | реализовано в roadmap branch |
| Production | pgvector/PostgreSQL, Redis, OpenTelemetry | реализовано в roadmap branch |
| Providers/frameworks | Anthropic, Google GenAI, Bedrock, PydanticAI, LlamaIndex | реализовано в roadmap branch |
| Data/enterprise | local readers, Elasticsearch/OpenSearch, AWS/GCP secrets, FastAPI | реализовано в roadmap branch |

Все внешние зависимости остаются опциональными и лениво импортируются.

## 3. Приоритеты

| Метка | Значение |
|---|---|
| **P0** | архитектурный фундамент или интеграция следующего релиза |
| **P1** | высокий пользовательский охват или production-ценность |
| **P2** | полезное расширение после стабилизации P0/P1 |
| **P3** | community adapter или реализация после подтверждённого спроса |
| **Example** | сначала эталонное приложение; adapter — если найден повторяемый шов |

Приоритет задаёт порядок принятия решений, но не обещает срок.

## 4. Архитектурный фундамент

### 4.1 Разделить chat и embeddings — P0

Текущий `LLMClientProtocol` требует одновременно `chat()` и `embed()`. Это
вынуждает embedding-only клиенты бросать `NotImplementedError` и мешает честной
интеграции с провайдерами без embedding API.

Целевые контракты:

```python
class ChatClientProtocol(Protocol):
    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        **options: object,
    ) -> str: ...


class EmbeddingClientProtocol(Protocol):
    async def embed(
        self,
        texts: list[str],
        model: str = "",
    ) -> list[list[float]]: ...


class LLMClientProtocol(ChatClientProtocol, EmbeddingClientProtocol, Protocol):
    """Обратная совместимость для клиентов с обеими возможностями."""
```

`ContextBuilder`, `Retriever`, `DocumentIndexer` и `WorkingMemory` принимают
`EmbeddingClientProtocol`; стратегии сжатия, reranker и profile source —
`ChatClientProtocol`; `Pipeline` получает оба явно или composite-клиент.

### 4.2 Нормализовать scope/namespace — P0

Framework stores используют namespace, а текущий `StoreProtocol` — `doc_id` и
metadata-фильтры. Нужен небольшой тип `MemoryScope` (`tenant`, `user`, `thread`,
`kind`) и единое отображение scope в metadata без изменения физической схемы
существующих хранилищ.

Обязательные свойства:

- tenant/user/thread изоляция;
- scope задаёт host, а не модель;
- одинаковая семантика в SQLite, Postgres, Redis, LangGraph и MCP;
- удаление сессии не затрагивает профиль или документы пользователя.

### 4.3 Контрактные тесты адаптеров — P0

Добавить переиспользуемые test suites:

- `ChatClientContract` и `EmbeddingClientContract`;
- `StoreContract` / `AsyncStoreContract`;
- `ProfileStoreContract` и `SecretStoreContract`;
- проверки lazy imports и сообщений об отсутствующих extras.

Без прохождения контракта adapter не входит в официальный пакет.

### 4.4 Типизированные события observability — P1

Существующие hooks сохранить. Под ними ввести события с `trace_id`, `scope`,
длительностью, token usage и причиной решения. Содержимое промптов, документов,
профиля и секретов по умолчанию не экспортируется.

## 5. Протоколы и agent runtimes

| Цель | Форма интеграции | Приоритет | Зачем |
|---|---|---:|---|
| **Model Context Protocol (MCP)** | server с tools/resources | **P0** | стандартный вход для MCP-хостов |
| **OpenAI Agents SDK** | `Session` + input callback | **P0** | расширенная память вместо простой истории |
| **LangGraph** | store adapter + `build_context` node | **P0** | thread и long-term memory |
| **PydanticAI** | history processor / dependency adapter | P1 | типизированные Python-агенты |
| **LlamaIndex** | Memory/ChatStore/VectorStore bridges | P1 | data/RAG и LlamaHub readers |
| **Google ADK** | session/memory service adapter | P1 | Gemini/Vertex AI agents |
| **CrewAI** | memory/knowledge adapter | P2 | multi-agent crews и flows |
| **Haystack** | DocumentStore/Retriever bridge | P2 | retrieval pipelines |
| **Microsoft Agent Framework / Semantic Kernel** | memory provider | P2 | enterprise Microsoft stack |
| **AutoGen / AG2** | memory/context provider | P3 | после запроса пользователей |

### 5.1 MCP — целевой публичный шов

Предлагаемый extra: `protoprompt[mcp]`.

Tools:

- `memory_remember` — добавить подтверждённое воспоминание;
- `memory_search` — вернуть recall с provenance;
- `memory_forget` — удалить запись в разрешённом scope;
- `memory_profile_update` — записать сигнал профиля;
- `memory_explain` — объяснить inclusion/eviction;
- `memory_budget_report` — показать token budget.

Resources:

- `memory://current/profile`;
- `memory://current/manifest`;
- `memory://current/last-report`.

Scope и разрешения предоставляет host. Модель не может выбрать произвольный
tenant или получить raw secret. Поддерживаются stdio и Streamable HTTP;
transport не проникает в core.

Официальный SDK: <https://py.sdk.modelcontextprotocol.io/>.

### 5.2 OpenAI Agents SDK

Предлагаемый extra: `protoprompt[agents]`.

- `ProtoPromptSession` реализует публичный `Session` protocol;
- оригинальная история доступна для `pop_item`/correction;
- context assembly выполняется через session input callback;
- server-managed conversation state не смешивается с локальной сессией;
- пример сравнивает plain SQLite history и budgeted recall.

Официальный контракт: <https://openai.github.io/openai-agents-python/sessions/>.

### 5.3 LangGraph

Предлагаемый extra: `protoprompt[langgraph]`.

- `ProtoPromptStoreAdapter` отображает namespace/key/search;
- `build_context` node получает `thread_id`, `user_id`, query и возвращает
  messages + provenance;
- checkpointer остаётся ответственностью LangGraph;
- adapter не создаёт скрытые глобальные stores;
- sync и async графы проходят отдельные contract tests.

LangGraph разделяет thread-level и long-term memory:
<https://langchain-ai.github.io/langgraph/how-tos/memory/manage-conversation-history/>.

## 6. Прикладные каналы

| Канал | Первая форма | Приоритет | Решение |
|---|---|---:|---|
| **Telegram / aiogram** | runnable bot + middleware | **P0 Example** | главный понятный memory use case |
| **FastAPI / Starlette** | dependency + lifespan service | готово | backend для UI и webhook-ботов |
| **Slack Bolt** | runnable app | gate: спрос не подтверждён | память пользователя и треда |
| **Discord.py** | runnable bot | gate: спрос не подтверждён | community-боты |
| **Jupyter / IPython** | notebook helper | P2 | исследование памяти и provenance |
| WhatsApp/Matrix | recipe | P3 | только при внешнем запросе |

### 6.1 Эталонный Telegram-бот

Демонстрация должна отвечать на вопрос: «могу ли я сделать боту практически
неограниченную память?»

- aiogram 3;
- SQLite по умолчанию, Postgres опционально;
- OpenAI/Ollama через существующие clients;
- `/memory` показывает hot/cold объём без приватного содержимого;
- `/why` показывает provenance последнего recall;
- `/forget` удаляет память текущего пользователя;
- synthetic conversation проверяет возврат старого факта;
- baseline с FIFO/LRU показывает различие без обещания «бесконечности».

Aiogram предоставляет async router, middleware и dependency injection:
<https://docs.aiogram.dev/en/latest/>.

## 7. Модели, embeddings и токены

### 7.1 Chat providers

| Провайдер | Путь | Приоритет | Комментарий |
|---|---|---:|---|
| OpenAI | native SDK | готово | сохранить |
| Ollama | native HTTP | готово | сохранить |
| OpenAI-compatible | generic HTTP | готово | vLLM, LM Studio, gateways |
| **Anthropic Claude** | `AsyncAnthropic` | P1 | chat-only после split протоколов |
| **Google Gemini** | `google-genai` | P1 | chat + embeddings |
| **Amazon Bedrock** | Converse API | P1 | единый enterprise provider |
| Mistral | native SDK | P2 | только при отличиях от compatible API |
| Cohere | native SDK | P2 | chat, embed, rerank |
| Hugging Face Inference | provider adapter | P2 | hosted open models |
| Azure AI Foundry | compatible/native | P2 | enterprise auth и endpoints |

Anthropic имеет sync/async Messages SDK, но не общий embedding API:
<https://platform.claude.com/docs/en/api/sdks/python>.

Для Gemini использовать `google-genai`, а не legacy `google-generativeai`:
<https://ai.google.dev/gemini-api/docs/libraries>.

Bedrock Converse предоставляет единый chat API:
<https://docs.aws.amazon.com/bedrock/latest/userguide/apis.html>.

### 7.2 Embedding, reranking и counters

| Цель | Приоритет | Форма |
|---|---:|---|
| Gemini embeddings | P1 | `EmbeddingClientProtocol` |
| Cohere Embed + Rerank | P1 | embedding + `RerankerProtocol` |
| Voyage AI | P2 | embedding + rerank |
| Hugging Face / TEI | P2 | compatible или native HTTP |
| Anthropic token counting | P1 | remote exact counter, только opt-in |
| Hugging Face tokenizer | P1 | локальные модели |
| Gemini token counter | P2 | provider call или оценка |

Provider-specific wrapper не добавляется, если generic HTTP сохраняет нужную
семантику и аутентификацию. Remote token counter не становится default.

## 8. Хранилища

### 8.1 Vector stores

| Backend | Приоритет | Причина |
|---|---:|---|
| **PostgreSQL + pgvector** | **P0** | production backend общего назначения |
| **Redis Vector Search** | P1 | low-latency memory, TTL, metadata filters |
| Elasticsearch / OpenSearch | готово | dense-vector + metadata filters; hybrid остаётся research |
| Qdrant | готово | сохранить и расширить contract tests |
| ChromaDB | готово | dev/local |
| Milvus | P2 | embedded Lite и distributed deployment |
| Weaviate | P2 | sync/async, managed/local modes |
| Pinecone | P2 | managed vector search |
| MongoDB Atlas Vector Search | P2 | приложения на MongoDB |
| SQLite vector extensions | P3 | после portable extension API |

Источники: [pgvector](https://github.com/pgvector/pgvector),
[Redis Vector Search](https://redis.io/docs/latest/develop/ai/search-and-query/vectors/),
[Milvus](https://milvus.io/docs/quickstart.md),
[Weaviate](https://docs.weaviate.io/weaviate/client-libraries/python).

### 8.2 Profile/session/cache stores

| Backend | Контракт | Приоритет |
|---|---|---:|
| PostgreSQL | profile + session + optimistic locking | **P0** |
| Redis | cache + ephemeral session + TTL | P1 |
| SQLAlchemy | generic profile/session adapter | P1 research |
| MongoDB | document profile/session | P2 |
| Dapr state store | deployment bridge | P3 |

Выбор между прямым `psycopg` и SQLAlchemy принимается после spike. Vector store
не принуждает пользователя хранить profile/session в той же БД.

### 8.3 PostgreSQL Memory Ledger

`PostgresMemoryLedger` — отдельный синхронный experimental backend для
host-owned lifecycle, admission, strict recall и recall checkpoints. Это не
новый vector store и не замена async `PgVectorStore`/`PostgresProfileStore`.
Он ставится через `protoprompt[postgres]`, создаётся только явным `setup()` и
владеет одной выделенной PostgreSQL schema.

- Поддерживается только свежая schema Ledger v6: перенос SQLite, миграция
  старой PostgreSQL schema и destructive downgrade не предусмотрены.
- Для сохранения точной финальной валидации при PostgreSQL MVCC записи в одной
  Ledger schema сериализуются transaction-scoped advisory lock-ом. Это
  осознанный выбор корректности перед throughput; host повторяет idempotent
  команду с тем же `event_id` при `LedgerConflictError`.
- `backup()` намеренно не копирует PostgreSQL в файл. Ownership backup, restore,
  retention, WAL/replica и проверка восстановления остаются у оператора
  (например, `pg_dump --schema=...` и platform policy).

Подробный production recipe, требования к ролям, выделенной schema и границам
удаления описаны в [docs/ru/postgres.md](docs/ru/postgres.md).

## 9. Observability и evaluation

| Цель | Форма | Приоритет |
|---|---|---:|
| **OpenTelemetry** | spans/events bridge | **P0** |
| Langfuse | OTEL recipe/exporter | P1 |
| Prometheus | counters/histograms exporter | P1 |
| OpenInference / Arize Phoenix | semantic attributes | P2 |
| LangSmith | callback/OTEL recipe | P2 |
| Ragas | dataset/evaluation adapter | P2 |
| Promptfoo | JSONL export + CLI recipe | P2 |

Минимальные spans: `context.build`, `retrieve`, `session.compress`,
`profile.update`, `memory.recall`, `memory.evict`, `cache.lookup`.

Attributes: длительность, число кандидатов, токены, причина eviction/drop,
backend и model. Тексты и секреты экспортируются только через opt-in redaction
policy. Langfuse использует OpenTelemetry как основу SDK:
<https://langfuse.com/docs/observability/sdk/overview>.

## 10. Ingestion и документы

| Источник | Форма | Приоритет |
|---|---|---:|
| plain text / Markdown / source code | встроенные readers | P1 |
| PDF | optional reader | P1 |
| LlamaIndex readers | `Document` converter | P1 |
| локальный HTML | sanitized reader | готово |
| URL | отдельный SSRF-safe downloader | research |
| DOCX | optional reader | готово |
| Git repository | file walker + language metadata | P2 |
| Unstructured | `Document` converter | P2 |
| S3/GCS/Azure Blob | stream adapter | P2 |

Readers получают `Document`; chunking, embeddings и index остаются в
protoprompt. URL-reader требует SSRF-safe policy и не входит в core.

## 11. Secrets и key management

| Backend | Контракт | Приоритет |
|---|---|---:|
| HashiCorp Vault | `SecretStore` / key provider | P1 |
| AWS Secrets Manager | `SecretStore` | готово |
| Azure Key Vault | `SecretStore` | P2 |
| Google Secret Manager | `SecretStore` | готово |
| Kubernetes Secrets | read-only provider recipe | P2 |

Cloud adapters используют workload identity/default credential chain и не
принимают raw credentials от модели. `SecretAccess` остаётся единственным швом
к model-visible tools.

Официальные клиенты: [Vault](https://developer.hashicorp.com/vault/api-docs/libraries),
[AWS](https://docs.aws.amazon.com/boto3/latest/guide/secrets-manager.html),
[Azure](https://learn.microsoft.com/en-us/azure/key-vault/secrets/quick-create-python),
[GCP](https://docs.cloud.google.com/secret-manager/docs/reference/libraries).

## 12. Packaging

Предлагаемые extras:

```text
mcp, agents, langgraph, pydanticai, llamaindex
anthropic, google, bedrock
postgres, redis, elasticsearch, opensearch
otel, prometheus
telegram, fastapi
aws-secrets, gcp-secrets
```

Правила:

- один extra не тянет другой framework без необходимости;
- `all` extra не публикуется;
- все внешние SDK импортируются лениво;
- constructor сообщает точную команду установки;
- диапазоны версий проверяются в CI;
- cloud/live tests отделены от deterministic contract tests.

## 13. Что сознательно не делаем

- Не превращаем protoprompt в ещё один agent orchestrator.
- Не дублируем tool calling, planning и workflow API framework'ов.
- Не пишем native wrapper для каждого OpenAI-compatible endpoint.
- Не экспортируем secrets/raw prompts/documents в telemetry по умолчанию.
- Не обещаем «бесконечную память»: измеряем recall под token pressure.
- Не принимаем adapter без владельца, contract tests и deprecation path.
- Не добавляем cloud integration только ради README badge.

## 14. Definition of done интеграции

- [ ] adapter имеет узкий документированный контракт;
- [ ] отсутствие extra не ломает импорт `protoprompt`;
- [ ] deterministic unit/contract tests не требуют сети;
- [ ] live test существует отдельно и только при необходимости;
- [ ] sync/async поведение определено;
- [ ] tenant/user/thread isolation проверена;
- [ ] dependency/auth/network errors понятны пользователю;
- [ ] secrets и memory content не логируются по умолчанию;
- [ ] есть runnable example и migration/removal notes;
- [ ] RU/EN docs и changelog обновлены;
- [ ] wheel smoke test проверяет lazy imports и extras metadata.

## 15. Пересмотр приоритетов

Интеграция поднимается, если два независимых пользователя её запросили, есть
готовый сопровождать adapter contributor, она нужна benchmark/example либо
закрывает production-blocker. Стандартный protocol имеет преимущество перед
framework-specific решением.

Интеграция понижается или удаляется, если upstream нестабилен, adapter требует
частых breaking fixes, generic protocol покрывает тот же сценарий или нет
пользователя, готового проверить реальное использование.
