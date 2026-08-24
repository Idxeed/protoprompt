from __future__ import annotations

import logging
from typing import Any, Protocol

from protoprompt.llm import LLMClientProtocol
from protoprompt.session.types import CompressedBlock, Session

logger = logging.getLogger(__name__)


class StrategyProtocol(Protocol):
    async def compress(self, session: Session, llm: LLMClientProtocol) -> list[CompressedBlock]:
        ...


class HeuristicStrategy:
    """Pure-Python sliding-window compression.

    Splits a session into three regions:
    - head: first ``head_count`` messages (anchors context);
    - tail: last ``tail_count`` messages (keeps recent intent);
    - middle: keyword-bearing turns longer than ``min_length``.

    The middle slice is the only one that can be empty; head and tail
    collapse into each other when the session is short.
    """

    def __init__(
        self,
        head_count: int = 3,
        tail_count: int = 5,
        min_length: int = 80,
        min_messages: int = 6,
        keywords: tuple[str, ...] = (),
    ) -> None:
        self._head = head_count
        self._tail = tail_count
        self._min_length = min_length
        self._min_messages = min_messages
        self._keywords = keywords or (
            "хочешь", "думаю", "считаю", "предпочитаю", "важно",
            "нравится", "надо", "план", "задача", "цель", "проблема",
            "решение", "итог", "вывод", "хочу", "хотел",
        )

    async def compress(self, session: Session, llm: LLMClientProtocol) -> list[CompressedBlock]:
        msgs = session.messages
        if len(msgs) < self._min_messages:
            return []

        blocks: list[CompressedBlock] = []

        head = msgs[: self._head]
        if head:
            blocks.append(CompressedBlock(
                text="Начало диалога:\n" + "\n".join(
                    f"[{m['role']}]: {m['content']}" for m in head
                ),
                metadata={"segment": "head", "turn_range": f"0-{min(self._head - 1, len(msgs) - 1)}"},
            ))

        tail_start = max(0, len(msgs) - self._tail)
        tail = msgs[tail_start:]
        if tail and tail_start > self._head:
            blocks.append(CompressedBlock(
                text="Последние сообщения:\n" + "\n".join(
                    f"[{m['role']}]: {m['content']}" for m in tail
                ),
                metadata={"segment": "tail", "turn_range": f"{tail_start}-{len(msgs) - 1}"},
            ))

        middle = msgs[self._head : tail_start] if tail_start > self._head else []
        important: list[dict[str, Any]] = []
        for m in middle:
            content = m.get("content", "")
            if len(content) >= self._min_length and self._is_important(content):
                important.append(m)

        if important:
            blocks.append(CompressedBlock(
                text="Ключевые реплики:\n" + "\n".join(
                    f"[{m['role']}]: {m['content']}" for m in important
                ),
                metadata={"segment": "important", "count": len(important)},
            ))

        return blocks

    def _is_important(self, text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in self._keywords)


class LLMSummaryStrategy:
    """LLM-driven summarisation with heuristic fallback.

    Splits the session into rolling windows of ``window_size`` messages
    and asks the LLM to summarise each window in ``language``. On any
    LLM failure (timeout, bad JSON, refused answer) the configured
    fallback strategy is used instead, so a degraded model never blocks
    a chat.
    """

    SUMMARY_PROMPT_RU = (
        "Ты ассистент, который сжимает фрагмент длинного диалога. "
        "Сохрани факты, намерения пользователя, принятые решения и "
        "открытые вопросы. Выкини приветствия, междометия и повторы. "
        "Пиши кратко, на русском, без списков-маркеров.\n\n"
        "Фрагмент диалога:\n{transcript}\n\n"
        "Сжатие:"
    )

    SUMMARY_PROMPT_EN = (
        "You compress a fragment of a long conversation. Preserve facts, "
        "user intents, decisions made, and open questions. Drop "
        "greetings, filler, and repetitions. Write concisely in English, "
        "no bullet markers.\n\n"
        "Fragment:\n{transcript}\n\n"
        "Summary:"
    )

    def __init__(
        self,
        model: str = "",
        window_size: int = 8,
        max_blocks: int = 3,
        target_chars_per_block: int = 600,
        language: str = "ru",
        fallback: StrategyProtocol | None = None,
        temperature: float = 0.2,
        max_tokens: int = 400,
        min_messages: int = 6,
    ) -> None:
        self._model = model
        self._window = max(1, window_size)
        self._max_blocks = max(1, max_blocks)
        self._target_chars = max(80, target_chars_per_block)
        self._language = language
        self._fallback: StrategyProtocol = fallback or HeuristicStrategy()
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._min_messages = min_messages

    async def compress(self, session: Session, llm: LLMClientProtocol) -> list[CompressedBlock]:
        msgs = session.messages
        if len(msgs) < self._min_messages:
            return await self._safe_fallback(session, llm)

        windows = self._split_windows(msgs)
        blocks: list[CompressedBlock] = []
        for idx, window in enumerate(windows[-self._max_blocks:]):
            try:
                summary = await self._summarise_window(llm, window)
            except Exception as exc:
                logger.warning(
                    "LLMSummaryStrategy: window %d failed (%s), falling back",
                    idx, exc,
                )
                return await self._safe_fallback(session, llm)
            if not summary.strip():
                continue
            blocks.append(CompressedBlock(
                text=summary,
                metadata={
                    "segment": "summary",
                    "window_index": idx,
                    "turn_range": f"{window[0]['__i']}-{window[-1]['__i']}",
                },
            ))
        return blocks

    async def _safe_fallback(
        self, session: Session, llm: LLMClientProtocol
    ) -> list[CompressedBlock]:
        try:
            return await self._fallback.compress(session, llm)
        except Exception:
            logger.exception("LLMSummaryStrategy: fallback strategy also failed")
            return []

    async def _summarise_window(
        self, llm: LLMClientProtocol, window: list[dict]
    ) -> str:
        transcript = "\n".join(
            f"[{m['role']}]: {m['content']}" for m in window if "content" in m
        )
        template = self.SUMMARY_PROMPT_RU if self._language == "ru" else self.SUMMARY_PROMPT_EN
        prompt = template.format(transcript=transcript)
        response = await llm.chat(
            [{"role": "user", "content": prompt}],
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        return response.strip()[: self._target_chars * 4]

    def _split_windows(self, msgs: list[dict]) -> list[list[dict]]:
        indexed = [{**m, "__i": i} for i, m in enumerate(msgs)]
        return [indexed[i : i + self._window] for i in range(0, len(indexed), self._window)]
