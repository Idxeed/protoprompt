"""Офлайн-демо движка профиля пользователя.

Показывает кросс-сессионный поток: сигналы → источник → ProfileManager →
инкрементальный merge → рендер → вставка в ContextBuilder.

Работает без сети и ключей (источник — правила):

    python examples/profile.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protoprompt import ContextBuilder, ContextInput, InMemStore  # noqa: E402
from protoprompt.profile import (  # noqa: E402
    ProfileManager,
    RuleProfileSource,
    Signal,
    SqliteProfileStore,
    render,
)


class StubLLM:
    async def chat(self, messages, model="", **options):
        return "заглушка"

    async def embed(self, texts, model=""):
        return [[0.1] * 16 for _ in texts]


async def main() -> None:
    store = SqliteProfileStore(":memory:")
    manager = ProfileManager(store, RuleProfileSource())

    first = await manager.update("u1", [
        Signal(
            user_id="u1", kind="message", role="user",
            text="Здравствуйте, помогите, пожалуйста, настроить "
                 "RAG-пайплайн на Python и SQLite.",
        ),
        Signal(
            user_id="u1", kind="message", role="user",
            text="Предпочитаю короткие ответы списками.",
        ),
    ])
    print("=== после первого update ===")
    print(f"version={first.version} source={first.source} "
          f"language={first.preferences.language} "
          f"verbosity={first.traits.verbosity} "
          f"formality={first.traits.formality}")

    # профиль накапливается между вызовами
    second = await manager.update("u1", [
        Signal(
            user_id="u1", kind="feedback",
            text="Мне нравятся технические детали.",
        ),
    ])
    print("\n=== после второго update ===")
    print(f"version={second.version} (было {first.version})")

    print("\n=== рендер профиля ===")
    print(render(second))

    # интеграция со сборщиком контекста: структурированный профиль
    builder = ContextBuilder(InMemStore(), StubLLM())
    out = await builder.build(ContextInput(
        query="как настроить RAG?",
        system_prompt="Ты ассистент по настройке.",
        include_profile=True,
        profile=second,
        language="ru",
    ))
    print("\n=== system_prompt ===")
    print(out.system_prompt)

    # с LLM-источником вместо правил (нужен живой LLM):
    # from protoprompt.profile import LLMProfileSource
    # manager = ProfileManager(store, LLMProfileSource(llm, language="ru"))


if __name__ == "__main__":
    asyncio.run(main())
