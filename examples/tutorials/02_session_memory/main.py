"""Урок 2: память сессии и сжатие.

Показывает, как Pipeline сжимает длинный диалог в короткие блоки и кладёт
их в хранилище под именем session_{chat_id}.

Запуск:
    python examples/tutorials/02_session_memory/main.py

Работает офлайн (заглушка FakeLLM).
"""

import asyncio
import hashlib
import re

from protoprompt import HeuristicStrategy, InMemStore, Pipeline, Session


class FakeLLM:
    """LLM-заглушка: эмбеддинги строятся из слов, сеть не нужна."""

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


DIALOG = [
    {"role": "user", "content": "Меня зовут Илья, настраиваю протопромпт."},
    {"role": "assistant", "content": "Отлично! Какие слои нужны?"},
    {"role": "user", "content": "RAG и память сессии, бюджет 2048."},
    {"role": "assistant", "content": "Принято. Хранилище какое?"},
    {"role": "user", "content": "SQLite локально, важно без сервисов."},
    {"role": "assistant", "content": "Разумно для разработки."},
    {"role": "user", "content": "Хочу ещё профиль пользователя позже."},
    {"role": "assistant", "content": "ProfileBuilder уже есть в ядре."},
    {"role": "user", "content": "Итог: RAG + память, SQLite, бюджет 2048."},
    {"role": "assistant", "content": "Конфигурация ясна, удачи!"},
    {"role": "user", "content": "И ещё кэш эмбеддингов подключи."},
    {"role": "assistant", "content": "Добавлю CachedLLMClient."},
]


async def main():
    llm = FakeLLM()
    store = InMemStore()

    session = Session(chat_id="c1", messages=DIALOG)
    pipeline = Pipeline(
        store, llm,
        strategy=HeuristicStrategy(),
        compress_every_n=10,  # сжимаем, когда сообщений не меньше 10
    )

    print(f"Сообщений в диалоге: {len(session.messages)}")

    if pipeline.should_compress(len(session.messages)):
        blocks = await pipeline.compress_and_store(session)
        print(f"Сжато в {len(blocks)} блоков")
        for i, block in enumerate(blocks, start=1):
            print(f"\n--- Блок {i} [{block.metadata.get('segment')}] ---")
            print(block.text)
    else:
        print("Сообщений слишком мало — сжимать ещё рано.")

    # Память сессии лежит в сторе. Проверяем, что её можно найти по смыслу.
    # Настоящие эмбеддинги поймут и перефразированный вопрос; примитивная
    # заглушка ловит только совпавшие слова (кэш, эмбеддингов).
    print("\n\nИщем память сессии по вопросу:")
    hits = store.query(
        (await llm.embed(["что за кэш эмбеддингов подключили"]))[0],
        top_k=2,
        where={"chat_id": "c1"},
    )
    for hit in hits:
        print(f"\nscore={hit['score']:.2f}")
        print(hit["document"])


if __name__ == "__main__":
    asyncio.run(main())