from __future__ import annotations

import pytest

from protoprompt.integrations.pydantic_ai import PydanticAIMemoryAdapter


class _MemoryService:
    def __init__(self) -> None:
        self.queries = []
        self.remembered = []

    async def search(self, query, *, top_k, score_threshold=None):
        self.queries.append((query, top_k, score_threshold))
        return [
            {
                "memory_id": "contract-renewal",
                "text": "The contract renews on 15 May.",
                "score": 0.93,
            }
        ]

    async def remember(self, text, *, memory_id=None, metadata=None):
        self.remembered.append((text, memory_id, metadata))
        return {"memory_id": memory_id or "generated", "stored": True}


@pytest.mark.asyncio
async def test_pydantic_ai_capability_runs_inside_real_agent():
    pydantic_ai = pytest.importorskip("pydantic_ai")
    from pydantic_ai.models.function import FunctionModel

    service = _MemoryService()
    received = []

    def model_function(messages, info):
        received[:] = messages
        return pydantic_ai.ModelResponse(
            parts=[pydantic_ai.TextPart(content="15 May")]
        )

    adapter = PydanticAIMemoryAdapter(service, top_k=3, score_threshold=0.5)
    agent = pydantic_ai.Agent(
        FunctionModel(model_function),
        capabilities=[adapter.capability()],
    )
    result = await agent.run("When does the contract renew?")

    assert result.output == "15 May"
    assert service.queries == [("When does the contract renew?", 3, 0.5)]
    request = received[-1]
    injected = request.parts[0]
    assert injected.part_kind == "user-prompt"
    assert "<protoprompt_memory>" in injected.content
    assert "15 May" in injected.content
    assert request.parts[1].content == "When does the contract renew?"


@pytest.mark.asyncio
async def test_pydantic_adapter_does_not_mutate_history_and_skips_empty_hits():
    pydantic_ai = pytest.importorskip("pydantic_ai")
    service = _MemoryService()
    adapter = PydanticAIMemoryAdapter(service)
    original = pydantic_ai.ModelRequest(
        parts=[pydantic_ai.UserPromptPart(content="question")]
    )

    output = await adapter.process_history([original])

    assert output[0] is not original
    assert len(original.parts) == 1
    assert len(output[0].parts) == 2


@pytest.mark.asyncio
async def test_llamaindex_block_runs_inside_real_memory():
    pytest.importorskip("llama_index.core")
    from llama_index.core.memory import Memory
    from protoprompt.integrations.llamaindex import ProtoPromptMemoryBlock

    service = _MemoryService()
    block = ProtoPromptMemoryBlock(service, top_k=2)
    memory = Memory.from_defaults(
        session_id="thread-a",
        token_limit=1000,
        memory_blocks=[block],
        insert_method="user",
    )

    history = await memory.aget(input="When does the contract renew?")

    assert service.queries == [("When does the contract renew?", 2, None)]
    assert any("15 May" in (message.content or "") for message in history)


@pytest.mark.asyncio
async def test_llamaindex_writes_are_explicit_by_default_and_opt_in_when_requested():
    pytest.importorskip("llama_index.core")
    from llama_index.core.llms import ChatMessage
    from protoprompt.integrations.llamaindex import ProtoPromptMemoryBlock

    service = _MemoryService()
    message = ChatMessage(role="user", content="Remember 15 May")
    safe_block = ProtoPromptMemoryBlock(service)
    await safe_block.aput([message], from_short_term_memory=True)
    assert service.remembered == []

    automatic = ProtoPromptMemoryBlock(service, auto_remember=True)
    await automatic.aput([message], from_short_term_memory=True)
    assert service.remembered == [("Remember 15 May", None, None)]
    assert await automatic.remember("Confirmed", memory_id="explicit") == {
        "memory_id": "explicit",
        "stored": True,
    }
