# protoprompt

[![CI](https://github.com/Idxeed/protoprompt/actions/workflows/ci.yml/badge.svg)](https://github.com/Idxeed/protoprompt/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/codecov/c/github/Idxeed/protoprompt)](https://codecov.io/gh/Idxeed/protoprompt)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![RU](https://img.shields.io/badge/%D0%AF%D0%B7%D1%8B%D0%BA-RU-blue)](README.ru.md)
[![EN](https://img.shields.io/badge/Language-EN-blue)](README.en.md)

[Русская версия](README.ru.md)

Layered context builder for LLM prompts. Three independent, composable
layers feed the model: **RAG over documents**, **compressed session
memory**, and an optional **user profile**. Pluggable vector store,
pluggable tokenizer, pluggable compression strategy.

## Why

Production LLM apps hit the same wall: the model needs context, but the
context window is finite. Hand-rolled prompt assembly gets messy:
documents are queried separately from session history, the system prompt
gets duplicated, and you end up rewriting the same glue code per project.

`protoprompt` separates the three concerns and gives each a clean
protocol:

- `StoreProtocol` — vector storage (in-memory for tests, ChromaDB for prod).
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

# Dev / docs
pip install "protoprompt[chroma,dev]"
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
| `protoprompt.store`      | `StoreProtocol`, `InMemStore`, `ChromaStore`                      |
| `protoprompt.session`    | `Session`, `CompressedBlock`, `HeuristicStrategy`, `LLMSummaryStrategy` |
| `protoprompt.profile`    | `UserProfile`, `ProfileBuilder`                                   |
| `protoprompt.tokens`     | `TokenCounter`, `RegexTokenCounter`, `TiktokenCounter`            |
| `protoprompt.llm`        | `LLMClientProtocol`                                               |

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
pip install -e ".[chroma,dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
