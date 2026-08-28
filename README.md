# protoprompt

[![CI](https://github.com/Idxeed/protoprompt/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/Idxeed/protoprompt/actions/workflows/ci.yml)
[![Python 3.11–3.13](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

**Контекстный движок для LLM-приложений:** RAG, память диалога, профиль
пользователя и строгий токен-бюджет через единый Python API.

[Документация](https://idxeed.github.io/protoprompt/ru/) ·
[English](README.en.md) ·
[Примеры](examples/) ·
[Каталог интеграций](INTEGRATIONS.md) ·
[Roadmap](ROADMAP.md) ·
[Как добавить интеграцию](CONTRIBUTING.md) ·
[Changelog](CHANGELOG.md)

> Проект находится в alpha-стадии. Публичный API уже покрыт тестами, но до
> версии 1.0 возможны изменения контрактов.

![Telegram-бот вспоминает старый факт и показывает provenance](docs/assets/telegram-memory.gif)

Эталонный [Telegram-бот](docs/ru/telegram.md) сохраняет длинную память в
SQLite, работает с OpenAI или Ollama и объясняет каждый recall через `/why`.

## Что решает protoprompt

LLM обычно нужна не просто история чата, а несколько разных видов контекста:
найденные документы, важные факты из прошлых сессий, профиль пользователя и
исходный system prompt. Если собирать всё вручную, логика поиска, приоритетов и
обрезки быстро расползается по приложению.

`protoprompt` собирает эти слои в одном месте и возвращает не только готовый
промпт, но и provenance — какие RAG-чанки, блоки памяти и данные профиля были
использованы.

| Возможность | Что входит |
|---|---|
| RAG | чанкинг, индексация, top-k поиск, фильтры, reranking и provenance |
| Память сессии | эвристическое или LLM-сжатие длинных диалогов |
| Профиль | извлечение, merge, optimistic locking и SQLite-хранилище |
| Токен-бюджет | жёсткий лимит, приоритеты слоёв и отчёт об обрезке |
| Хранилища | in-memory, SQLite, ChromaDB, Qdrant, pgvector, Elasticsearch/OpenSearch и Redis services |
| LLM и embeddings | OpenAI, Anthropic, Google GenAI, Bedrock, Ollama и локальные модели |
| Секреты | encrypted SQLite, AWS Secrets Manager и GCP Secret Manager |
| Connectivity | MCP, OpenAI Agents SDK, LangGraph, PydanticAI, LlamaIndex, aiogram 3 и FastAPI |
| Данные | bounded readers для text/source/HTML/PDF/DOCX и framework converters |

Ядро не имеет обязательных сторонних зависимостей. Интеграции подключаются
через extras и не импортируются, пока не понадобятся.

## Установка

```bash
pip install protoprompt

# Частые варианты
pip install "protoprompt[openai,tiktoken]"
pip install "protoprompt[ollama]"
pip install "protoprompt[chroma]"
pip install "protoprompt[qdrant]"
pip install "protoprompt[local]"       # sentence-transformers
pip install "protoprompt[fastembed]"
pip install "protoprompt[secrets]"
pip install "protoprompt[mcp]"
pip install "protoprompt[agents]"
pip install "protoprompt[langgraph]"
pip install "protoprompt[telegram,ollama]"
pip install "protoprompt[anthropic]"
pip install "protoprompt[google]"
pip install "protoprompt[bedrock]"
pip install "protoprompt[pydanticai]"
pip install "protoprompt[llamaindex]"
pip install "protoprompt[postgres,redis,otel]"
pip install "protoprompt[elasticsearch]"  # или opensearch
pip install "protoprompt[documents,fastapi]"
pip install "protoprompt[aws-secrets]"    # или gcp-secrets
```

Для работы из текущей ветки:

```bash
pip install "protoprompt @ git+https://github.com/Idxeed/protoprompt.git@master"
```

Требуется Python 3.11 или новее.

## Быстрый старт

Пример полностью локальный: сеть, API-ключ и сторонняя векторная БД не нужны.

```python
import asyncio

from protoprompt import ContextBuilder, ContextInput, InMemStore


class DemoLLM:
    async def embed(self, texts, model=""):
        # В приложении замените на OpenAIClient, OllamaClient
        # или локальный embedding-клиент.
        return [[1.0, 0.0] for _ in texts]

    async def chat(self, messages, model="", **options):
        return "demo"


async def main():
    llm = DemoLLM()
    store = InMemStore()
    chunks = [
        "protoprompt объединяет RAG, память сессии и профиль пользователя.",
        "Токеновый бюджет не позволяет итоговому контексту превысить лимит.",
    ]
    store.add("guide", chunks, await llm.embed(chunks))

    builder = ContextBuilder(store, llm)
    messages = await builder.build_messages(
        ContextInput(
            query="Что умеет protoprompt?",
            system_prompt="Отвечай кратко и только по контексту.",
            doc_ids=["guide"],
            include_session=False,
        ),
        user_message="Что умеет protoprompt?",
    )

    print(messages)  # готовый OpenAI-style список system + user


asyncio.run(main())
```

Более реалистичные рецепты находятся в [`examples/`](examples/): Ollama RAG,
OpenAI с токен-бюджетом, локальные embeddings, сжатие сессии, профиль и
зашифрованный vault.

## Как устроена сборка контекста

```text
запрос ─┬─> RAG по документам ───────┐
        ├─> память текущей сессии ───┤
        ├─> профиль пользователя ────┼─> ContextBuilder ─> ContextOutput
        └─> исходный system prompt ──┘          │
                                                └─ provenance + budget report
```

Основные контракты намеренно небольшие:

- `StoreProtocol` / `AsyncStoreProtocol` — синхронное или асинхронное
  векторное хранилище;
- `LLMClientProtocol` — `chat()` и `embed()`;
- `StrategyProtocol` — стратегия сжатия диалога;
- `TokenCounter` — подсчёт токенов для конкретной модели.

Благодаря этому встроенные адаптеры можно заменить своими без переписывания
сборщика контекста.

## Основные точки входа

```python
from protoprompt import (
    ContextBuilder,
    TokenBudgetedContextBuilder,
    Pipeline,
    ProfileManager,
    InMemStore,
    SqliteStore,
)

from protoprompt.rag import DocumentIndexer, Retriever
from protoprompt.secrets import EncryptedSqliteSecretStore, SecretAccess
from protoprompt.integrations import OpenAIClient, OllamaClient, QdrantStore
```

Полный API и подробные руководства:

- [быстрый старт](https://idxeed.github.io/protoprompt/ru/quickstart/);
- [RAG](https://idxeed.github.io/protoprompt/ru/rag/);
- [память и сжатие](https://idxeed.github.io/protoprompt/ru/concepts/compression/);
- [профиль пользователя](https://idxeed.github.io/protoprompt/ru/profile/);
- [секреты](https://idxeed.github.io/protoprompt/ru/secrets/);
- [интеграции](https://idxeed.github.io/protoprompt/ru/integrations/).

## Экспериментальный coding-agent

В монорепозитории есть CLI поверх `protoprompt.agent.WorkingMemory`:

```bash
pip install -e "apps/agent-cli[ollama]"
pp-agent /path/to/project
```

Он поддерживает сессии, hot/cold memory, план-режим и подтверждение опасных
инструментов. Подробнее — в [`apps/agent-cli/README.md`](apps/agent-cli/README.md).

## Разработка

```bash
git clone https://github.com/Idxeed/protoprompt.git
cd protoprompt
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

pip install -e ".[chroma,qdrant,dev]"
pytest
python scripts/build_docs.py --clean
```

CI проверяет Python 3.11–3.13, интеграционные тесты, CLI, содержимое wheel и
обе строгие сборки документации.

## Лицензия

[MIT](LICENSE)
