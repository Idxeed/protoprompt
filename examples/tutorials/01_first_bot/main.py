"""Урок 1: первая память чат-бота.

Бот-географ хранит факты в векторном хранилище (InMemStore) и находит
ответ на вопрос по смыслу, даже если формулировки не совпадают.

Запуск:
    python examples/tutorials/01_first_bot/main.py

Работает офлайн: эмбеддинги строит заглушка FakeLLM из слов текста.
"""

import asyncio
import hashlib
import re

from protoprompt import InMemStore


class FakeLLM:
    """LLM-заглушка: эмбеддинги строятся из слов, сеть не нужна.

    В настоящем проекте замените на OllamaClient/OpenAIClient или
    любую реализацию LLMClientProtocol.
    """

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    async def chat(self, messages, model="", **options):
        return "Я заглушка. Подключите настоящую модель."

    async def embed(self, texts, model=""):
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in re.findall(r"[а-яёa-z0-9]+", text.lower()):
            idx = int(hashlib.md5(word.encode()).hexdigest()[:8], 16) % self.dim
            vec[idx] += 1.0
        norm = sum(x * x for x in vec) ** 0.5
        return [x / norm for x in vec] if norm else vec


FACTS = [
    ("Париж — столица Франции, стоит на реке Сена.",
     {"topic": "география"}),
    ("Берлин — столица Германии.",
     {"topic": "география"}),
    ("Python — язык программирования, придуманный Гвидо ван Россумом.",
     {"topic": "программирование"}),
]


async def main():
    llm = FakeLLM()
    store = InMemStore()

    # 1. Кладём факты в память: текст + эмбеддинг + ярлык
    for i, (text, meta) in enumerate(FACTS):
        store.add(f"fact-{i}", [text], await llm.embed([text]), meta)

    # 2. Спрашиваем бота
    questions = [
        "Какая столица у Франции?",
        "Расскажи про Берлин",
        "Кто придумал Python?",
        "Сколько будет 2+2?",
    ]

    for question in questions:
        print(f"\nВопрос: {question}")
        hits = store.query(
            (await llm.embed([question]))[0],
            top_k=1,
            score_threshold=0.2,  # ниже порога считаем, что ответа нет
        )
        if not hits:
            print("  (в памяти ничего похожего нет)")
            continue
        best = hits[0]
        print(f"  Похожесть: {best['score']:.2f}")
        print(f"  Ответ: {best['document']}")


if __name__ == "__main__":
    asyncio.run(main())