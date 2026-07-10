# Quickstart

A 30-line working example. Replace `MyLLM` with anything implementing
`LLMClientProtocol` (Ollama, OpenAI, anything) and you're done.

```python
import asyncio
from protoprompt import (
    ContextBuilder,
    ContextInput,
    InMemStore,
    HeuristicStrategy,
    Pipeline,
    Session,
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
        blocks = await pipeline.compress_and_store(session)
        print(f"compressed into {len(blocks)} blocks")


asyncio.run(main())
```

## With ChromaDB

```python
from protoprompt.store.chroma import ChromaStore

store = ChromaStore(persist_dir="./chroma_data")
```

## With token budget

```python
from protoprompt import TokenBudgetedContextBuilder
from protoprompt.tokens import RegexTokenCounter

builder = TokenBudgetedContextBuilder(
    store, llm,
    counter=RegexTokenCounter(),
    max_tokens=4096,
)
out = await builder.build(ContextInput(...))
print(out.budget_report.used_tokens, "/", out.budget_report.budget)
```

## With LLM summarisation

```python
from protoprompt import LLMSummaryStrategy

pipeline = Pipeline(
    store, llm,
    strategy=LLMSummaryStrategy(
        model="llama3.1",
        window_size=8,
        max_blocks=3,
        language="ru",
    ),
    compress_every_n=12,
)
```

The strategy falls back to `HeuristicStrategy` automatically on any
LLM error, so a degraded model never blocks a chat.
