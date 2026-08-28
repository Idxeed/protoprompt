# Telegram memory bot

The aiogram 3 demo is a real polling application with persistent SQLite
memory. It automatically stores completed turns, recalls relevant old turns,
keeps a small hot-history window, and exposes content-free diagnostics.

## Install and run

Choose one model backend:

=== "Ollama"

    ```bash
    pip install "protoprompt[telegram,ollama]"
    ollama pull llama3.1 nomic-embed-text
    export TELEGRAM_BOT_TOKEN="..."
    export PROTOPROMPT_PROVIDER="ollama"
    python examples/telegram_memory_bot.py
    ```

=== "OpenAI"

    ```bash
    pip install "protoprompt[telegram,openai]"
    export TELEGRAM_BOT_TOKEN="..."
    export OPENAI_API_KEY="..."
    export PROTOPROMPT_PROVIDER="openai"
    python examples/telegram_memory_bot.py
    ```

On PowerShell, use `$env:NAME="value"`. `PROTOPROMPT_DB` selects the SQLite
file and defaults to `telegram_memory.db`. Model names and endpoints can be
changed with `OPENAI_*` or `OLLAMA_*` environment variables shown in the
example source.

Do not reuse an existing database after changing to an embedding model with a
different vector dimension. Use a new database or re-embed the stored memory.

## Commands and privacy

- `/memory` reports current-thread, all-thread, and hot-memory counts;
- `/why` shows ids and similarity scores for the last recall, never text;
- `/forget` explains the destructive action;
- `/forget confirm` deletes this Telegram user's registered long-term memory
  across chats.

The host derives `MemoryScope` from Telegram's trusted user/chat ids. Model
text cannot choose another user or tenant. The deletion registry stores only
scope fields and opaque memory ids; conversation text stays in the vector
store. The bot does not log tokens or message content by default.

## Reproducible long-dialog check

The deterministic offline scenario inserts an access-code fact at turn 3 of a
100-turn conversation and gives FIFO and LRU baselines a capacity of 12:

```bash
python examples/telegram_long_dialog.py
```

Expected result: both bounded retention baselines lose the unaccessed early
fact; semantic memory retrieves `turn-2`. This demonstrates a retention and
retrieval difference, not literally infinite storage or guaranteed factual
recall for arbitrary embedding models.
