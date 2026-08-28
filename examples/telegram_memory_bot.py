"""Persistent aiogram 3 memory bot using OpenAI or Ollama.

Environment:
    TELEGRAM_BOT_TOKEN=...
    PROTOPROMPT_PROVIDER=openai|ollama
    PROTOPROMPT_DB=telegram_memory.db

Install:
    pip install "protoprompt[telegram,openai]"
    # or: pip install "protoprompt[telegram,ollama]"
"""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from protoprompt import SqliteStore
from protoprompt.integrations import (
    OllamaClient,
    OpenAIClient,
    TelegramMemoryBot,
    TelegramMemoryRegistry,
    create_telegram_router,
)


def create_model():
    provider = os.environ.get("PROTOPROMPT_PROVIDER", "ollama").lower()
    if provider == "openai":
        return OpenAIClient(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL"),
            chat_model=os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
            embed_model=os.environ.get(
                "OPENAI_EMBED_MODEL", "text-embedding-3-small"
            ),
        )
    if provider == "ollama":
        return OllamaClient(
            host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
            chat_model=os.environ.get("OLLAMA_CHAT_MODEL", "llama3.1"),
            embed_model=os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        )
    raise ValueError("PROTOPROMPT_PROVIDER must be 'openai' or 'ollama'")


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN before starting the bot")
    database = os.environ.get("PROTOPROMPT_DB", "telegram_memory.db")
    model = create_model()
    store = SqliteStore(database)
    registry = TelegramMemoryRegistry(database)
    memory_bot = TelegramMemoryBot(store, model, registry)

    bot = Bot(token)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_telegram_router(memory_bot))
    await bot.set_my_commands([
        BotCommand(command="memory", description="Memory counters"),
        BotCommand(command="why", description="Why the bot recalled items"),
        BotCommand(command="forget", description="Delete my long-term memory"),
    ])
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        registry.close()
        store.close()
        close = getattr(model, "aclose", None)
        if close is not None:
            await close()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
