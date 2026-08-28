# Примеры protoprompt

Запускабельные рецепты для типовых сценариев. Каждый файл самодостаточен.

| Файл | Что показывает | Требует |
|---|---|---|
| `session_memory.py` | Сжатие сессии `Pipeline`, хуки, кэш эмбеддингов | ничего — работает офлайн |
| `rag.py` | RAG: чанкинг → индексация → поиск → контекст | ничего — работает офлайн |
| `ollama_rag.py` | RAG через нативный OllamaClient | запущенный Ollama + `pip install "protoprompt[ollama]"` |
| `openai_budgeted.py` | Токеновый бюджет, обрезка истории, `build_messages()` | `OPENAI_API_KEY` + `protoprompt[openai,tiktoken]` |
| `local_embeddings.py` | Локальные эмбеддинги + персистентный SqliteStore | `protoprompt[local]` или `[fastembed]` |
| `contract_kit.py` | Проверка собственного embedding/vector adapter | ничего — работает офлайн |
| `mcp_memory_server.py` | Scoped MCP tools/resources через stdio или Streamable HTTP | `protoprompt[mcp]` |
| `openai_agents_session.py` | Plain Agents history vs budgeted scoped recall | `protoprompt[agents]` |
| `langgraph_memory.py` | Thread checkpointer + scoped cross-thread profile | `protoprompt[langgraph]` |
| `telegram_memory_bot.py` | Persistent aiogram 3 bot, OpenAI/Ollama | `protoprompt[telegram,openai]` or `[telegram,ollama]` |
| `telegram_long_dialog.py` | Deterministic FIFO/LRU vs semantic recall | nothing — works offline |
| `otel_tracing.py` | Content-safe spans to an OTLP/Jaeger collector | `protoprompt[otel]` |
| `secret_store.py` | Шифрованный scoped vault без утечки credentials | `protoprompt[secrets]` |
| `provider_clients.py` | Native Anthropic / Google GenAI / Bedrock + exact token count | соответствующий provider extra и credentials |
| `pydantic_ai_memory.py` | Настоящий PydanticAI Agent + scoped recall, офлайн | `protoprompt[pydanticai]` |
| `llamaindex_memory.py` | Настоящий LlamaIndex Memory block + scoped recall, офлайн | `protoprompt[llamaindex]` |
| `search_vector_store.py` | Один vector-store flow на Elasticsearch или OpenSearch | `protoprompt[elasticsearch]` или `[opensearch]` + сервер |
| `cloud_secret_store.py` | Scoped AWS/GCP secret lifecycle без печати значения | `[aws-secrets]` или `[gcp-secrets]` + тестовый cloud account |
| `fastapi_memory_service.py` | Authenticated HTTP memory API + lifespan | `protoprompt[fastapi]` |
| `read_documents.py` | Bounded local text/PDF/DOCX/HTML ingestion + provenance | `protoprompt[documents]` для office/web formats |

Быстрый старт без сети и ключей:

```bash
python examples/session_memory.py
```

## Обучающие проекты

Небольшие проекты для курса «Память в LLM-приложениях» (см.
[docs/ru/tutorials](../docs/ru/tutorials/index.md)). Работают офлайн —
эмбеддинги строит заглушка `FakeLLM`.

| Проект | Тема | Запуск |
|---|---|---|
| `tutorials/01_first_bot/` | Хранилище и поиск по смыслу (`store`) | `python examples/tutorials/01_first_bot/main.py` |
| `tutorials/02_session_memory/` | Сжатие длинного диалога (`session`) | `python examples/tutorials/02_session_memory/main.py` |
| `tutorials/03_embedding_cache/` | LRU-кэш эмбеддингов (`cache`) | `python examples/tutorials/03_embedding_cache/main.py` |
| `tutorials/04_coder_agent/` | Рабочая память код-агента (`agent`) | `python examples/tutorials/04_coder_agent/main.py` |
| `tutorials/05_user_profile/` | Профиль пользователя (`profile`) | `python examples/tutorials/05_user_profile/main.py` |
