"""Compare plain Agents history with a protoprompt budgeted session view."""

from __future__ import annotations

import asyncio
import hashlib

from protoprompt import InMemStore, MemoryScope, TokenBudgetedContextBuilder
from protoprompt.integrations import ProtoPromptSession
from protoprompt.rag import DocumentIndexer


class DemoEmbeddings:
    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [
            [byte / 255.0 for byte in hashlib.sha256(text.encode()).digest()]
            for text in texts
        ]


async def main() -> None:
    store = InMemStore()
    embeddings = DemoEmbeddings()
    scope = MemoryScope(tenant="demo", user="alice", thread="legal-chat")
    await DocumentIndexer(store, embeddings, scope=scope).index(
        "contract",
        "The supplier contract renews on 15 May unless cancelled 30 days earlier.",
    )

    session = ProtoPromptSession("legal-chat")
    history = [
        {"role": "user", "content": f"Routine discussion turn {index}"}
        for index in range(12)
    ]
    await session.add_items(history)
    new_input = [{"role": "user", "content": "When does the contract renew?"}]

    plain_view = await session.get_items() + new_input
    builder = TokenBudgetedContextBuilder(
        store,
        embeddings,
        max_tokens=80,
        scope=scope,
    )
    callback = session.input_callback(
        builder,
        system_prompt="Answer only from recalled contract facts.",
    )
    memory_view = await callback(await session.get_items(), new_input)

    print("Plain session items:", len(plain_view))
    print("Protoprompt items:", len(memory_view))
    print("Recalled system context:\n", memory_view[0]["content"])


if __name__ == "__main__":
    asyncio.run(main())
