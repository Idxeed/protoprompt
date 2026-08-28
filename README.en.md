# protoprompt

[![CI](https://github.com/Idxeed/protoprompt/actions/workflows/ci.yml/badge.svg)](https://github.com/Idxeed/protoprompt/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/codecov/c/github/Idxeed/protoprompt)](https://codecov.io/gh/Idxeed/protoprompt)
[![Python 3.11–3.13](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![RU](https://img.shields.io/badge/%D0%AF%D0%B7%D1%8B%D0%BA-RU-blue)](README.md)
[![EN](https://img.shields.io/badge/Language-EN-blue)](README.en.md)

[Русская версия](README.md)

Composable context and long-term memory for LLM applications. The core combines
**RAG over documents**, **compressed session memory**, an optional **user
profile**, token budgeting, and recall provenance. Integration adapters connect
the same memory contract to bots, agents, APIs, model providers, and production
storage without coupling those dependencies to the core package.

![Telegram bot recalls an old fact and shows provenance](docs/assets/telegram-memory.gif)

The reference [Telegram bot](docs/en/telegram.md) persists long-term memory in
SQLite, works with OpenAI or Ollama, and explains each recall through `/why`.

## Why

Production LLM apps hit the same wall: the model needs context, but the
context window is finite. Hand-rolled prompt assembly gets messy:
documents are queried separately from session history, the system prompt
gets duplicated, and you end up rewriting the same glue code per project.

`protoprompt` separates the three concerns and gives each a clean
protocol:

- `StoreProtocol` — pluggable local and production vector storage.
- `StrategyProtocol` — how to compress old turns of a long session.
- `TokenCounter` — how to budget the final prompt.

The `ContextBuilder` orchestrates all three. The result is a single
`system_prompt` plus a structured `ContextOutput` describing what went in
(head, tail, RAG, profile) so the UI can show provenance.

## Install

```bash
pip install protoprompt

# With ChromaDB backend
pip install "protoprompt[chroma]"

# With tiktoken-based tokenizer
pip install "protoprompt[tiktoken]"

# Integrations: OpenAI SDK / Ollama / any OpenAI-compatible REST
pip install "protoprompt[openai]"
pip install "protoprompt[ollama]"
pip install "protoprompt[http]"
pip install "protoprompt[anthropic]"
pip install "protoprompt[google]"
pip install "protoprompt[bedrock]"
pip install "protoprompt[pydanticai]"
pip install "protoprompt[llamaindex]"
pip install "protoprompt[mcp,agents,langgraph]"
pip install "protoprompt[postgres,redis,otel]"
pip install "protoprompt[elasticsearch]"  # or opensearch
pip install "protoprompt[documents,fastapi]"
pip install "protoprompt[aws-secrets]"    # or gcp-secrets

# Vector DBs and API-free local embeddings
pip install "protoprompt[qdrant]"
pip install "protoprompt[local]"
pip install "protoprompt[fastembed]"

# Dev / docs
pip install "protoprompt[chroma,qdrant,dev]"
```

## Quickstart

```python
import asyncio
from protoprompt import (
    InMemStore,
    ContextBuilder,
    ContextInput,
    Pipeline,
    HeuristicStrategy,
    Session,
)


class MyLLM:
    async def chat(self, messages, model="", **options):
        return "stub"

    async def embed(self, texts, model=""):
        # replace with real embeddings
        return [[0.1] * 384 for _ in texts]


async def main():
    store = InMemStore()
    store.add("doc-1", ["Paris is the capital of France."], [[0.5] * 384])
    llm = MyLLM()

    builder = ContextBuilder(store, llm)
    out = await builder.build(ContextInput(
        query="What is the capital of France?",
        system_prompt="You are a geography tutor.",
        doc_ids=[1],
    ))
    print(out.system_prompt)

    pipeline = Pipeline(
        store, llm,
        strategy=HeuristicStrategy(),
        compress_every_n=10,
    )
    session = Session(chat_id="c1", messages=[
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
    ])
    if pipeline.should_compress(len(session.messages)):
        await pipeline.compress_and_store(session)


asyncio.run(main())
```

## Architecture

```
                 +--------------------+
                 |   ContextBuilder   |
                 |   (orchestrator)   |
                 +---------+----------+
                           |
        +------------------+------------------+------------------+
        |                  |                  |                  |
        v                  v                  v                  v
+---------------+  +----------------+  +---------------+  +---------+
| RAG retrieval |  | Session memory |  | User profile  |  |  Store  |
| (vector top-k)|  | (compressed)   |  | (LLM-derived) |  |  query  |
+---------------+  +----------------+  +---------------+  +---------+
        |                  |                  |
        +--------+---------+--------+---------+
                 |                  |
                 v                  v
        +----------------+  +----------------+
        |  TokenCounter  |  |  StoreProtocol |
        |  (pluggable)   |  |  (in-mem/chroma)|
        +----------------+  +----------------+
```

## Public API

| Module                   | Exports                                                           |
|--------------------------|-------------------------------------------------------------------|
| `protoprompt`            | `Pipeline`, `ContextBuilder`, `ContextInput`, `ContextOutput`     |
| `protoprompt.store`      | `StoreProtocol`, `AsyncStoreProtocol`, `InMemStore`, `SqliteStore`, `as_async` |
| `protoprompt.session`    | `Session`, `CompressedBlock`, `HeuristicStrategy`, `LLMSummaryStrategy` |
| `protoprompt.profile`    | `UserProfile`, `ProfileBuilder`                                   |
| `protoprompt.tokens`     | `TokenCounter`, `RegexTokenCounter`, `TiktokenCounter`            |
| `protoprompt.llm`        | `LLMClientProtocol`                                               |
| `protoprompt.cache`      | `CachedLLMClient`, `InMemoryEmbeddingCache`                       |
| `protoprompt.hooks`      | `ContextHooks`, `PipelineHooks`                                   |
| `protoprompt.integrations` | `OpenAIClient`, `OllamaClient`, `HttpxLLMClient`, `QdrantStore`, `SentenceTransformersClient`, `FastEmbedClient` |
| `protoprompt.connectivity` | scope-pinned `MemoryService` shared by runtime adapters |
| `protoprompt.readers`      | bounded local readers and framework document converters |

Runnable recipes live in [examples/](examples/): an offline
session-compression demo, Ollama RAG, budgeted OpenAI chat, and local
embeddings.

## Documentation

Full docs are built in two languages:

- 🇷🇺 Russian: <https://idxeed.github.io/protoprompt/ru/>
- 🇬🇧 English: <https://idxeed.github.io/protoprompt/en/>

Build locally:

```bash
pip install "protoprompt[dev]"
python scripts/build_docs.py --serve  # both versions on different ports
```

## Development

```bash
git clone https://github.com/Idxeed/protoprompt
cd protoprompt
python -m venv .venv && source .venv/bin/activate
pip install -e ".[chroma,qdrant,dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
