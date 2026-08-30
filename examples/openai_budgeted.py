"""Budgeted context assembly + build_messages() with the OpenAI SDK.

    pip install "protoprompt[openai,tiktoken]"
    export OPENAI_API_KEY=sk-...
    python examples/openai_budgeted.py
"""

from __future__ import annotations

import asyncio
import os

from protoprompt import (
    ContextInput,
    SqliteStore,
    TokenBudgetedContextBuilder,
)
from protoprompt.integrations import OpenAIClient

KNOWLEDGE = [
    "Отчёты за Q3 показали рост выручки на 12% при сохранении маржи.",
    "План Q4: фокус на enterprise-сегменте и сокращение цикла сделки.",
    "Команда поддержки сократила среднее время ответа до 40 минут.",
]


async def main() -> None:
    llm = OpenAIClient(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL"),  # опционально: шлюз/vLLM
        chat_model="gpt-4o-mini",
        embed_model="text-embedding-3-small",
    )

    store = SqliteStore("knowledge.db")  # персистентно между запусками
    if store.count() == 0:
        embeddings = await llm.embed(KNOWLEDGE)
        store.add("reports", KNOWLEDGE, embeddings)

    builder = TokenBudgetedContextBuilder(
        store, llm,
        max_tokens=2000,  # компактный бюджет: видно, как режутся блоки
        output_reserve=400,  # оставляем место для ответа модели
        priorities=("system", "session", "rag", "profile"),
    )

    history = [
        {"role": "user", "content": "Мы обсуждали план на четвёртый квартал."},
        {"role": "assistant", "content": "Да, основной фокус — enterprise."},
    ] * 20  # длинная история, чтобы обрезка была заметна

    messages = await builder.build_messages(
        ContextInput(query="Какие итоги третьего квартала?", doc_ids=["reports"]),
        history=history,
        user_message="Сводка по итогам Q3 и приоритетам Q4?",
    )

    report = builder.last_report
    assert report is not None
    print(f"бюджет {report.budget} | used {report.used_tokens}"
          f" | history kept {report.history_kept}/{len(history)}")
    print("dropped:", report.dropped_blocks or "—")

    answer = await llm.chat(messages)
    print("--- ответ ---")
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
