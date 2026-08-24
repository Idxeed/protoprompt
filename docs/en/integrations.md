# Integrations

The `protoprompt` core keeps zero required dependencies. Everything
external arrives via optional extras; imports happen inside
constructors, so `protoprompt.integrations` itself is instant to import.

## LLM clients

Every client implements `LLMClientProtocol` (`chat` + `embed`), so all
of them work with both `ContextBuilder` and `Pipeline`.

| Class | Extra | Targets |
|---|---|---|
| `integrations.OpenAIClient` | `[openai]` | OpenAI, LiteLLM, vLLM (via `base_url`) |
| `integrations.OllamaClient` | `[ollama]` | local/remote Ollama (`/api/chat`, `/api/embed`) |
| `integrations.HttpxLLMClient` | `[http]` | any OpenAI-compatible REST (LM Studio, llama.cpp) |

```python
from protoprompt import ContextBuilder, ContextInput, InMemStore
from protoprompt.integrations import OllamaClient

llm = OllamaClient(host="http://localhost:11434")
builder = ContextBuilder(InMemStore(), llm)
```

`HttpxLLMClient` accepts a `transport=` (e.g. `httpx.MockTransport`) —
handy for network-free tests.

## API-free embeddings

These classes provide `embed()` only; `chat()` raises a descriptive
`NotImplementedError`.

| Class | Extra | Notes |
|---|---|---|
| `SentenceTransformersClient` | `[local]` | HF models on CPU/GPU |
| `FastEmbedClient` | `[fastembed]` | ONNX runtime, light install |

Both encode batches in a worker thread (`asyncio.to_thread`) and never
block the event loop.

## Vector stores

| Class | Source | Notes |
|---|---|---|
| `SqliteStore` (core) | dependency-free | persistent, replace-on-add |
| `QdrantStore` | `[qdrant]` | server (`url=`), embedded local (`path=`), in-memory |
| `ChromaStore` | `[chroma]` | as before |

## Async stores

Any store can run asynchronously:

```python
from protoprompt import as_async, AsyncInMemStore

# ready-made async twin of InMemStore
store = AsyncInMemStore()

# or wrap a sync backend: every call is dispatched to a thread
store = as_async(ChromaStore(persist_dir="./chroma"))
```

Builders and the `Pipeline` accept sync and async stores alike.

## Embedding cache

```python
from protoprompt import CachedLLMClient, InMemoryEmbeddingCache

cached = CachedLLMClient(OllamaClient(), InMemoryEmbeddingCache(capacity=4096))
# repeated build() calls with the same query skip the model entirely
```

## Observability hooks

```python
from protoprompt import ContextHooks, PipelineHooks, TokenBudgetedContextBuilder

hooks = ContextHooks(
    on_section_used=lambda label, tokens: print(f"+{tokens} {label}"),
    on_block_dropped=lambda label, reason: print(f"-{label} ({reason})"),
    on_build_done=lambda report: print(f"total: {report.used_tokens}"),
)
builder = TokenBudgetedContextBuilder(store, llm, hooks=hooks)
```

Hook exceptions are logged and swallowed — observability can never
break the main flow.
