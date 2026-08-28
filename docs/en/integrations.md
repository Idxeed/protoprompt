# Integrations

The `protoprompt` core keeps zero required dependencies. Everything
external arrives via optional extras; imports happen inside
constructors, so `protoprompt.integrations` itself is instant to import.

## LLM clients

Full clients implement the backwards-compatible `LLMClientProtocol`
(`chat` + `embed`). Narrow integrations may implement only
`ChatClientProtocol` or `EmbeddingClientProtocol`; `CompositeLLMClient`
pairs independent capabilities.

| Class | Extra | Targets |
|---|---|---|
| `integrations.OpenAIClient` | `[openai]` | OpenAI, LiteLLM, vLLM (via `base_url`) |
| `integrations.OllamaClient` | `[ollama]` | local/remote Ollama (`/api/chat`, `/api/embed`) |
| `integrations.HttpxLLMClient` | `[http]` | any OpenAI-compatible REST (LM Studio, llama.cpp) |
| `integrations.AnthropicClient` | `[anthropic]` | native Anthropic Messages API (chat) |
| `integrations.GoogleGenAIClient` | `[google]` | Gemini Developer API / Vertex AI (chat + embed) |
| `integrations.BedrockConverseClient` | `[bedrock]` | Amazon Bedrock Converse (chat) |

See the [provider and framework matrix](providers-frameworks.md) for native
token counting, auth semantics, PydanticAI/LlamaIndex bridges, and the Google
ADK support decision.

```python
from protoprompt import ContextBuilder, ContextInput, InMemStore
from protoprompt.integrations import OllamaClient

llm = OllamaClient(host="http://localhost:11434")
builder = ContextBuilder(InMemStore(), llm)
```

`HttpxLLMClient` accepts a `transport=` (e.g. `httpx.MockTransport`) —
handy for network-free tests.

## API-free embeddings

These classes implement only `EmbeddingClientProtocol` and do not pretend
to be chat models.

| Class | Extra | Notes |
|---|---|---|
| `SentenceTransformersClient` | `[local]` | HF models on CPU/GPU |
| `FastEmbedClient` | `[fastembed]` | ONNX runtime, light install |

Both encode batches in a worker thread (`asyncio.to_thread`) and never
block the event loop.

```python
from protoprompt import CompositeLLMClient
from protoprompt.integrations import FastEmbedClient, OpenAIClient

llm = CompositeLLMClient(
    chat_client=OpenAIClient(),
    embedding_client=FastEmbedClient(),
)
```

## Vector stores

| Class | Source | Notes |
|---|---|---|
| `SqliteStore` (core) | dependency-free | persistent, replace-on-add |
| `QdrantStore` | `[qdrant]` | server (`url=`), embedded local (`path=`), in-memory |
| `ChromaStore` | `[chroma]` | as before |
| `PgVectorStore` | `[postgres]` | async pgvector, explicit schema setup |
| `ElasticsearchStore` | `[elasticsearch]` | Elasticsearch 9 dense vectors |
| `OpenSearchStore` | `[opensearch]` | OpenSearch Lucene HNSW |

Redis supplies embedding cache, session, and profile adapters rather than vector
retrieval. See [PostgreSQL](postgres.md), [Redis](redis.md), and
[Elasticsearch/OpenSearch](search.md).

Managed credential stores are `AWSSecretsManagerStore` (`[aws-secrets]`) and
`GCPSecretManagerStore` (`[gcp-secrets]`); see [Secrets](secrets.md). Local
document ingestion and framework converters are covered by [Readers](readers.md),
and the authenticated service recipe by [FastAPI](fastapi.md).

### Tenant/user/thread isolation

The host application pins a `MemoryScope` to `DocumentIndexer`, `Retriever`,
`ContextBuilder`, and `Pipeline`. The model receives no parameter with which it
could switch tenants. Scope maps to the same metadata keys in every store, and
the internal `doc_id` gets a deterministic namespace, so identical logical IDs
from different users never overwrite each other.

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

An empty `MemoryScope()` and an omitted `scope` preserve the 0.3 storage
layout, allowing one-tenant-at-a-time migration without rewriting all data.
`kind` is useful for single-purpose adapters; a builder that reads both RAG
and session memory will normally leave it empty (`kind=""`).

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

New integrations should use typed events: `ContextEvent`, `RetrieveEvent`,
`CompressEvent`, `ProfileEvent`, `RecallEvent`, `EvictEvent`, and `CacheEvent`.
Each carries a `trace_id`, opaque `scope_id`, duration, and safe metrics.
`EventDispatcher` recursively redacts `prompt`, `content`, `document`,
`profile`, `secret`, `token`, and other content-bearing fields by default.

```python
from protoprompt import ContextBuilder, EventDispatcher

events = EventDispatcher(lambda event: telemetry.emit(event.to_dict()))
builder = ContextBuilder(store, embeddings, scope=scope, event_sink=events)
```

Existing `ContextHooks` and `PipelineHooks` remain supported: the typed event
is sent first, followed by the compatibility hook. Observer failures are
logged and never interrupt the main operation.

```python
from protoprompt import ContextHooks, PipelineHooks, TokenBudgetedContextBuilder

hooks = ContextHooks(
    on_section_used=lambda label, tokens: print(f"+{tokens} {label}"),
    on_block_dropped=lambda label, reason: print(f"-{label} ({reason})"),
    on_build_done=lambda report: print(f"total: {report.used_tokens}"),
)
builder = TokenBudgetedContextBuilder(store, llm, hooks=hooks)
```

Legacy hooks may receive the original `Session` or `BudgetReport`, so treat
them as a trusted in-process API. Use typed events for external export.

## Contract kit for adapter authors

`protoprompt.testing` provides executable contracts for chat clients,
embeddings, and vector/profile/secret stores. It has no pytest dependency and
checks sync and async implementations through the same API:

```python
from protoprompt.testing import check_chat_client, check_vector_store

await check_chat_client(client)
report = await check_vector_store(store)
print(report.checks)
```

Official adapters run these exact contracts in CI. No live credentials are
required: HTTP clients use a local transport and server-backed stores use an
isolated test collection.

## Ownership and deprecation

Adapters shipped in the `protoprompt` distribution are maintained by the
EnergoAI Hub/core maintainer group. Their supported upstream ranges are pinned
in `pyproject.toml`; contract tests and at least one runnable example are the
acceptance boundary. A community adapter without an active owner remains an
external recipe and is not presented as officially supported.

An official adapter is documented as deprecated and listed in the changelog for
at least one minor release before removal. Removal normally waits for the next
major release and must include a replacement or migration path plus an explicit
rollback. An urgent security issue may disable unsafe behaviour earlier, with
the migration and rollback note shipped in the same release.
