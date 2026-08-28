"""Офлайн-демо RAG-движка: чанкинг → индексация → поиск → контекст.

Работает без сети и ключей (эмбеддинги — детерминированная заглушка):

    python examples/rag.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protoprompt import ContextBuilder, ContextInput, InMemStore  # noqa: E402
from protoprompt.rag import (  # noqa: E402
    DocumentIndexer,
    FixedSizeChunker,
    Retriever,
)


class StubLLM:
    """Детерминированные эмбеддинги: похожие строки → похожие векторы."""

    async def chat(self, messages, model="", **options):
        return "заглушка"

    async def embed(self, texts, model=""):
        out = []
        for t in texts:
            words = t.lower().split()
            v = [0.0] * 16
            for w in words:
                v[hash(w) % 16] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


async def main() -> None:
    store = InMemStore()
    llm = StubLLM()

    indexer = DocumentIndexer(store, llm, chunker=FixedSizeChunker(40, overlap=8))
    n = await indexer.index(
        "geo",
        "Париж — столица Франции. Берлин — столица Германии. "
        "Рим — столица Италии.",
    )
    print(f"проиндексировано чанков: {n}")

    retriever = Retriever(store, llm)
    chunks = await retriever.retrieve("Какая столица Франции?", top_k=2)
    print("найдено:")
    for c in chunks:
        print(f"  [{c.doc_id}#{c.index}] score={c.score:.2f}  {c.text!r}")

    builder = ContextBuilder(store, llm)
    out = await builder.build(ContextInput(
        query="Какая столица Франции?",
        system_prompt="Ты учитель географии.",
        doc_ids=["geo"],
    ))
    print("\n-- system_prompt --")
    print(out.system_prompt)


if __name__ == "__main__":
    asyncio.run(main())
