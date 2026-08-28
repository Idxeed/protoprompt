"""Profile sources: pluggable extractors of durable facts from signals.

A source implements :class:`ProfileProtocol` and turns a batch of
:class:`~protoprompt.profile.types.Signal` into a
:class:`~protoprompt.profile.types.ProfileDelta`. Built-ins:

- :class:`LLMProfileSource` — asks the model for strict JSON, retries
  once on bad output, then falls back (rules or empty);
- :class:`RuleProfileSource` — deterministic, zero-LLM heuristics;
- :class:`CompositeProfileSource` — runs several sources and folds their
  deltas together.
"""

from __future__ import annotations

import logging
from statistics import mean
from typing import Any, Protocol

from protoprompt.llm import ChatClientProtocol
from protoprompt.profile.codec import (
    DEFAULT_PROFILE,
    CodecProfile,
    coerce_profile,
    parse_profile_json,
)
from protoprompt.profile.types import ProfileDelta, Signal

logger = logging.getLogger(__name__)


class ProfileProtocol(Protocol):
    async def extract(self, user_id: str, signals: list[Signal]) -> ProfileDelta:
        ...


_PROMPT_RU = (
    "Проанализируй сообщения пользователя и выдели ДОЛГОВЕЧНЫЕ факты "
    "и предпочтения (не пересказывай текущую задачу).\n"
    "Верни строго один JSON-объект без markdown-блоков и пояснений:\n"
    '{\n'
    '  "facts": [{"op": "add|update|forget", "key": "короткий_слаг", '
    '"value": "..."}],\n'
    '  "traits": {"style": "concise|balanced|detailed", '
    '"expertise": "beginner|intermediate|expert", '
    '"verbosity": "concise|balanced|detailed", '
    '"formality": "casual|neutral|formal"},\n'
    '  "preferences": {"format": "bullets|narrative|code_heavy|mixed", '
    '"language": "ru|en", "topics": ["..."]},\n'
    '  "summary": "короткая выжимка"\n'
    '}\n'
    "Правила: op только add/update/forget; key — короткий слаг без пробелов; "
    "значения traits/preferences строго из перечисленных enum; "
    "fact_ops — только стабильные факты (имя, роль, стек, предпочтения).\n\n"
    "Сообщения:\n{transcript}"
)

_PROMPT_EN = (
    "Analyze the user's messages and extract DURABLE facts and "
    "preferences (do not summarize the current task).\n"
    "Return strictly one JSON object, no markdown fences or prose:\n"
    '{\n'
    '  "facts": [{"op": "add|update|forget", "key": "short_slug", '
    '"value": "..."}],\n'
    '  "traits": {"style": "concise|balanced|detailed", '
    '"expertise": "beginner|intermediate|expert", '
    '"verbosity": "concise|balanced|detailed", '
    '"formality": "casual|neutral|formal"},\n'
    '  "preferences": {"format": "bullets|narrative|code_heavy|mixed", '
    '"language": "ru|en", "topics": ["..."]},\n'
    '  "summary": "short distillation"\n'
    '}\n'
    "Rules: op must be add/update/forget; key is a short slug without spaces; "
    "traits/preferences must use the listed enum values; "
    "facts must be stable facts (name, role, stack, preferences).\n\n"
    "Messages:\n{transcript}"
)

_RETRY_PROMPT = (
    "Ты вернул невалидный JSON. Верни СТРОГО один JSON-объект по схеме, "
    "без markdown-блоков и пояснений."
)

_CYRILLIC = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")
_CASUAL_MARKERS = ("привет", "здорово", "ок", "ладно", "хай")
_FORMAL_MARKERS = ("здравствуйте", "пожалуйста", "благодарю", "уважаемый")


def _transcript(signals: list[Signal]) -> str:
    lines: list[str] = []
    for s in signals:
        if not s.text:
            continue
        if s.kind == "feedback":
            lines.append(f"[feedback]: {s.text}")
        else:
            role = s.role or "user"
            lines.append(f"[{role}]: {s.text}")
    return "\n".join(lines)


def _merge_deltas(deltas: list[ProfileDelta], *, conflict: str) -> ProfileDelta:
    """Fold several deltas into one.

    ``conflict`` selects the winner for a non-empty scalar field:
    ``"first_nonempty"`` (default) or ``"last_nonempty"``. ``fact_ops`` are
    always concatenated in source order; ``topics`` follows the same
    conflict rule over non-``None`` lists.
    """
    merged = ProfileDelta()
    merged.fact_ops = [op for d in deltas for op in d.fact_ops]

    for field in ("traits", "preferences"):
        target: dict[str, str] = getattr(merged, field)
        for d in deltas:
            for key, value in getattr(d, field).items():
                if conflict == "last_nonempty":
                    if value:
                        target[key] = value
                elif key not in target:
                    target[key] = value

    for d in deltas:
        if d.topics is not None:
            if conflict == "last_nonempty" or merged.topics is None:
                merged.topics = list(d.topics)

    if conflict == "last_nonempty":
        for d in deltas:
            if d.summary:
                merged.summary = d.summary
    else:
        for d in deltas:
            if d.summary and not merged.summary:
                merged.summary = d.summary

    merged.source = ",".join(d.source for d in deltas if d.source)
    return merged


class LLMProfileSource:
    """LLM-driven profile extractor with one retry and a safe fallback.

    Args:
        llm: chat-capable client.
        language: prompt language (``"ru"`` or ``"en"``).
        max_signals: cap on the number of signals fed to the model.
        model: model name passed to :meth:`ChatClientProtocol.chat`.
        retries: extra attempts after the first (default 1).
        fallback: a :class:`ProfileProtocol` used when the LLM cannot
            produce valid JSON; defaults to :class:`RuleProfileSource`.
    """

    def __init__(
        self,
        llm: ChatClientProtocol,
        *,
        language: str = "ru",
        max_signals: int = 20,
        model: str = "",
        retries: int = 1,
        fallback: ProfileProtocol | None = None,
        codec: CodecProfile | None = None,
    ) -> None:
        self._llm = llm
        self._language = language
        self._max_signals = max(1, max_signals)
        self._model = model
        self._retries = max(0, retries)
        self._fallback = fallback or RuleProfileSource()
        self._codec = codec or DEFAULT_PROFILE

    @property
    def name(self) -> str:
        return "llm"

    async def extract(self, user_id: str, signals: list[Signal]) -> ProfileDelta:
        transcript = _transcript(signals[-self._max_signals:])
        if not transcript:
            return ProfileDelta(source=self.name)

        template = _PROMPT_RU if self._language == "ru" else _PROMPT_EN
        prompt = template.replace("{transcript}", transcript)

        for attempt in range(self._retries + 1):
            try:
                response = await self._llm.chat(
                    [{"role": "user", "content": prompt}],
                    model=self._model,
                    temperature=0.3,
                    max_tokens=600,
                )
            except Exception as exc:
                logger.warning("LLMProfileSource: chat failed (%s)", exc)
                break

            raw = parse_profile_json(response)
            if raw:
                delta = coerce_profile(raw, profile=self._codec)
                delta.source = self.name
                return delta
            prompt = _RETRY_PROMPT + "\n\n" + prompt

        logger.warning(
            "LLMProfileSource: no valid JSON after %d attempts; falling back",
            self._retries + 1,
        )
        return await self._safe_fallback(user_id, signals)

    async def _safe_fallback(
        self, user_id: str, signals: list[Signal]
    ) -> ProfileDelta:
        try:
            return await self._fallback.extract(user_id, signals)
        except Exception:
            logger.exception("LLMProfileSource: fallback source failed")
            return ProfileDelta(source=self.name)


class RuleProfileSource:
    """Deterministic heuristics, no LLM.

    Infers verbosity from average message length and language from the
    alphabet of the input. Cheap, reproducible, and always safe to call.
    """

    @property
    def name(self) -> str:
        return "rules"

    async def extract(self, user_id: str, signals: list[Signal]) -> ProfileDelta:
        texts = [s.text for s in signals if s.text.strip()]
        if not texts:
            return ProfileDelta(source=self.name)

        delta = ProfileDelta(source=self.name)

        avg_len = mean(len(t) for t in texts)
        if avg_len < 40:
            delta.traits["verbosity"] = "concise"
        elif avg_len < 160:
            delta.traits["verbosity"] = "balanced"
        else:
            delta.traits["verbosity"] = "detailed"

        joined = " ".join(texts).lower()
        if any(ch in _CYRILLIC for ch in joined):
            delta.preferences["language"] = "ru"
        else:
            delta.preferences["language"] = "en"

        if any(m in joined for m in _CASUAL_MARKERS):
            delta.traits["formality"] = "casual"
        elif any(m in joined for m in _FORMAL_MARKERS):
            delta.traits["formality"] = "formal"

        return delta


class CompositeProfileSource:
    """Run several sources and fold their deltas together.

    Args:
        sources: ordered list of :class:`ProfileProtocol` instances.
        conflict: ``"first_nonempty"`` (default) or ``"last_nonempty"`` —
            how overlapping scalar fields are resolved.
    """

    def __init__(
        self,
        sources: list[ProfileProtocol],
        *,
        conflict: str = "first_nonempty",
    ) -> None:
        if conflict not in ("first_nonempty", "last_nonempty"):
            raise ValueError(f"unknown conflict policy: {conflict}")
        self._sources = sources
        self._conflict = conflict

    @property
    def name(self) -> str:
        return "composite"

    async def extract(self, user_id: str, signals: list[Signal]) -> ProfileDelta:
        deltas: list[ProfileDelta] = []
        for source in self._sources:
            try:
                deltas.append(await source.extract(user_id, signals))
            except Exception:
                logger.exception("CompositeProfileSource: source failed")
        return _merge_deltas(deltas, conflict=self._conflict)
