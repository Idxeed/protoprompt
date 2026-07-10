# Быстрый старт

Рабочий пример в 30 строк. Замените `MyLLM` на всё, что реализует
`LLMClientProtocol` (Ollama, OpenAI, что угодно) — и готово.

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
        return "заглушка"

    async def embed(self, texts, model=""):
        return [[0.1] * 384 for _ in texts]


async def main():
    store = InMemStore()
    store.add("doc-1", ["Париж — столица Франции."], [[0.5] * 384])
    llm = MyLLM()

    builder = ContextBuilder(store, llm)
    out = await builder.build(ContextInput(
        query="Какая столица Франции?",
        system_prompt="Ты учитель географии.",
        doc_ids=[1],
    ))
    print(out.system_prompt)

    pipeline = Pipeline(
        store, llm,
        strategy=HeuristicStrategy(),
        compress_every_n=10,
    )
    session = Session(chat_id="c1", messages=[
        {"role": "user", "content": "Привет"},
        {"role": "assistant", "content": "Здравствуйте!"},
    ])
    if pipeline.should_compress(len(session.messages)):
        blocks = await pipeline.compress_and_store(session)
        print(f"сжато в {len(blocks)} блоков")


asyncio.run(main())
```

## С ChromaDB

```python
from protoprompt.store.chroma import ChromaStore

store = ChromaStore(persist_dir="./chroma_data")
```

## С токен-бюджетом

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

## С LLM-суммаризацией

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

Стратегия автоматически откатывается на `HeuristicStrategy` при любой
ошибке LLM — деградация модели не должна ломать чат.

[English version](../en/quickstart.md)
