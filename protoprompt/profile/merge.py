"""Incremental merge of a :class:`ProfileDelta` into a :class:`UserProfile`.

Semantics (see ``ROADMAP.md``, decisions D2/D3):

- ``fact_ops`` are applied in order: ``add``/``update`` set a fact,
  ``forget`` removes it (missing key is a no-op);
- scalar ``traits``/``preferences`` follow newest-wins: a non-empty delta
  value replaces the current one, an empty value is ignored;
- ``topics`` is *replace* semantics: ``None`` keeps the current list, a
  list (including ``[]``) replaces it (deduplicated);
- ``summary`` is overwritten by a non-empty delta summary;
- ``version`` increments and ``updated_at``/``source`` are set only when
  the delta actually changes something.
"""

from __future__ import annotations

from dataclasses import replace

from protoprompt.profile.types import ProfileDelta, UserProfile

_TRAIT_FIELDS = ("style", "expertise", "verbosity", "formality")
_PREF_FIELDS = ("format", "language")


def _is_empty(delta: ProfileDelta) -> bool:
    return (
        not delta.fact_ops
        and not delta.traits
        and not delta.preferences
        and delta.topics is None
        and not delta.summary
    )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def merge(
    profile: UserProfile,
    delta: ProfileDelta,
    *,
    now: str = "",
) -> UserProfile:
    """Return a new profile with ``delta`` folded in.

    ``now`` is the ISO-8601 timestamp recorded into ``updated_at``. An
    empty delta returns ``profile`` unchanged (no version bump).
    """
    if _is_empty(delta):
        return profile

    facts = dict(profile.facts)
    for op in delta.fact_ops:
        if op.op == "forget":
            facts.pop(op.key, None)
        else:
            facts[op.key] = op.value

    traits = replace(profile.traits)
    for name, value in delta.traits.items():
        if name in _TRAIT_FIELDS and value:
            setattr(traits, name, value)

    prefs = replace(profile.preferences)
    for name, value in delta.preferences.items():
        if name in _PREF_FIELDS and value:
            setattr(prefs, name, value)
    if delta.topics is not None:
        prefs.topics = _dedupe(delta.topics)

    summary = delta.summary if delta.summary else profile.summary

    return UserProfile(
        user_id=profile.user_id,
        traits=traits,
        preferences=prefs,
        facts=facts,
        summary=summary,
        updated_at=now,
        version=profile.version + 1,
        source=delta.source or profile.source,
    )
