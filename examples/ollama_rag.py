"""RAG over local documents with a running Ollama server.

    pip install "protoprompt[ollama]"
    ollama pull llama3.1 nomic-embed-text
    python examples/ollama_rag.py
"""

from __future__ import annotations

import asyncio
import os

from protoprompt import ContextBuilder, ContextInput, InMemStore
from protoprompt.integrations import OllamaClient

DOCS = {
    "kb-1": [
        "Protoprompt собирает контекст из трёх слоёв: RAG, память сессии "
        "и профиль пользователя.",
        "Бюджет токенов распределяется жадно по приоритетам: system, "
        "profile, session, rag.",
    ],
    "kb-2": [
        "Ollama отдаёт эмбеддинги через POST /api/embed, модель по "
        "умолчанию — nomic-embed-text (768 измерений).",
    ],
}


async def main() -> None:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    llm = OllamaClient(host=host)

    store = InMemStore()
    for doc_id, chunks in DOCS.items():
        embeddings = await llm.embed(chunks)
        store.add(doc_id, chunks, embeddings)

    builder = ContextBuilder(store, llm)
    out = await builder.build(ContextInput(
        query="Как protoprompt распределяет токеновый бюджет?",
        system_prompt="Ты краткий технический ассистент.",
        doc_ids=list(DOCS),
        top_k_rag=2,
    ))

    print("--- system prompt ---")
    print(out.system_prompt[:600])

    answer = await llm.chat(
        [{"role": "system", "content": out.system_prompt},
         {"role": "user", "content": "Как protoprompt распределяет бюджет?"}],
        model=os.environ.get("OLLAMA_CHAT_MODEL", "llama3.1"),
    )
    print("--- ответ модели ---")
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
