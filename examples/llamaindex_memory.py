"""Offline LlamaIndex Memory using ProtoPrompt long-term recall."""

from __future__ import annotations

import asyncio

from llama_index.core.memory import Memory

from protoprompt import InMemStore, MemoryScope
from protoprompt.connectivity import MemoryService
from protoprompt.integrations import ProtoPromptMemoryBlock


class DemoEmbeddings:
    async def embed(self, texts, model=""):
        return [[1.0, 0.0] if "contract" in text.lower() else [0.8, 0.2] for text in texts]


async def main() -> None:
    service = MemoryService(
        InMemStore(),
        DemoEmbeddings(),
        MemoryScope(tenant="demo", user="tim", thread="llamaindex"),
    )
    block = ProtoPromptMemoryBlock(service)
    await block.remember(
        "The contract renews on 15 May.",
        memory_id="contract-renewal",
    )
    memory = Memory.from_defaults(
        session_id="thread-a",
        token_limit=1000,
        memory_blocks=[block],
        insert_method="user",
    )

    messages = await memory.aget(input="When does the contract renew?")
    for message in messages:
        print(message.role, message.content)


if __name__ == "__main__":
    asyncio.run(main())
