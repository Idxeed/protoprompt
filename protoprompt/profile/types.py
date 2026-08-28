"""Profile data model: durable user facts, typed traits and preferences.

The model is split into three buckets:

- ``Traits`` / ``Preferences`` — schema-backed, closed-world fields with
  canonical enum values (see :mod:`protoprompt.profile.schema`);
- ``facts`` — an open-world ``dict[str, str]`` of named durable facts
  (``name``, ``role``, ``tech_stack``, ...) mutated via :class:`FactOp`;
- ``summary`` — a free-form distillation, regenerated/overwritten on merge.

A profile is *incremental*: sources emit a :class:`ProfileDelta`, which a
merger folds into the existing :class:`UserProfile`, bumping ``version``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Traits:
    """Schema-backed communication traits (canonical enum values)."""

    style: str = ""          # concise | balanced | detailed
    expertise: str = ""      # beginner | intermediate | expert
    verbosity: str = ""      # concise | balanced | detailed
    formality: str = ""      # casual | neutral | formal


@dataclass
class Preferences:
    """Schema-backed output preferences (canonical enum values)."""

    format: str = ""         # bullets | narrative | code_heavy | mixed
    language: str = ""       # ru | en | ...
    topics: list[str] = field(default_factory=list)


@dataclass
class UserProfile:
    user_id: str
    traits: Traits = field(default_factory=Traits)
    preferences: Preferences = field(default_factory=Preferences)
    facts: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    updated_at: str = ""     # ISO-8601, set by the merger
    version: int = 0         # monotonic, bumped on every merge
    source: str = ""         # last source that mutated the profile


@dataclass
class Signal:
    """One typed input event for the profile engine."""

    user_id: str
    kind: str                # message | tool_result | feedback | ...
    text: str
    role: str = ""
    ts: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class FactOp:
    """Explicit memory operation over a named durable fact."""

    op: str                  # add | update | forget
    key: str
    value: str = ""


@dataclass
class ProfileDelta:
    """What a single source run contributes.

    Empty strings in ``traits``/``preferences`` mean "no change";
    ``topics=None`` means "no change", ``topics=[]`` clears the list.
    """

    fact_ops: list[FactOp] = field(default_factory=list)
    traits: dict[str, str] = field(default_factory=dict)
    preferences: dict[str, str] = field(default_factory=dict)
    topics: list[str] | None = None
    summary: str = ""
    source: str = ""
