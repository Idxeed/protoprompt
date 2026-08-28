from __future__ import annotations

import pytest

from protoprompt import InMemStore
from protoprompt.integrations.telegram import (
    TelegramMemoryBot,
    TelegramMemoryRegistry,
    create_telegram_router,
)

from _mocks import MockLLM

pytest.importorskip("aiogram")


@pytest.mark.asyncio
async def test_memory_bot_recalls_reports_and_forgets_all_user_threads(tmp_path):
    store = InMemStore()
    llm = MockLLM(embed_dim=8)
    registry = TelegramMemoryRegistry(str(tmp_path / "telegram.db"))
    app = TelegramMemoryBot(store, llm, registry, max_tokens=200)

    await app.reply("alice", "chat-a", "My contract renews on 15 May")
    answer = await app.reply("alice", "chat-a", "When does my contract renew?")
    await app.reply("alice", "chat-b", "I prefer concise answers")
    await app.reply("bob", "chat-a", "Bob's private note")

    assert answer == "mocked response"
    assert "renews on 15 May" in llm.chat_calls[1]["messages"][0]["content"]
    status = app.memory_status("alice", "chat-a")
    assert status.current_thread == 2
    assert status.all_threads == 3
    assert status.hot_messages == 4
    why = app.why("alice", "chat-a")
    assert why["result_count"] == 1
    assert "text" not in why["results"][0]
    assert why["budget"]["budget"] == 200

    assert await app.forget_user("alice") == 3
    assert app.memory_status("alice", "chat-a").all_threads == 0
    assert app.memory_status("bob", "chat-a").all_threads == 1
    assert store.count() == 1


def test_registry_is_persistent_and_contains_no_message_text(tmp_path):
    path = str(tmp_path / "registry.db")
    first = TelegramMemoryRegistry(path)
    scope = TelegramMemoryBot(
        InMemStore(), MockLLM(), first
    ).scope_for("alice", "chat")
    first.add(scope, "opaque-id")
    first.close()

    second = TelegramMemoryRegistry(path)
    assert second.list_user("telegram", "alice") == [("chat", "opaque-id")]
    second.close()


def test_aiogram_router_registers_memory_commands():
    app = TelegramMemoryBot(InMemStore(), MockLLM(), TelegramMemoryRegistry())
    router = create_telegram_router(app)
    assert router.name == "protoprompt-memory"
    assert len(router.message.handlers) == 5
