"""Re-ranking of retrieved chunks.

Cosine top-k is a cheap first pass; a reranker refines the order. The
default :class:`NoOpReranker` keeps the vector order (zero extra cost);
:class:`LLMReranker` asks the model to order the candidates and safely
falls back to the original order on any failure.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from protoprompt.llm import ChatClientProtocol
from protoprompt.rag.types import RetrievedChunk

_RANK_PROMPT_RU = (
    "Вопрос: {query}\n\n"
    "Фрагменты:\n{numbered}\n\n"
    "Отсортируй фрагменты по релевантности к вопросу. "
    "Верни ТОЛЬКО их индексы через запятую, от лучшего к худшему."
)


@runtime_checkable
class RerankerProtocol(Protocol):
    async def rerank(
        self, query: str, chunks: list[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        ...


class NoOpReranker:
    """Keep the vector order unchanged."""

    async def rerank(
        self, query: str, chunks: list[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        return chunks


def _parse_indices(text: str, n: int) -> list[int]:
    seen: set[int] = set()
    order: list[int] = []
    for token in re.findall(r"\d+", text):
        idx = int(token)
        if 0 <= idx < n and idx not in seen:
            seen.add(idx)
            order.append(idx)
    return order


class LLMReranker:
    """Ask a chat model to order candidates by relevance.

    Args:
        llm: chat-capable client.
        model: model name passed to :meth:`ChatClientProtocol.chat`.
        temperature: sampling temperature for the ranking call.
    """

    def __init__(
        self,
        llm: ChatClientProtocol,
        model: str = "",
        temperature: float = 0.0,
    ) -> None:
        self._llm = llm
        self._model = model
        self._temperature = temperature

    async def rerank(
        self, query: str, chunks: list[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        if len(chunks) <= 1:
            return chunks

        numbered = "\n".join(f"{i}. {c.text[:200]}" for i, c in enumerate(chunks))
        prompt = _RANK_PROMPT_RU.format(query=query, numbered=numbered)

        try:
            response = await self._llm.chat(
                [{"role": "user", "content": prompt}],
                model=self._model,
                temperature=self._temperature,
                max_tokens=100,
            )
        except Exception:
            return chunks

        order = _parse_indices(response, len(chunks))
        if not order:
            return chunks

        # Re-order, appending any chunk the model omitted (keeps them all).
        reordered = [chunks[i] for i in order]
        missing = [c for i, c in enumerate(chunks) if i not in order]
        return reordered + missing
