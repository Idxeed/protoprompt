# protoprompt 0.1.0

First public release. Layered context builder for LLM prompts: RAG over
documents, compressed session memory, optional user profile, with a
pluggable vector store, pluggable token counter, and a hard token
budget that protects the model's context window.

## What's inside

- **RAG** over user-attached documents with top-k retrieval and metadata
  filters. Pluggable backends (`InMemStore` for tests, `ChromaStore` for
  production).
- **Session memory** with two compression strategies:
  - `HeuristicStrategy` — pure-Python sliding window (head + tail +
    keyword-bearing middle), no LLM cost.
  - `LLMSummaryStrategy` — calls the LLM per rolling window, falls back
    to `HeuristicStrategy` automatically on any failure.
- **User profile** auto-extracted from prior messages
  (`ProfileBuilder`).
- **Token budget** via `TokenBudgetedContextBuilder` with priority-based
  greedy allocation, word-boundary trimming, and a `BudgetReport` for
  observability.
- **Pluggable token counter** (`TokenCounter` protocol) with a
  multilingual `RegexTokenCounter` default and an optional
  `TiktokenCounter` adapter for exact counts.
- **Bilingual documentation** (Russian primary, English mirror) on
  GitHub Pages.

## Install

```bash
pip install protoprompt
pip install "protoprompt[chroma]"     # vector backend
pip install "protoprompt[tiktoken]"   # exact token counts
```

## Quickstart

```python
import asyncio
from protoprompt import (
    ContextBuilder, ContextInput, InMemStore, Pipeline,
    HeuristicStrategy, Session,
)

class MyLLM:
    async def chat(self, messages, model="", **options):
        return "stub"
    async def embed(self, texts, model=""):
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

asyncio.run(main())
```

## Compatibility

- Python 3.11, 3.12, 3.13
- `chromadb>=0.5,<0.6` (optional)
- `tiktoken>=0.5` (optional)

## Verification

- 51 unit + integration tests passing
- Test coverage: 90.5% of executable code
- CI matrix: 3.11 / 3.12 / 3.13

## Known limitations

- `TiktokenCounter` requires the optional `tiktoken` extra
  (`pip install protoprompt[tiktoken]`).
- Real ChromaDB tests run in CI; in pure unit-test environments the
  `chroma` tests skip automatically.
- This is `0.1.0` — public API may shift before `1.0.0`.

## Links

- Source: https://github.com/Idxeed/protoprompt
- Docs: https://idxeed.github.io/protoprompt/
- Issues: https://github.com/Idxeed/protoprompt/issues
- Changelog: https://github.com/Idxeed/protoprompt/blob/main/CHANGELOG.md
