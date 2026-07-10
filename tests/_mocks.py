"""Shared test doubles for protoprompt test modules.

Lives next to ``conftest.py`` so test files can import it as
``from _mocks import MockLLM``.
"""

from __future__ import annotations


class MockLLM:
    """Minimal async LLM double.

    Records every call so tests can assert on prompt structure. Embeddings
    are deterministic functions of the input text so cosine similarity
    is reproducible across runs.
    """

    def __init__(self, embed_dim: int = 16) -> None:
        self.embed_dim = embed_dim
        self.chat_calls: list[dict] = []
        self.embed_calls: list[dict] = []

    async def chat(self, messages, model="", **options):
        self.chat_calls.append({"messages": list(messages), "model": model, **options})
        return "mocked response"

    async def embed(self, texts, model=""):
        self.embed_calls.append({"texts": list(texts), "model": model})
        return [_deterministic_embedding(t, self.embed_dim) for t in texts]


def _deterministic_embedding(text: str, dim: int) -> list[float]:
    seed = abs(hash(text)) % (10**6)
    return [((seed >> (i % 30)) & 0xFF) / 255.0 for i in range(dim)]
