# Интеграции

Ядро `protoprompt` остаётся без обязательных зависимостей. Всё внешнее
подключается через опциональные экстра — импорт внутри конструкторов,
поэтому сам пакет `protoprompt.integrations` ставится мгновенно.

## LLM-клиенты

Полные клиенты реализуют совместимый `LLMClientProtocol` (`chat` + `embed`).
Узкие интеграции могут реализовать только `ChatClientProtocol` или
`EmbeddingClientProtocol`; независимые возможности объединяет
`CompositeLLMClient`.

| Класс | Экстра | Адресаты |
|---|---|---|
| `integrations.OpenAIClient` | `[openai]` | OpenAI, LiteLLM, vLLM (через `base_url`) |
| `integrations.OllamaClient` | `[ollama]` | локальный/удалённый Ollama (`/api/chat`, `/api/embed`) |
| `integrations.HttpxLLMClient` | `[http]` | любой OpenAI-совместимый REST (LM Studio, llama.cpp) |
| `integrations.AnthropicClient` | `[anthropic]` | нативный Anthropic Messages API (chat) |
| `integrations.GoogleGenAIClient` | `[google]` | Gemini Developer API / Vertex AI (chat + embed) |
| `integrations.BedrockConverseClient` | `[bedrock]` | Amazon Bedrock Converse (chat) |

Нативный token counting, auth-семантика, PydanticAI/LlamaIndex и решение по
Google ADK собраны в [матрице провайдеров и фреймворков](providers-frameworks.md).

```python
from protoprompt import ContextBuilder, ContextInput, InMemStore
from protoprompt.integrations import OllamaClient

llm = OllamaClient(host="http://localhost:11434")
builder = ContextBuilder(InMemStore(), llm)
```

`HttpxLLMClient` принимает `transport=` (например `httpx.MockTransport`)
— удобно для тестов без сети.

## Эмбеддинги без API

Классы реализуют только `EmbeddingClientProtocol` и не притворяются
chat-моделями.

| Класс | Экстра | Комментарий |
|---|---|---|
| `SentenceTransformersClient` | `[local]` | HF-модели на CPU/GPU |
| `FastEmbedClient` | `[fastembed]` | ONNX, лёгкая установка |

Оба кодируют батчи в рабочем потоке (`asyncio.to_thread`), не блокируя
событийный цикл.

```python
from protoprompt import CompositeLLMClient
from protoprompt.integrations import FastEmbedClient, OpenAIClient

llm = CompositeLLMClient(
    chat_client=OpenAIClient(),
    embedding_client=FastEmbedClient(),
)
```

## Хранилища

| Класс | Откуда | Особенности |
|---|---|---|
| `SqliteStore` (ядро) | без зависимостей | персистентность, replace-on-add |
| `QdrantStore` | `[qdrant]` | сервер (`url=`), локальный режим (`path=`), in-memory |
| `ChromaStore` | `[chroma]` | как раньше |
| `PgVectorStore` | `[postgres]` | async pgvector, явный setup схемы |
| `PostgresMemoryLedger` | `[postgres]` | экспериментальный sync Ledger; fresh-v6 выделенная schema |
| `ElasticsearchStore` | `[elasticsearch]` | dense vectors Elasticsearch 9 |
| `OpenSearchStore` | `[opensearch]` | OpenSearch Lucene HNSW |

Redis даёт embedding cache, session и profile adapters, но не vector retrieval.
`PostgresMemoryLedger` — не vector store и не async adapter: он явно
provision-ит изолированную PostgreSQL schema и сериализует записи Ledger
transaction-scoped advisory lock-ом. См. [PostgreSQL](postgres.md),
[Redis](redis.md) и [Elasticsearch/OpenSearch](search.md).

Управляемые credential stores: `AWSSecretsManagerStore` (`[aws-secrets]`) и
`GCPSecretManagerStore` (`[gcp-secrets]`), подробности в [Secrets](secrets.md).
Локальное ingestion документов и framework converters описаны в
[Readers](readers.md), authenticated service recipe — в [FastAPI](fastapi.md).

### Изоляция tenant/user/thread

`MemoryScope` закрепляется host-приложением на `DocumentIndexer`, `Retriever`,
`ContextBuilder` и `Pipeline`. Модель не получает параметр, которым можно
переключить tenant. Scope одинаково отображается в metadata любого стора, а
внутренний `doc_id` получает детерминированный namespace: одинаковые логические
ID разных пользователей не перезаписывают друг друга.

```python
from protoprompt import ContextBuilder, MemoryScope, Pipeline
from protoprompt.rag import DocumentIndexer

scope = MemoryScope(tenant="acme", user="u-42", thread="support-chat")
indexer = DocumentIndexer(store, embedding_client, scope=scope)
builder = ContextBuilder(store, embedding_client, scope=scope)
pipeline = Pipeline(
    store,
    chat_client=chat_client,
    embedding_client=embedding_client,
    scope=scope,
)
```

Пустой `MemoryScope()` и отсутствие `scope` сохраняют layout `0.3`: это
позволяет мигрировать по одному tenant без массового переноса данных. Поле
`kind` можно использовать для одноцелевых адаптеров; для билдера, который
одновременно читает RAG и session memory, обычно оставляют `kind=""`.

## Async-хранилища

Любой стор может работать асинхронно:

```python
from protoprompt import as_async, AsyncInMemStore

# готовый async-двойник InMemStore
store = AsyncInMemStore()

# или обёртка над синхронным бэкендом: каждый вызов уходит в поток
store = as_async(ChromaStore(persist_dir="./chroma"))
```

Билдеры и `Pipeline` принимают синхронные и асинхронные сторы
одинаково.

## Кэш эмбеддингов

```python
from protoprompt import CachedLLMClient, InMemoryEmbeddingCache

cached = CachedLLMClient(OllamaClient(), InMemoryEmbeddingCache(capacity=4096))
# повторные build() с тем же query больше не ходят в модель
```

## Хуки наблюдаемости

Для новых интеграций используйте typed events: `ContextEvent`,
`RetrieveEvent`, `CompressEvent`, `ProfileEvent`, `RecallEvent`, `EvictEvent` и
`CacheEvent`. Все они несут `trace_id`, непрозрачный `scope_id`, длительность и
безопасные метрики. `EventDispatcher` по умолчанию рекурсивно редактирует поля
`prompt`, `content`, `document`, `profile`, `secret`, `token` и другие
content-bearing значения.

```python
from protoprompt import ContextBuilder, EventDispatcher

events = EventDispatcher(lambda event: telemetry.emit(event.to_dict()))
builder = ContextBuilder(store, embeddings, scope=scope, event_sink=events)
```

Старые `ContextHooks` и `PipelineHooks` сохранены: typed event отправляется
первым, затем вызывается совместимый hook. Ошибка любого observer логируется и
не прерывает основной поток.

```python
from protoprompt import ContextHooks, PipelineHooks, TokenBudgetedContextBuilder

hooks = ContextHooks(
    on_section_used=lambda label, tokens: print(f"+{tokens} {label}"),
    on_block_dropped=lambda label, reason: print(f"-{label} ({reason})"),
    on_build_done=lambda report: print(f"итог: {report.used_tokens}"),
)
builder = TokenBudgetedContextBuilder(store, llm, hooks=hooks)
```

В legacy hooks могут передаваться исходные `Session`/`BudgetReport`, поэтому их
следует считать доверенным in-process API. Для внешнего экспорта используйте
typed events.

## Contract kit для авторов адаптеров

`protoprompt.testing` содержит исполняемые контракты для chat-клиентов,
эмбеддингов, vector/profile/secret stores. Они не зависят от pytest и одинаково
проверяют sync и async реализации:

```python
from protoprompt.testing import check_chat_client, check_vector_store

await check_chat_client(client)
report = await check_vector_store(store)
print(report.checks)
```

Официальные адаптеры проходят эти же контракты в CI. Live credentials не
нужны: HTTP-клиенты тестируются через локальный transport, а server-backed
stores — через изолированную тестовую collection.

## Владение и deprecation

Адаптеры из дистрибутива `protoprompt` сопровождает группа EnergoAI Hub/core
maintainers. Поддерживаемые диапазоны upstream зафиксированы в `pyproject.toml`,
а обязательная граница приёмки — contract tests и минимум один runnable example.
Community adapter без активного владельца остаётся внешним recipe и не
объявляется официально поддерживаемым.

Официальный adapter помечается deprecated в документации и changelog минимум
за один minor-релиз до удаления. Удаление обычно ждёт следующего major и должно
иметь replacement или migration path и явный откат. При срочной уязвимости
небезопасное поведение может быть отключено раньше, но migration и rollback note
выходят в том же релизе.
