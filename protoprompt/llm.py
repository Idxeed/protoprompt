from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClientProtocol(Protocol):
    async def chat(self, messages: list[dict], model: str = "", **options: object) -> str:
        ...

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        ...
