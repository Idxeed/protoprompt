# protoprompt

[![CI](https://github.com/Idxeed/protoprompt/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/Idxeed/protoprompt/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/protoprompt.svg)](https://pypi.org/project/protoprompt/)
[![Python 3.11–3.13](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

**Надёжная память агента в фиксированном контекстном окне.** ProtoPrompt —
embeddable context runtime: RAG, память диалога, профиль пользователя и
объяснимый строгий token budget через единый Python API.

[Документация](https://idxeed.github.io/protoprompt/ru/) ·
[English](README.en.md) ·
[Примеры](examples/) ·
[Каталог интеграций](INTEGRATIONS.md) ·
[Roadmap](ROADMAP.md) ·
[Launch v0.17.0](LAUNCH-v0.17.0.md) ·
[Как добавить интеграцию](CONTRIBUTING.md) ·
[Changelog](CHANGELOG.md)

> Проект находится в alpha-стадии. Публичный API уже покрыт тестами, но до
> версии 1.0 возможны изменения контрактов.

![Telegram-бот вспоминает старый факт и показывает provenance](docs/assets/telegram-memory.gif)

![Жизненный цикл Memory Ledger](docs/assets/memory-ledger-lifecycle.svg)

Эталонный [Telegram-бот](docs/ru/telegram.md) сохраняет длинную память в
SQLite, работает с OpenAI или Ollama и объясняет каждый recall через `/why`.

## Что решает protoprompt

LLM обычно нужна не просто история чата, а несколько разных видов контекста:
найденные документы, важные факты из прошлых сессий, профиль пользователя и
исходный system prompt. Если собирать всё вручную, логика поиска, приоритетов и
обрезки быстро расползается по приложению.

`protoprompt` собирает эти слои в одном месте и возвращает не только готовый
промпт, но и provenance — какие RAG-чанки, блоки памяти и данные профиля были
использованы. Наша ставка до `1.0`: **хранить можно долго, но в активный
контекст попадёт только объяснимый набор, который действительно помещается**.

| Возможность | Что входит |
|---|---|
| RAG | чанкинг, индексация, top-k поиск, фильтры, reranking и provenance |
| Память сессии | эвристическое или LLM-сжатие длинных диалогов |
| Профиль | извлечение, merge, optimistic locking и SQLite-хранилище |
| Memory Ledger *(experimental)* | scoped lifecycle, host admission, immutable provenance/audit, atomic source revocation и проверяемая очистка live rows; SQLite и optional fresh-v6 PostgreSQL backend |
| Bounded ledger recall *(experimental)* | local deterministic selection active memory в JSON data lane; opt-in admitted-only composition в exact request, sealed strict-recall resume без agent/provider state, token/byte receipt и final stale check |
| Токен-бюджет | жёсткий лимит, приоритеты слоёв, `ContextPlan`/receipt и объяснение обрезки |
| Хранилища | in-memory, SQLite, ChromaDB, Qdrant, pgvector, Elasticsearch/OpenSearch и Redis services |
| LLM и embeddings | OpenAI, Anthropic, Google GenAI, Bedrock, Ollama и локальные модели |
| Секреты | encrypted SQLite, AWS Secrets Manager и GCP Secret Manager |
| Connectivity | MCP, OpenAI Agents SDK, LangGraph, PydanticAI, LlamaIndex, aiogram 3 и FastAPI |
| Данные | bounded readers для text/source/HTML/PDF/DOCX и framework converters |

Ядро не имеет обязательных сторонних зависимостей. Интеграции подключаются
через extras и не импортируются, пока не понадобятся.

Экспериментальный `LedgerRecallPlanner` — первый безопасный read-path для
текущей задачи агента: для concrete v5 origin он выбирает только active,
host-admitted записи с проверенным audit, создаёт свежий ограниченный JSON data
lane и fail-closed при изменении памяти до отправки. Standalone planner не
подмешивает записи в system prompt и не заменяет финальный request accounting.
Явный `LedgerContextComposer` использует этот accounting для одного bounded
request, требует strict admission policy и того же scope/counter; raw `unknown`
и `legacy_unknown` в composed request не попадают. Ни `pp-ollama-chat`, ни
`pp-agent` пока не подключают composer автоматически. См. [Bounded recall из
ledger](docs/ru/ledger-recall.md).

v0.17 добавляет ещё более узкий experimental `TaskResumePlanner`: trusted host
связывает canonical `TaskEpisode` с отдельным task scope, HMAC-sealed Ledger
checkpoint и своим durable mapping `{task_ref, descriptor, checkpoint_id}`.
При resume снова проверяются scope, admission, typed payload и lifecycle, а
текущий `ContextInput.query` остаётся live RAG query. `TaskProcedure` пока
является только typed reference data и не выбирается. Это не agent/workflow
checkpoint, не tool authority и не auto-wiring для `pp-ollama-chat` или
`pp-agent`. См. [возобновление задачи](docs/ru/task-resume.md).

v0.13 добавляет experimental `PostgresMemoryLedger`: тот же явный sync
`MemoryWriter` contract и Ledger v6 в выделенной PostgreSQL schema. Backend
принимает только fresh v6 setup, сериализует записи schema-wide advisory
lock-ом с 5-секундной границей retry и fail-closed валидирует storage layout.
Он не переносит автоматически SQLite/старые PostgreSQL Ledger, не обещает
throughput и не превращает Ledger в agent state, workflow engine или
exactly-once механизм. См. [руководство PostgreSQL](docs/ru/postgres.md).

v0.14 — RC-hardening без расширения public API: bounded property/state-machine
gate проверяет scope, lifecycle, deletion/source revocation и exact Ledger
token/byte accounting на SQLite и PostgreSQL. Он не является latency,
throughput или model-quality benchmark.

v0.15 добавил frozen dual-backend evidence protocol для strict Ledger
selection: один content-free synthetic fixture обязан дать одинаковую
семантику на SQLite и PostgreSQL. v0.16 hardens reference-agent boundary:
user-owned state, identity-bound project namespaces, strict native jail
operations и явный provider transport. v0.17 добавляет только host-owned
task-episode resume boundary; это не claim о качестве модели, general recall,
latency, throughput или package `1.0.0`.

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

### Изменение HTTP-клиентов в 0.16

`HttpxLLMClient` и `OllamaClient` теперь по умолчанию создают транспорт с
`trust_env=False`. Это больше не наследует proxy/CA-настройки процесса и не
может незаметно направить prompts или bearer token через ambient route. Если
приложение сознательно полагалось на такие environment-настройки, включите их
явно и только для доверенного endpoint:

```python
HttpxLLMClient(base_url="https://llm.example", trust_env=True)
OllamaClient(host="https://ollama.example", trust_env=True)
```

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
from protoprompt.ledger import MemoryReviewGate, MemoryWriter, SqliteMemoryLedger
from protoprompt.secrets import EncryptedSqliteSecretStore, SecretAccess
from protoprompt.integrations import OpenAIClient, OllamaClient, QdrantStore
```

Полный API и подробные руководства:

- [быстрый старт](https://idxeed.github.io/protoprompt/ru/quickstart/);
- [RAG](https://idxeed.github.io/protoprompt/ru/rag/);
- [память и сжатие](https://idxeed.github.io/protoprompt/ru/concepts/compression/);
- [экспериментальный Memory Ledger](https://idxeed.github.io/protoprompt/ru/memory-ledger/);
- [профиль пользователя](https://idxeed.github.io/protoprompt/ru/profile/);
- [секреты](https://idxeed.github.io/protoprompt/ru/secrets/);
- [интеграции](https://idxeed.github.io/protoprompt/ru/integrations/).

## Локальный Ollama web chat

В репозитории есть reference-интерфейс для локальной Ollama: чат, PDF RAG и
долговременный архив диалогов. Он хранит полный transcript локально, но перед
каждым ответом передаёт модели только новый `ContextPlan` в заданном бюджете.

```bash
ollama pull llama3.1
ollama pull nomic-embed-text
pip install -e ".[documents,fastapi,ollama]"
pip install -e "apps/ollama-chat"
pp-ollama-chat
```

По умолчанию UI слушает только `127.0.0.1`. Подробности о хранении, удалении,
remote-Ollama opt-in и пределах token estimate — в
[`apps/ollama-chat/README.md`](apps/ollama-chat/README.md).

## Экспериментальный coding-agent

В монорепозитории есть CLI поверх `protoprompt.agent.WorkingMemory`:

```bash
pip install -e ".[ollama]"
pip install -e "apps/agent-cli[ollama]"
pp-agent /path/to/project
```

В релизе 0.17.0 `protoprompt-cli` не публикуется отдельно в PyPI:
соответствующие wheel и sdist прикрепляются к GitHub Release. Для локального
Ollama setup:

```bash
python -m pip install "protoprompt[ollama]==0.17.0"
python -m pip install "https://github.com/Idxeed/protoprompt/releases/download/v0.17.0/protoprompt_cli-0.17.0-py3-none-any.whl"
```

Вместо wheel можно после установки соответствующей версии ядра поставить
source из тега:
`python -m pip install "git+https://github.com/Idxeed/protoprompt.git@v0.17.0#subdirectory=apps/agent-cli"`.

Он поддерживает сессии, hot/cold memory, план-режим и подтверждение опасных
инструментов. Каждый вызов модели собирается через immutable `ContextPlan`:
system context, tail, обязательный input и reserve ответа учитываются под одним
жёстким лимитом; action и его результат не разрываются при обрезке. Подробнее —
в [`apps/agent-cli/README.md`](apps/agent-cli/README.md).

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

Офлайн-гейт памяти запускается без Ollama и сети:

```bash
python scripts/run_memory_benchmark.py --suite v0.1 --verify
python scripts/run_memory_benchmark.py --suite v0.2 --verify
python scripts/run_memory_benchmark.py --suite v0.3 --verify
python scripts/run_memory_benchmark.py --suite v0.4 --verify
```

Отдельный v1 evidence gate проверяет один строгий Ledger fixture сразу на
SQLite и PostgreSQL. Для него нужны локальный PostgreSQL и
`protoprompt[postgres]`; одиночный backend нельзя выдать за verified result:

```powershell
$env:PROTOPROMPT_POSTGRES_DSN = "postgresql://protoprompt:protoprompt@localhost:5432/protoprompt_test"
python scripts/run_memory_benchmark.py --suite v1.0 --ledger-backend all --verify
```

`v1.0` — версия протокола доказательств, а не уже вышедшая версия пакета
`1.0.0`. Он измеряет только зафиксированную selection semantics synthetic
fixture, не model quality, latency или throughput.

Его сценарии, фиксированные baseline и правила версионирования находятся в
[`benchmarks/README.md`](benchmarks/README.md). CI проверяет Python 3.11–3.13,
интеграционные тесты, CLI, содержимое wheel, этот benchmark и обе строгие
сборки документации.

## Лицензия

[MIT](LICENSE)
