from __future__ import annotations

import pytest

from protoprompt.profile.source import (
    CompositeProfileSource,
    LLMProfileSource,
    RuleProfileSource,
)
from protoprompt.profile.types import FactOp, ProfileDelta, Signal


class ScriptedLLM:
    """Returns queued responses; records prompts."""

    def __init__(self, *responses: str):
        self._queue = list(responses)
        self.chat_calls: list[dict] = []

    async def chat(self, messages, model="", **options):
        self.chat_calls.append({"messages": list(messages), "model": model, **options})
        return self._queue.pop(0) if self._queue else "{}"

    async def embed(self, texts, model=""):
        return [[0.0] for _ in texts]


def signals(*texts: str) -> list[Signal]:
    return [Signal(user_id="u1", kind="message", role="user", text=t) for t in texts]


# ── LLMProfileSource ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_source_parses_valid_json():
    payload = (
        '{"facts": [{"op": "add", "key": "stack", "value": "python"}],'
        ' "traits": {"expertise": "эксперт"},'
        ' "preferences": {"format": "списки", "language": "ru"},'
        ' "summary": "опытный"}'
    )
    llm = ScriptedLLM(payload)
    source = LLMProfileSource(llm)
    delta = await source.extract("u1", signals("я люблю python и списки"))

    assert delta.source == "llm"
    assert delta.fact_ops == [FactOp("add", "stack", "python")]
    assert delta.traits == {"expertise": "expert"}
    assert delta.preferences == {"format": "bullets", "language": "ru"}
    assert delta.summary == "опытный"


@pytest.mark.asyncio
async def test_llm_source_handles_fenced_json():
    llm = ScriptedLLM('```json\n{"summary": "x"}\n```')
    source = LLMProfileSource(llm)
    delta = await source.extract("u1", signals("hello"))
    assert delta.summary == "x"


@pytest.mark.asyncio
async def test_llm_source_retries_then_succeeds():
    llm = ScriptedLLM("not json", '{"summary": "ok"}')
    source = LLMProfileSource(llm, retries=1)
    delta = await source.extract("u1", signals("hello"))
    assert delta.summary == "ok"
    assert len(llm.chat_calls) == 2


@pytest.mark.asyncio
async def test_llm_source_falls_back_on_persistent_bad_json():
    llm = ScriptedLLM("garbage", "still garbage")
    source = LLMProfileSource(llm, retries=1)
    delta = await source.extract(
        "u1",
        signals("привет, это довольно длинное сообщение про наши планы на завтра"),
    )
    # Fallback (rules) kicks in; source reflects the rules, not the LLM.
    assert delta.source == "rules"
    assert delta.preferences["language"] == "ru"


@pytest.mark.asyncio
async def test_llm_source_empty_transcript():
    llm = ScriptedLLM('{"summary": "x"}')
    source = LLMProfileSource(llm)
    delta = await source.extract("u1", [])
    assert delta == ProfileDelta(source="llm")
    assert llm.chat_calls == []


# ── RuleProfileSource ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rules_verbosity_short():
    source = RuleProfileSource()
    delta = await source.extract("u1", signals("ок", "да"))
    assert delta.traits["verbosity"] == "concise"


@pytest.mark.asyncio
async def test_rules_verbosity_long():
    source = RuleProfileSource()
    delta = await source.extract("u1", signals("подробное сообщение " * 20))
    assert delta.traits["verbosity"] == "detailed"


@pytest.mark.asyncio
async def test_rules_language_and_formality():
    source = RuleProfileSource()
    delta = await source.extract(
        "u1",
        signals("Здравствуйте, пожалуйста, помогите разобраться с этой задачей"),
    )
    assert delta.preferences["language"] == "ru"
    assert delta.traits["formality"] == "formal"


@pytest.mark.asyncio
async def test_rules_empty():
    source = RuleProfileSource()
    delta = await source.extract("u1", [])
    assert delta == ProfileDelta(source="rules")


# ── CompositeProfileSource ───────────────────────────────────────


class _StubSource:
    def __init__(self, delta: ProfileDelta, name: str):
        self._delta = delta
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def extract(self, user_id, signals):
        d = self._delta
        d.source = self._name
        return d


@pytest.mark.asyncio
async def test_composite_first_nonempty():
    a = _StubSource(ProfileDelta(traits={"verbosity": "concise"}), "a")
    b = _StubSource(ProfileDelta(traits={"verbosity": "detailed"}), "b")
    composite = CompositeProfileSource([a, b])
    delta = await composite.extract("u1", signals("x"))
    assert delta.traits["verbosity"] == "concise"


@pytest.mark.asyncio
async def test_composite_last_nonempty():
    a = _StubSource(ProfileDelta(traits={"verbosity": "concise"}), "a")
    b = _StubSource(ProfileDelta(traits={"verbosity": "detailed"}), "b")
    composite = CompositeProfileSource([a, b], conflict="last_nonempty")
    delta = await composite.extract("u1", signals("x"))
    assert delta.traits["verbosity"] == "detailed"


@pytest.mark.asyncio
async def test_composite_concatenates_fact_ops():
    a = _StubSource(ProfileDelta(fact_ops=[FactOp("add", "k1", "v1")]), "a")
    b = _StubSource(ProfileDelta(fact_ops=[FactOp("add", "k2", "v2")]), "b")
    composite = CompositeProfileSource([a, b])
    delta = await composite.extract("u1", signals("x"))
    assert [f.key for f in delta.fact_ops] == ["k1", "k2"]
