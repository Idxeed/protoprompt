"""Parsing and coercion of raw LLM profile output into a ``ProfileDelta``.

The LLM is asked for strict JSON, but real models fence it in markdown,
embed it in prose, return Russian enum labels, or produce ``null``/wrong
types. This module is the defensive layer: it extracts the first JSON
object, normalizes enums to the canonical values from
:mod:`protoprompt.profile.schema`, and drops anything unrecognized. It
never raises on bad input — an empty delta is a valid outcome.

Normalization is configured by :class:`CodecProfile`, so different models
(local 8B vs hosted GPT-style) can share one pipeline with different
tolerances — see :data:`DEFAULT_PROFILE` and :data:`STRICT_PROFILE`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from protoprompt.profile.schema import ENUM_VALUES
from protoprompt.profile.types import FactOp, ProfileDelta

#: Russian (and other common) labels → canonical enum values.
_ENUM_MAP: dict[str, str] = {
    # expertise
    "новичок": "beginner",
    "начинающий": "beginner",
    "средний": "intermediate",
    "эксперт": "expert",
    # verbosity / style
    "кратко": "concise",
    "сжато": "concise",
    "лаконично": "concise",
    "сбалансированно": "balanced",
    "развёрнуто": "detailed",
    "развернуто": "detailed",
    "подробно": "detailed",
    # formality
    "неформально": "casual",
    "нейтрально": "neutral",
    "формально": "formal",
    # format
    "списки": "bullets",
    "маркеры": "bullets",
    "нарратив": "narrative",
    "код": "code_heavy",
    "смешанно": "mixed",
}

#: Cyrillic → Latin transliteration used by :func:`slugify`.
_TRANSLIT: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_FACT_OPS = ("add", "update", "forget")


@dataclass
class CodecProfile:
    """How to normalize raw LLM output into a ``ProfileDelta``.

    Args:
        slugify_keys: normalize fact keys to stable ASCII slugs
            (``"Язык программирования"`` → ``"yazyk_programmirovaniya"``).
        enum_map: label → canonical enum translation (default: Russian).
            Pass ``{}`` for models that already emit canonical English.
        fact_key_map: optional slug/raw-key → canonical fact key renaming
            (keys are matched lowercase), e.g. ``{"язык_программирования":
            "language"}``.
    """

    slugify_keys: bool = True
    enum_map: dict[str, str] = field(default_factory=lambda: dict(_ENUM_MAP))
    fact_key_map: dict[str, str] = field(default_factory=dict)


#: Tolerant default for local / small models (8B and friends).
DEFAULT_PROFILE = CodecProfile()

#: For models that already emit canonical English keys and enum values.
STRICT_PROFILE = CodecProfile(slugify_keys=False, enum_map={})


def slugify(text: str) -> str:
    """Turn an arbitrary fact key into a stable ASCII slug.

    Lowercases, transliterates Cyrillic to Latin, and replaces anything
    else with ``_`` (collapsed). ``"Язык Программирования"`` →
    ``"yazyk_programmirovaniya"``. Idempotent and deterministic, so the
    same fact keeps the same key across calls.
    """
    out: list[str] = []
    for ch in text.strip().lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and (ch.isalnum()):
            out.append(ch)
        else:
            out.append("_")
    return re.sub(r"_+", "_", "".join(out)).strip("_")


def parse_profile_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from arbitrary LLM text.

    Handles markdown fences and surrounding prose. Returns ``{}`` when no
    valid JSON object is present or parsing fails.
    """
    if not text:
        return {}
    candidate = text.strip()

    fence = _FENCE_RE.search(candidate)
    if fence:
        candidate = fence.group(1).strip()
    else:
        obj = _OBJECT_RE.search(candidate)
        if obj:
            candidate = obj.group(0)

    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def normalize_enum(
    value: Any,
    field: str,
    *,
    profile: CodecProfile = DEFAULT_PROFILE,
) -> str:
    """Map an arbitrary label to its canonical enum value for ``field``.

    Returns ``""`` when the value is empty or cannot be mapped to one of
    the schema's allowed values for ``field``.
    """
    if value is None:
        return ""
    v = str(value).strip().lower()
    if not v:
        return ""
    allowed = ENUM_VALUES.get(field, [])
    if not allowed:
        return str(value).strip()
    if v in allowed:
        return v
    mapped = profile.enum_map.get(v)
    if mapped is not None and mapped in allowed:
        return mapped
    return ""


def coerce_topics(topics: Any) -> list[str]:
    """Coerce ``topics`` into a de-duplicated list of non-empty strings."""
    if topics is None:
        return []
    if isinstance(topics, str):
        items = [t.strip() for t in topics.split(",") if t.strip()]
    elif isinstance(topics, (list, tuple, set)):
        items = [str(t).strip() for t in topics if str(t).strip()]
    else:
        return []

    seen: set[str] = set()
    out: list[str] = []
    for t in items:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _normalize_fact_key(raw: str, profile: CodecProfile) -> str:
    canonical = profile.fact_key_map.get(raw.lower())
    if canonical:
        return canonical
    return slugify(raw) if profile.slugify_keys else raw


def _coerce_fact_ops(raw: Any, *, profile: CodecProfile) -> list[FactOp]:
    if not isinstance(raw, list):
        return []
    ops: list[FactOp] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        op = str(entry.get("op", "")).strip().lower()
        if op not in _FACT_OPS:
            continue
        key = str(entry.get("key", "")).strip()
        if not key:
            continue
        value = "" if op == "forget" else str(entry.get("value", ""))
        ops.append(FactOp(op=op, key=_normalize_fact_key(key, profile), value=value))
    return ops


def coerce_profile(
    raw: dict[str, Any],
    *,
    profile: CodecProfile = DEFAULT_PROFILE,
) -> ProfileDelta:
    """Turn a parsed JSON object into a validated :class:`ProfileDelta`."""
    if not isinstance(raw, dict):
        return ProfileDelta()

    delta = ProfileDelta()
    delta.fact_ops = _coerce_fact_ops(raw.get("facts"), profile=profile)

    traits = raw.get("traits")
    if isinstance(traits, dict):
        for name, value in traits.items():
            normalized = normalize_enum(value, name, profile=profile)
            if normalized:
                delta.traits[name] = normalized

    prefs = raw.get("preferences")
    if isinstance(prefs, dict):
        for name in ("format", "language"):
            if name not in prefs:
                continue
            normalized = normalize_enum(prefs[name], name, profile=profile)
            if normalized:
                delta.preferences[name] = normalized
        if "topics" in prefs:
            delta.topics = coerce_topics(prefs["topics"])

    summary = raw.get("summary")
    if isinstance(summary, str):
        delta.summary = summary.strip()

    return delta
