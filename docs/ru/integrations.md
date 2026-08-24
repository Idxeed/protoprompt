# Интеграции

Ядро `protoprompt` остаётся без обязательных зависимостей. Всё внешнее
подключается через опциональные экстра — импорт внутри конструкторов,
поэтому сам пакет `protoprompt.integrations` ставится мгновенно.

## LLM-клиенты

Все клиенты реализуют протокол `LLMClientProtocol` (`chat` + `embed`),
то есть подойдут и в `ContextBuilder`, и в `Pipeline`.

| Класс | Экстра | Адресаты |
|---|---|---|
| `integrations.OpenAIClient` | `[openai]` | OpenAI, LiteLLM, vLLM (через `base_url`) |
| `integrations.OllamaClient` | `[ollama]` | локальный/удалённый Ollama (`/api/chat`, `/api/embed`) |
| `integrations.HttpxLLMClient` | `[http]` | любой OpenAI-совместимый REST (LM Studio, llama.cpp) |

```python
from protoprompt import ContextBuilder, ContextInput, InMemStore
from protoprompt.integrations import OllamaClient

llm = OllamaClient(host="http://localhost:11434")
builder = ContextBuilder(InMemStore(), llm)
```

`HttpxLLMClient` принимает `transport=` (например `httpx.MockTransport`)
— удобно для тестов без сети.

## Эмбеддинги без API

Классы дают только `embed()`; `chat()` бросает понятный
`NotImplementedError`.

| Класс | Экстра | Комментарий |
|---|---|---|
| `SentenceTransformersClient` | `[local]` | HF-модели на CPU/GPU |
| `FastEmbedClient` | `[fastembed]` | ONNX, лёгкая установка |

Оба кодируют батчи в рабочем потоке (`asyncio.to_thread`), не блокируя
событийный цикл.

## Хранилища

| Класс | Откуда | Особенности |
|---|---|---|
| `SqliteStore` (ядро) | без зависимостей | персистентность, replace-on-add |
| `QdrantStore` | `[qdrant]` | сервер (`url=`), локальный режим (`path=`), in-memory |
| `ChromaStore` | `[chroma]` | как раньше |

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

```python
from protoprompt import ContextHooks, PipelineHooks, TokenBudgetedContextBuilder

hooks = ContextHooks(
    on_section_used=lambda label, tokens: print(f"+{tokens} {label}"),
    on_block_dropped=lambda label, reason: print(f"-{label} ({reason})"),
    on_build_done=lambda report: print(f"итог: {report.used_tokens}"),
)
builder = TokenBudgetedContextBuilder(store, llm, hooks=hooks)
```

Исключения в хуках логируются и глотаются — наблюдаемость не может
сломать основной поток.
