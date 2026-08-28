# Примеры protoprompt

Запускабельные рецепты для типовых сценариев. Каждый файл самодостаточен.

| Файл | Что показывает | Требует |
|---|---|---|
| `session_memory.py` | Сжатие сессии `Pipeline`, хуки, кэш эмбеддингов | ничего — работает офлайн |
| `rag.py` | RAG: чанкинг → индексация → поиск → контекст | ничего — работает офлайн |
| `ollama_rag.py` | RAG через нативный OllamaClient | запущенный Ollama + `pip install "protoprompt[ollama]"` |
| `openai_budgeted.py` | Токеновый бюджет, обрезка истории, `build_messages()` | `OPENAI_API_KEY` + `protoprompt[openai,tiktoken]` |
| `local_embeddings.py` | Локальные эмбеддинги + персистентный SqliteStore | `protoprompt[local]` или `[fastembed]` |

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