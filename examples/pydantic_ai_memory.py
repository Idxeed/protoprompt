"""Offline PydanticAI agent recalling a scoped ProtoPrompt memory."""

from __future__ import annotations

import asyncio

from pydantic_ai import Agent, ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from protoprompt import InMemStore, MemoryScope
from protoprompt.connectivity import MemoryService
from protoprompt.integrations import create_pydantic_ai_capability


class DemoEmbeddings:
    async def embed(self, texts, model=""):
        return [[1.0, 0.0] if "contract" in text.lower() else [0.8, 0.2] for text in texts]


async def main() -> None:
    service = MemoryService(
        InMemStore(),
        DemoEmbeddings(),
        MemoryScope(tenant="demo", user="tim", thread="pydantic"),
    )
    await service.remember(
        "The contract renews on 15 May.",
        memory_id="contract-renewal",
    )

    def local_model(messages, info):
        recalled = next(
            part.content
            for message in messages
            for part in message.parts
            if getattr(part, "part_kind", "") == "user-prompt"
            and isinstance(part.content, str)
            and "<protoprompt_memory>" in part.content
        )
        assert "15 May" in recalled
        return ModelResponse(parts=[TextPart(content="It renews on 15 May.")])

    agent = Agent(
        FunctionModel(local_model),
        capabilities=[create_pydantic_ai_capability(service)],
    )
    result = await agent.run("When does the contract renew?")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
