"""Text chunkers: split a document into indexable pieces.

All chunkers implement :class:`ChunkerProtocol` (``split(text) ->
list[str]``), so they are interchangeable and trivially testable. The
split happens before embedding; the indexer owns the embed + store step.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from protoprompt.tokens.protocol import TokenCounter
from protoprompt.tokens.regex_counter import RegexTokenCounter


@runtime_checkable
class ChunkerProtocol(Protocol):
    def split(self, text: str) -> list[str]:
        ...


class FixedSizeChunker:
    """Cut text into fixed-length character windows with overlap.

    Args:
        chunk_size: max characters per chunk.
        overlap: characters shared between consecutive chunks (must be
            smaller than ``chunk_size``).
    """

    def __init__(self, chunk_size: int = 512, overlap: int = 64) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if overlap < 0:
            raise ValueError("overlap must be non-negative")
        if overlap >= chunk_size:
            raise ValueError("overlap must be smaller than chunk_size")
        self._size = chunk_size
        self._overlap = overlap

    def split(self, text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []
        if len(text) <= self._size:
            return [text]

        step = self._size - self._overlap
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + self._size, len(text))
            chunks.append(text[start:end])
            if end == len(text):
                break
            start += step
        return chunks


class ParagraphChunker:
    """Split on blank lines, then break over-long paragraphs further."""

    def __init__(self, max_chars: int = 800) -> None:
        self._max = max(1, max_chars)

    def split(self, text: str) -> list[str]:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        chunks: list[str] = []
        for para in paras:
            if len(para) <= self._max:
                chunks.append(para)
            else:
                chunks.extend(self._split_long(para))
        return chunks

    def _split_long(self, para: str) -> list[str]:
        out: list[str] = []
        start = 0
        while start < len(para):
            end = min(start + self._max, len(para))
            out.append(para[start:end])
            if end == len(para):
                break
            start = end
        return out


class TokenChunker:
    """Accumulate words until a token budget is reached, with overlap.

    Token counts come from the supplied :class:`TokenCounter`, so the
    chunks track the same budget signal the context builder uses.
    """

    def __init__(
        self,
        counter: TokenCounter | None = None,
        chunk_tokens: int = 200,
        overlap_words: int = 0,
    ) -> None:
        self._counter: TokenCounter = counter or RegexTokenCounter()
        self._chunk_tokens = max(1, chunk_tokens)
        self._overlap = max(0, overlap_words)
        if self._overlap >= self._chunk_tokens:
            raise ValueError("overlap_words must be smaller than chunk_tokens")

    def split(self, text: str) -> list[str]:
        words = text.split()
        if not words:
            return []

        chunks: list[str] = []
        cur: list[str] = []
        cur_tokens = 0
        for word in words:
            w_tokens = max(1, self._counter.count(word))
            if cur and cur_tokens + w_tokens > self._chunk_tokens:
                chunks.append(" ".join(cur))
                if self._overlap > 0:
                    cur = cur[-self._overlap:]
                    cur_tokens = self._counter.count(" ".join(cur))
                else:
                    cur = []
                    cur_tokens = 0
            cur.append(word)
            cur_tokens += w_tokens

        if cur:
            chunks.append(" ".join(cur))
        return chunks
