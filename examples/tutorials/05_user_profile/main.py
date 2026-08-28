"""Урок 5: профиль пользователя.

Показывает ProfileManager: как из сообщений появляются долговечные факты
и предпочтения, как они копятся между сессиями и как попадают в промпт.

Запуск:
    python examples/tutorials/05_user_profile/main.py

Работает офлайн: правила (RuleProfileSource) не зовут LLM, а LLM-источник
использует заглушку FakeLLM, которая отдаёт «сырой» JSON с русскими
метками — как это часто делают настоящие модели.
"""

import asyncio
import hashlib
import re

from protoprompt import ContextBuilder, ContextInput, InMemStore
from protoprompt.profile import (
    InMemoryProfileStore,
    LLMProfileSource,
    ProfileManager,
    RuleProfileSource,
    Signal,
    render,
)


class FakeLLM:
    """Заглушка с очередью ответов на chat и эмбеддингами из слов."""

    def __init__(self, *responses: str, dim: int = 64) -> None:
        self._queue = list(responses)
        self.chat_calls = 0
        self.dim = dim

    async def chat(self, messages, model="", **options):
        self.chat_calls += 1
        return self._queue.pop(0) if self._queue else "{}"

    async def embed(self, texts, model=""):
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in re.findall(r"[а-яёa-z0-9]+", text.lower()):
            idx = int(hashlib.md5(word.encode()).hexdigest()[:8], 16) % self.dim
            vec[idx] += 1.0
        norm = sum(x * x for x in vec) ** 0.5
        return [x / norm for x in vec] if norm else vec


def signals(*texts: str) -> list[Signal]:
    return [Signal(user_id="u1", kind="message", role="user", text=t) for t in texts]


async def main():
    llm = FakeLLM()

    # ── 1. Детерминированные правила: без LLM ─────────────────────
    print("=== 1. RuleProfileSource: правила, без LLM ===")
    rules = RuleProfileSource()
    delta = await rules.extract(
        "u1",
        signals("Здравствуйте, пожалуйста, помогите разобраться с задачей"),
    )
    print("  verbosity:", delta.traits.get("verbosity"))
    print("  language :", delta.preferences.get("language"))
    print("  formality:", delta.traits.get("formality"))

    # ── 2. LLM-источник: «грязный» JSON -> чистый профиль ──────────
    print("\n=== 2. LLMProfileSource: модель, ретрай и нормализация ===")
    messy = "не JSON, извините"           # первый ответ — мусор
    fenced = (
        "```json\n"
        '{"facts": [{"op": "add", "key": "Имя", "value": "Илья"},'
        '{"op": "add", "key": "стек", "value": "python"}],'
        '"traits": {"expertise": "эксперт"},'
        '"preferences": {"format": "списки", "language": "ru"},'
        '"summary": "Опытный Python-разработчик"}\n'
        "```"
    )
    llm2 = FakeLLM(messy, fenced)
    source = LLMProfileSource(llm2, language="ru", retries=1)
    delta = await source.extract("u1", signals("Я пишу на python и люблю списки"))
    print(f"  вызовов chat: {llm2.chat_calls} (первый был мусор -> ретрай)")
    print(f"  fact_ops   : {[(op.op, op.key, op.value) for op in delta.fact_ops]}")
    print(f"  traits     : {delta.traits}")
    print(f"  preferences: {delta.preferences}")

    # ── 3. ProfileManager: инкрементальное накопление ─────────────
    print("\n=== 3. ProfileManager: профиль растёт между сессиями ===")
    fenced2 = (
        '{"facts": [{"op": "add", "key": "роль", "value": "тимлид"}],'
        '"traits": {"expertise": "эксперт"},'
        '"preferences": {"format": "списки", "language": "ru"},'
        '"summary": "Тимлид, пишет на Python"}'
    )
    store = InMemoryProfileStore()
    manager = ProfileManager(store, LLMProfileSource(
        FakeLLM(fenced, fenced2), language="ru", retries=0,
    ))

    profile = await manager.update("u1", signals("Я пишу на python и люблю списки"))
    print(f"  после 1-й встречи: version={profile.version}, source={profile.source}")
    print(f"    facts      : {profile.facts}")
    print(f"    expertise  : {profile.traits.expertise}")

    profile = await manager.update(
        "u1", signals("Теперь я тимлид в команде из пяти человек")
    )
    print(f"  после 2-й встречи: version={profile.version}")
    print(f"    facts      : {profile.facts}")

    # ── 4. Профиль в промпте: render и ContextBuilder ─────────────
    print("\n=== 4. Профиль попадает в промпт ===")
    print(render(profile))

    builder = ContextBuilder(InMemStore(), llm)
    out = await builder.build(ContextInput(
        query="Подскажи, как организовать код?",
        include_profile=True,
        profile=profile,
        language="ru",
    ))
    print("\n--- system_prompt после ContextBuilder ---")
    print(out.system_prompt)

    # ── 5. Сброс и удаление ────────────────────────────────────────
    print("\n=== 5. reset и delete ===")
    fresh = await manager.reset("u1")
    print(f"  после reset: version={fresh.version}, facts={fresh.facts}")
    await manager.delete("u1")
    print(f"  после delete: профиль найден? {await manager.get('u1') is not None}")


if __name__ == "__main__":
    asyncio.run(main())