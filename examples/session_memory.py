"""Fully offline demo: session compression pipeline with hooks and an
embedding cache. No API keys, no running services — just `python
examples/session_memory.py`.

The fake LLM derives embeddings from the text itself, so cosine
similarity still behaves sensibly for the demo.
"""

from __future__ import annotations

import asyncio
import hashlib

from protoprompt import (
    CachedLLMClient,
    InMemoryEmbeddingCache,
    InMemStore,
    Pipeline,
    PipelineHooks,
    Session,
)


class OfflineLLM:
    """Deterministic local double: hash-based embeddings, canned chat."""

    def __init__(self, dim: int = 64) -> None:
        self._dim = dim
        self.embed_calls = 0

    async def chat(self, messages, model="", **options):
        return "[offline] сжатие выполнено"

    async def embed(self, texts, model=""):
        self.embed_calls += 1
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vector = [b / 255.0 for b in digest[: self._dim]]
            norm = sum(v * v for v in vector) ** 0.5 or 1.0
            vectors.append([v / norm for v in vector])
        return vectors


def _make_session() -> Session:
    turns = [
        ("user", "Привет! Меня зовут Ира, я готовлюсь к марафону."),
        ("assistant", "Отлично, Ира! Какой у вас текущий план тренировок?"),
        ("user", "Бегаю три раза в неделю, хочу выйти на 10 км без остановок."),
        ("assistant", "Хорошая цель. Сколько километров даёте сейчас?"),
        ("user", "Пока 5 км, важно не травмироваться наращивая объём."),
        ("assistant", "Разумно. Добавляйте по 10% объёма в неделю."),
        ("user", "Думаю ещё купить кроссовки для плоской стопы."),
        ("assistant", "Да, правильная обувь критична. Обратитесь в спец-магазин."),
        ("user", "Итог: план — 3 тренировки, +10% объёма, кроссовки."),
        ("assistant", "Верный итог. Удачи на тренировках!"),
        ("user", "Спасибо, задача понятна, приступаю завтра."),
        ("assistant", "Жду отчёт через неделю!"),
    ]
    return Session(
        chat_id="running",
        messages=[{"role": r, "content": c} for r, c in turns],
    )


async def main() -> None:
    events: list[str] = []
    llm = CachedLLMClient(OfflineLLM(), InMemoryEmbeddingCache())
    pipeline = Pipeline(
        InMemStore(),
        llm,
        compress_every_n=6,
        hooks=PipelineHooks(
            on_before_compress=lambda s: events.append(f"compress {s.chat_id}"),
            on_after_compress=lambda s, b: events.append(f"{len(b)} blocks stored"),
            on_skip_compress=lambda s: events.append("skip"),
        ),
    )

    session = _make_session()
    blocks = await pipeline.compress_and_store(session)
    for block in blocks:
        print(f"[{block.metadata.get('segment')}] {block.text[:80]}…")

    # повторный прогон того же текста — эмбеддинги берутся из кэша
    await pipeline.compress_and_store(_make_session())
    print("events:", events)
    print("upstream embed calls:", llm.inner.embed_calls, "(второй вызов из кэша)")


if __name__ == "__main__":
    asyncio.run(main())
