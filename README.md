# protoprompt

[![CI](https://github.com/Idxeed/protoprompt/actions/workflows/ci.yml/badge.svg)](https://github.com/Idxeed/protoprompt/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/codecov/c/github/Idxeed/protoprompt)](https://codecov.io/gh/Idxeed/protoprompt)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![RU](https://img.shields.io/badge/%D0%AF%D0%B7%D1%8B%D0%BA-RU-blue)](README.ru.md)
[![EN](https://img.shields.io/badge/Language-EN-blue)](README.en.md)

Слоистый сборщик контекста для LLM-промптов. Три независимых, компонуемых
слоя кормят модель: **RAG по документам**, **сжатая история сессии** и
опциональный **профиль пользователя**. Подключаемое векторное хранилище,
подключаемый токенайзер, подключаемая стратегия сжатия.

[English version](README.en.md)

## Зачем

Прод-LLM-приложения упираются в одну и ту же стену: модели нужен контекст,
но окно конечно. Собранная вручную сборка промпта превращается в кашу:
документы ищутся отдельно от истории, system prompt дублируется, и каждая
команда переписывает один и тот же клей.

`protoprompt` разделяет три задачи и даёт каждой чистый протокол:

- `StoreProtocol` — векторное хранилище (in-memory для тестов, ChromaDB для прода).
- `StrategyProtocol` — как сжимать старые ходы длинной сессии.
- `TokenCounter` — как считать бюджет финального промпта.

`ContextBuilder` оркестрирует все три. Результат — единый `system_prompt`
плюс структурированный `ContextOutput` с описанием того, что вошло (head,
tail, RAG, profile), чтобы UI мог показать provenance.

## Установка

```bash
pip install protoprompt

# С ChromaDB-бэкендом
pip install "protoprompt[chroma]"

# С tiktoken-токенайзером
pip install "protoprompt[tiktoken]"

# Для разработки и документации
pip install "protoprompt[chroma,dev]"
```

## Быстрый старт

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
        return "заглушка"

    async def embed(self, texts, model=""):
        # замените на реальные эмбеддинги
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
        await pipeline.compress_and_store(session)


asyncio.run(main())
```

## Архитектура

```
                 +--------------------+
                 |   ContextBuilder   |
                 |   (оркестратор)    |
                 +---------+----------+
                           |
        +------------------+------------------+------------------+
        |                  |                  |                  |
        v                  v                  v                  v
+---------------+  +----------------+  +---------------+  +---------+
| RAG-поиск     |  | Память сессии  |  | Профиль польз.|  |  Store  |
| (vector top-k)|  | (сжатая)       |  | (из LLM)      |  |  query  |
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

## Публичный API

| Модуль                   | Экспорты                                                            |
|--------------------------|---------------------------------------------------------------------|
| `protoprompt`            | `Pipeline`, `ContextBuilder`, `ContextInput`, `ContextOutput`      |
| `protoprompt.store`      | `StoreProtocol`, `InMemStore`, `ChromaStore`                        |
| `protoprompt.session`    | `Session`, `CompressedBlock`, `HeuristicStrategy`, `LLMSummaryStrategy` |
| `protoprompt.profile`    | `UserProfile`, `ProfileBuilder`                                     |
| `protoprompt.tokens`     | `TokenCounter`, `RegexTokenCounter`, `TiktokenCounter`              |
| `protoprompt.llm`        | `LLMClientProtocol`                                                 |

## Документация

Полная документация собирается в двух языковых версиях:

- 🇷🇺 Русская: <https://idxeed.github.io/protoprompt/ru/>
- 🇬🇧 English: <https://idxeed.github.io/protoprompt/en/>

Локальная сборка:

```bash
pip install "protoprompt[dev]"
python scripts/build_docs.py --serve  # обе версии на разных портах
```

## Разработка

```bash
git clone https://github.com/Idxeed/protoprompt
cd protoprompt
python -m venv .venv && source .venv/bin/activate
pip install -e ".[chroma,dev]"
pytest
```

## Лицензия

MIT — см. [LICENSE](LICENSE).
