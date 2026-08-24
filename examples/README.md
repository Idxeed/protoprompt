# Примеры protoprompt

Запускабельные рецепты для типовых сценариев. Каждый файл самодостаточен.

| Файл | Что показывает | Требует |
|---|---|---|
| `session_memory.py` | Сжатие сессии `Pipeline`, хуки, кэш эмбеддингов | ничего — работает офлайн |
| `ollama_rag.py` | RAG через нативный OllamaClient | запущенный Ollama + `pip install "protoprompt[ollama]"` |
| `openai_budgeted.py` | Токеновый бюджет, обрезка истории, `build_messages()` | `OPENAI_API_KEY` + `protoprompt[openai,tiktoken]` |
| `local_embeddings.py` | Локальные эмбеддинги + персистентный SqliteStore | `protoprompt[local]` или `[fastembed]` |

Быстрый старт без сети и ключей:

```bash
python examples/session_memory.py
```
