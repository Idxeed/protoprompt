"""Общие тестовые удвоители для protoprompt_cli."""

from __future__ import annotations


class MockLLM:
    """Асинхронный LLM-дублёр с записью вызовов и детерминированными
    эмбеддингами. ``responses`` — очередь строк, выдаётся по одному
    ответу на каждый ``chat``."""

    def __init__(self, embed_dim: int = 16, responses: list[str] | None = None):
        self.embed_dim = embed_dim
        self.responses = list(responses or [])
        self.chat_calls: list[dict] = []
        self.embed_calls: list[dict] = []

    async def chat(self, messages, model="", **options):
        self.chat_calls.append({"messages": list(messages), "model": model, **options})
        if self.responses:
            return self.responses.pop(0)
        return "mocked response"

    async def embed(self, texts, model=""):
        self.embed_calls.append({"texts": list(texts), "model": model})
        return [_deterministic_embedding(t, self.embed_dim) for t in texts]


def _deterministic_embedding(text: str, dim: int) -> list[float]:
    seed = abs(hash(text)) % (10**6)
    return [((seed >> (i % 30)) & 0xFF) / 255.0 for i in range(dim)]


class FakeReader:
    """Итератор строк ввода; после исчерпания кидает EOFError."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def __call__(self, prompt: str) -> str:
        if not self._lines:
            raise EOFError()
        return self._lines.pop(0)


class FakeWriter:
    """Собирает выводимые строки."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)