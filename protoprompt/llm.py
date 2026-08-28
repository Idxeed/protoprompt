from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ChatClientProtocol(Protocol):
    """Minimal capability required by text-generation consumers."""

    async def chat(self, messages: list[dict], model: str = "", **options: object) -> str:
        ...


@runtime_checkable
class EmbeddingClientProtocol(Protocol):
    """Minimal capability required by retrieval and indexing consumers."""

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        ...


@runtime_checkable
class LLMClientProtocol(ChatClientProtocol, EmbeddingClientProtocol, Protocol):
    """Backward-compatible composite for clients supporting both capabilities."""


class CompositeLLMClient:
    """Pair independent chat and embedding clients behind the legacy protocol.

    This is useful for combinations such as Anthropic chat with local
    sentence-transformer embeddings. Options are forwarded unchanged to the
    selected capability provider.
    """

    def __init__(
        self,
        chat_client: ChatClientProtocol,
        embedding_client: EmbeddingClientProtocol,
    ) -> None:
        self.chat_client = chat_client
        self.embedding_client = embedding_client

    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        **options: object,
    ) -> str:
        return await self.chat_client.chat(messages, model=model, **options)

    async def embed(
        self,
        texts: list[str],
        model: str = "",
    ) -> list[list[float]]:
        return await self.embedding_client.embed(texts, model=model)
