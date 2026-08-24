"""Local embeddings without any API: sentence-transformers or fastembed
against the persistent SqliteStore. No chat model — RAG assembly only.

    pip install "protoprompt[local]"    # или [fastembed]
    python examples/local_embeddings.py
"""

from __future__ import annotations

import asyncio
import os

from protoprompt import ContextBuilder, ContextInput, SqliteStore

DOCS = {
    "guide": [
        "protoprompt ставится через pip и не тянет обязательных зависимостей.",
        "SqliteStore хранит векторы в обычном файле базы данных SQLite.",
        "Контекст собирается слоями, каждый слой можно отключить флагом include_*.",
    ],
}


async def main() -> None:
    backend = os.environ.get("EMBED_BACKEND", "st")  # "st" | "fastembed"
    if backend == "fastembed":
        from protoprompt.integrations import FastEmbedClient as Embedder
        kwargs = {"model_name": os.environ.get(
            "EMBED_MODEL", "BAAI/bge-small-en-v1.5")}
    else:
        from protoprompt.integrations import SentenceTransformersClient as Embedder
        kwargs = {"model_name": os.environ.get(
            "EMBED_MODEL",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        )}

    embedder = Embedder(**kwargs)
    print(f"embedding backend: {type(embedder).__name__}")

    store = SqliteStore("local_kb.db")
    if store.count() == 0:
        chunks = DOCS["guide"]
        store.add("guide", chunks, await embedder.embed(chunks))

    # билдеру нужен LLM-клиент только ради embed() — локального достаточно
    builder = ContextBuilder(store, embedder)
    out = await builder.build(ContextInput(
        query="Как установить protoprompt?",
        system_prompt="Ты справочный бот.",
        doc_ids=["guide"],
        top_k_rag=2,
    ))
    print(out.system_prompt)
    print("db rows:", store.count())


if __name__ == "__main__":
    asyncio.run(main())
