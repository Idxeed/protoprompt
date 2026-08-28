"""Урок 3: LRU-кэш эмбеддингов.

Показывает, как CachedLLMClient запоминает векторы: повторный текст не идёт
в модель за эмбеддингом, а берётся из кэша.

Запуск:
    python examples/tutorials/03_embedding_cache/main.py

Работает офлайн (заглушка FakeLLM со счётчиком вызовов).
"""

import asyncio
import hashlib
import re

from protoprompt import CachedLLMClient, InMemoryEmbeddingCache


class FakeLLM:
    """LLM-заглушка, которая ещё и считает, сколько раз её звали."""

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim
        self.embed_calls = 0
        self.embedded_texts = 0

    async def chat(self, messages, model="", **options):
        return "Я заглушка."

    async def embed(self, texts, model=""):
        self.embed_calls += 1
        self.embedded_texts += len(texts)
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in re.findall(r"[а-яёa-z0-9]+", text.lower()):
            idx = int(hashlib.md5(word.encode()).hexdigest()[:8], 16) % self.dim
            vec[idx] += 1.0
        norm = sum(x * x for x in vec) ** 0.5
        return [x / norm for x in vec] if norm else vec


async def main():
    raw = FakeLLM()
    llm = CachedLLMClient(raw, InMemoryEmbeddingCache(capacity=128))

    texts = ["что я решил насчёт хранилища?", "как устроен кэш?"]

    # 1. Первый раз — модель эмбеддит оба текста
    v1 = await llm.embed(texts)
    print(f"1. вызовов модели: {raw.embed_calls}")

    # 2. Тот же список — оба вектора берутся из кэша
    v2 = await llm.embed(texts)
    print(f"2. вызовов модели: {raw.embed_calls} (не изменилось)")

    # 3. Частичное совпадение: один старый текст + один новый
    v3 = await llm.embed(["что я решил насчёт хранилища?", "новый вопрос"])
    print(f"3. вызовов модели: {raw.embed_calls} (+1 только за новый текст)")

    print(f"\nВсего текстов заэмбедчено моделью: {raw.embedded_texts}")
    print(f"Запросов к llm.embed было: 3 (по 2 текста) = 6 текстов")
    print(f"Из них кэш отдал без модели: {6 - raw.embedded_texts}")
    print(f"Векторы из кэша совпадают с исходными: {v1 == v2}")


if __name__ == "__main__":
    asyncio.run(main())