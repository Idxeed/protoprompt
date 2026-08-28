from __future__ import annotations

from protoprompt.profile.merge import merge
from protoprompt.profile.types import FactOp, ProfileDelta, UserProfile


def _base() -> UserProfile:
    return UserProfile(user_id="u1", version=3, source="old")


def test_merge_applies_fact_ops():
    p = _base()
    p.facts = {"name": "Илья", "old_role": "junior"}
    delta = ProfileDelta(
        fact_ops=[
            FactOp("add", "tech_stack", "python"),
            FactOp("update", "old_role", "senior"),
            FactOp("forget", "old_role"),
        ],
        source="llm",
    )
    out = merge(p, delta, now="2026-01-01T00:00:00")
    assert out.facts == {"name": "Илья", "tech_stack": "python"}
    assert out.version == 4
    assert out.updated_at == "2026-01-01T00:00:00"
    assert out.source == "llm"


def test_merge_forget_missing_key_is_noop():
    p = _base()
    delta = ProfileDelta(fact_ops=[FactOp("forget", "nope")], source="s")
    out = merge(p, delta)
    assert out.facts == {}
    assert out.version == 4  # still counted as a change (op was applied)


def test_merge_traits_newest_wins():
    p = _base()
    p.traits.expertise = "beginner"
    delta = ProfileDelta(traits={"expertise": "expert", "style": ""}, source="s")
    out = merge(p, delta)
    assert out.traits.expertise == "expert"
    assert out.traits.style == ""  # empty value ignored


def test_merge_ignores_unknown_trait_fields():
    p = _base()
    delta = ProfileDelta(traits={"nonexistent": "x"}, source="s")
    out = merge(p, delta)
    assert out.traits == p.traits


def test_merge_topics_replace_and_dedupe():
    p = _base()
    p.preferences.topics = ["ai"]
    delta = ProfileDelta(topics=["rag", "ai", "ai"], source="s")
    out = merge(p, delta)
    assert out.preferences.topics == ["rag", "ai"]


def test_merge_topics_none_keeps_existing():
    p = _base()
    p.preferences.topics = ["ai"]
    delta = ProfileDelta(source="s")
    out = merge(p, delta)
    assert out.preferences.topics == ["ai"]


def test_merge_topics_empty_clears():
    p = _base()
    p.preferences.topics = ["ai", "rag"]
    delta = ProfileDelta(topics=[], source="s")
    out = merge(p, delta)
    assert out.preferences.topics == []


def test_merge_summary_overwritten():
    p = _base()
    p.summary = "old"
    delta = ProfileDelta(summary="new", source="s")
    out = merge(p, delta)
    assert out.summary == "new"


def test_merge_empty_delta_does_not_bump():
    p = _base()
    out = merge(p, ProfileDelta(), now="2026-01-01T00:00:00")
    assert out is p
    assert out.version == 3
    assert out.updated_at == ""
