"""Урок 4: рабочая память код-агента.

Показывает WorkingMemory: цель, разные виды элементов, вытеснение слабого
в холодную зону и возврат по запросу через recall.

Запуск:
    python examples/tutorials/04_coder_agent/main.py

Работает офлайн (заглушка FakeLLM + InMemStore в роли холодной зоны).
"""

import asyncio
import hashlib
import re

from protoprompt import InMemStore, RegexTokenCounter
from protoprompt.agent import WorkingMemory


class FakeLLM:
    """LLM-заглушка: эмбеддинги строятся из слов, сеть не нужна."""

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    async def chat(self, messages, model="", **options):
        return "Я заглушка."

    async def embed(self, texts, model=""):
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in re.findall(r"[а-яёa-z0-9]+", text.lower()):
            idx = int(hashlib.md5(word.encode()).hexdigest()[:8], 16) % self.dim
            vec[idx] += 1.0
        norm = sum(x * x for x in vec) ** 0.5
        return [x / norm for x in vec] if norm else vec


async def main():
    llm = FakeLLM()
    mem = WorkingMemory(
        store=InMemStore(),          # холодная зона
        llm=llm,
        counter=RegexTokenCounter(),
        max_tokens=600,              # горячая зона маленькая, чтобы показать вытеснение
    )

    await mem.set_goal("добавить функцию count_attempts в tenacity")

    # Важные элементы: файл, правка, заметка агента
    await mem.add("file", "# retry.py\n- Retrying\n- retry_if_exception",
                  summary="файл retry.py")
    await mem.add("edit", "def count_attempts(retry_state): return retry_state.attempt_number",
                  summary="новая функция count_attempts")
    await mem.note("count_attempts возвращает 0, если атрибута нет")

    # Шум: куча сырых логов, которая должна вытесниться
    for i in range(30):
        await mem.add("log", f"debug шаг {i}: почистил кэш, перечитал файл, гоняю тест",
                      summary=f"лог {i}")

    print(f"Элементов в горячей зоне: {len(mem.items)}")
    print(f"Выселено в холодную зону: {mem.evictions}")
    print(f"Записей в манифесте (холод): {len(mem.manifest.entries)}")
    print("\nЧто осталось в горячей зоне:")
    for item in mem.items.values():
        print(f"  {item.id} [{item.kind}] {item.label[:60]}")

    # Горячая зона — это и есть контекст для модели
    ctx = await mem.assemble()
    print("\n--- Контекст для модели (первые 400 символов) ---")
    print(ctx.render()[:400])

    # Возврат из холода: что-то вытеснилось, но вопрос важный
    restored = await mem.recall("что делает count_attempts?")
    print(f"\nВозвращено из холода: {restored}")


if __name__ == "__main__":
    asyncio.run(main())