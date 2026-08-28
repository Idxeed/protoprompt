from __future__ import annotations

import pytest

from protoprompt.profile.codec import (
    STRICT_PROFILE,
    CodecProfile,
    coerce_profile,
    coerce_topics,
    normalize_enum,
    parse_profile_json,
    slugify,
)
from protoprompt.profile.types import FactOp, ProfileDelta


# ── parse_profile_json ───────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"summary": "x"}', {"summary": "x"}),
        ('```json\n{"summary": "x"}\n```', {"summary": "x"}),
        ('Вот результат: {"summary": "x"} спасибо', {"summary": "x"}),
        ('{"a": 1, "b": [1, 2]}', {"a": 1, "b": [1, 2]}),
    ],
)
def test_parse_extracts_object(text, expected):
    assert parse_profile_json(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json at all",
        "```json\nне json\n```",
        "[1, 2, 3]",           # array is not an object
        "null",
    ],
)
def test_parse_returns_empty_on_bad_input(text):
    assert parse_profile_json(text) == {}


# ── normalize_enum ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,field,expected",
    [
        ("beginner", "expertise", "beginner"),
        ("новичок", "expertise", "beginner"),
        ("Эксперт", "expertise", "expert"),
        ("concise", "style", "concise"),
        ("развёрнуто", "style", "detailed"),
        ("списки", "format", "bullets"),
        ("NARRATIVE", "format", "narrative"),
        ("", "expertise", ""),
        (None, "expertise", ""),
        ("непонятное", "expertise", ""),  # unmappable -> empty
        ("freeform", "language", "freeform"),  # non-enum field passes through
    ],
)
def test_normalize_enum(value, field, expected):
    assert normalize_enum(value, field) == expected


# ── coerce_topics ────────────────────────────────────────────────


def test_coerce_topics_list_dedupes():
    assert coerce_topics(["ai", "rag", "ai", "", "llm"]) == ["ai", "rag", "llm"]


def test_coerce_topics_string_splits():
    assert coerce_topics("ai, rag, llm") == ["ai", "rag", "llm"]


def test_coerce_topics_invalid():
    assert coerce_topics(None) == []
    assert coerce_topics(123) == []
    assert coerce_topics([1, 2]) == ["1", "2"]


# ── coerce_profile ───────────────────────────────────────────────


def test_coerce_full_payload():
    raw = {
        "facts": [
            {"op": "add", "key": "tech_stack", "value": "python"},
            {"op": "update", "key": "role", "value": "backend"},
            {"op": "forget", "key": "old_role"},
        ],
        "traits": {"expertise": "эксперт", "style": "развёрнуто"},
        "preferences": {"format": "списки", "language": "ru", "topics": ["ai", "rag"]},
        "summary": "Опытный бэкендер",
    }
    delta = coerce_profile(raw)
    assert delta.fact_ops == [
        FactOp("add", "tech_stack", "python"),
        FactOp("update", "role", "backend"),
        FactOp("forget", "old_role", ""),
    ]
    assert delta.traits == {"expertise": "expert", "style": "detailed"}
    assert delta.preferences == {"format": "bullets", "language": "ru"}
    assert delta.topics == ["ai", "rag"]
    assert delta.summary == "Опытный бэкендер"


def test_coerce_drops_invalid_fact_ops():
    raw = {
        "facts": [
            {"op": "create", "key": "x", "value": "y"},   # bad op
            {"op": "add", "key": "", "value": "y"},       # empty key
            {"op": "add", "key": "ok", "value": "v"},     # valid
            "not-a-dict",                                  # junk
        ],
    }
    delta = coerce_profile(raw)
    assert delta.fact_ops == [FactOp("add", "ok", "v")]


def test_coerce_ignores_unmappable_traits():
    raw = {"traits": {"expertise": "гений", "style": "нейтрально"}}
    delta = coerce_profile(raw)
    assert delta.traits == {}  # гений not mappable, нейтрально maps to formality not style


def test_coerce_non_dict_input():
    assert coerce_profile(["x"]) == ProfileDelta()
    assert coerce_profile({}) == ProfileDelta()


def test_coerce_missing_summary():
    delta = coerce_profile({"traits": {}})
    assert delta.summary == ""


# ── slugify ──────────────────────────────────────────────────────


def test_slugify_transliterates_cyrillic():
    assert slugify("Язык Программирования") == "yazyk_programmirovaniya"
    assert slugify("база_данных") == "baza_dannykh"


def test_slugify_ascii_collapse_and_idempotent():
    assert slugify("Tech Stack") == "tech_stack"
    assert slugify("tech_stack") == "tech_stack"
    assert slugify("a-b/c") == "a_b_c"
    assert slugify("  ") == ""


# ── CodecProfile ─────────────────────────────────────────────────


def test_default_profile_slugifies_fact_keys():
    raw = {
        "facts": [{"op": "add", "key": "Язык Программирования", "value": "Python"}],
    }
    delta = coerce_profile(raw)
    assert delta.fact_ops[0].key == "yazyk_programmirovaniya"


def test_strict_profile_keeps_keys_and_skips_translation():
    raw = {
        "facts": [{"op": "add", "key": "tech_stack", "value": "Python"}],
        "traits": {"expertise": "эксперт"},
    }
    delta = coerce_profile(raw, profile=STRICT_PROFILE)
    assert delta.fact_ops[0].key == "tech_stack"
    assert delta.traits == {}  # RU label not translated under strict profile


def test_fact_key_map_renames_before_slugify():
    profile = CodecProfile(
        fact_key_map={"язык_программирования": "language"}
    )
    raw = {
        "facts": [{"op": "add", "key": "язык_программирования", "value": "Python"}],
    }
    delta = coerce_profile(raw, profile=profile)
    assert delta.fact_ops[0].key == "language"


def test_custom_enum_map():
    profile = CodecProfile(enum_map={"мастер": "expert"})
    raw = {"traits": {"expertise": "мастер"}}
    delta = coerce_profile(raw, profile=profile)
    assert delta.traits == {"expertise": "expert"}
