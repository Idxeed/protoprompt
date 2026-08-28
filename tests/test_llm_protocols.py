from __future__ import annotations

import pytest

from protoprompt import (
    ChatClientProtocol,
    CompositeLLMClient,
    EmbeddingClientProtocol,
    LLMClientProtocol,
)
from protoprompt.pipeline import Pipeline
from protoprompt.session.types import Session
from protoprompt.store.memory import InMemStore


class ChatOnly:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, messages, model="", **options):
        self.calls.append({"messages": messages, "model": model, **options})
        return "chat-result"


class EmbedOnly:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def embed(self, texts, model=""):
        self.calls.append({"texts": texts, "model": model})
        return [[1.0, 0.0] for _ in texts]


def test_capability_protocols_are_independent_at_runtime():
    chat = ChatOnly()
    embed = EmbedOnly()

    assert isinstance(chat, ChatClientProtocol)
    assert not isinstance(chat, EmbeddingClientProtocol)
    assert not isinstance(chat, LLMClientProtocol)
    assert isinstance(embed, EmbeddingClientProtocol)
    assert not isinstance(embed, ChatClientProtocol)
    assert not isinstance(embed, LLMClientProtocol)


async def test_composite_client_delegates_capabilities():
    chat = ChatOnly()
    embed = EmbedOnly()
    client = CompositeLLMClient(chat, embed)

    assert isinstance(client, LLMClientProtocol)
    assert await client.chat(
        [{"role": "user", "content": "hello"}],
        model="chat-model",
        temperature=0.2,
    ) == "chat-result"
    assert await client.embed(["one", "two"], model="embed-model") == [
        [1.0, 0.0],
        [1.0, 0.0],
    ]
    assert chat.calls[0]["temperature"] == 0.2
    assert embed.calls[0]["model"] == "embed-model"


async def test_pipeline_accepts_separate_clients():
    chat = ChatOnly()
    embed = EmbedOnly()
    pipeline = Pipeline(
        InMemStore(),
        chat_client=chat,
        embedding_client=embed,
        compress_every_n=1,
    )

    blocks = await pipeline.compress_and_store(
        Session(
            chat_id="split",
            messages=[
                {"role": "user", "content": f"message {index}"}
                for index in range(6)
            ],
        )
    )

    assert blocks
    assert embed.calls


def test_pipeline_requires_both_capabilities_without_composite():
    with pytest.raises(ValueError, match="requires both"):
        Pipeline(InMemStore(), chat_client=ChatOnly())
