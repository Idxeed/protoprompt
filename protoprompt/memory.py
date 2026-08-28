"""Shared significance scoring — the reusable core of memory lifecycle.

Both the autonomous-agent working memory and (future) profile decay need
the same question answered: *which of these items is worth keeping?* This
module owns that formula, independent of any particular store, eviction
policy, or summary step.

score = w_kind·kind + w_refs·log1p(refcount) + w_semantic·cos(item, goal)
        + w_recency·exp(−age/half_life) − w_size·√tokens/100

:class:`MemoryScorer` works against any object satisfying the
:class:`ScorableItem` protocol;
:class:`protoprompt.agent.types.MemoryItem` matches it structurally.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class ScorableItem(Protocol):
    """Minimal shape the scorer needs (duck-typed, no agent dependency)."""

    kind_weight: float
    refcount: int
    last_touched: int
    step: int
    tokens: int
    vector: list[float] | None


@dataclass
class ScorerWeights:
    kind: float = 1.0
    refs: float = 1.0
    semantic: float = 1.0
    recency: float = 0.7
    size: float = 0.6
    half_life_steps: float = 12.0
    #: When set, the refs term fades with a half-life measured from the
    #: item's last incoming reference (steps). ``None`` = links never fade.
    ref_half_life: float | None = None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class MemoryScorer:
    def __init__(self, weights: ScorerWeights | None = None) -> None:
        self.weights = weights or ScorerWeights()

    def explain(
        self,
        item: ScorableItem,
        *,
        now: int,
        goal_vector: list[float] | None = None,
    ) -> dict[str, float]:
        """Per-term breakdown of the score (for observability/UI)."""
        w = self.weights
        base = w.kind * item.kind_weight

        last = max(item.last_touched, item.step)
        age = max(0, now - last)

        refs_term = w.refs * math.log1p(item.refcount)
        if w.ref_half_life is not None and item.refcount:
            refs_term *= 0.5 ** (age / max(w.ref_half_life, 1e-9))

        semantic = 0.0
        if goal_vector and item.vector:
            semantic = w.semantic * max(
                0.0, cosine_similarity(goal_vector, item.vector)
            )

        recency = w.recency * math.exp(-age / w.half_life_steps)

        size_penalty = -w.size * math.sqrt(max(item.tokens, 0)) / 100.0

        terms = {
            "kind": base,
            "refs": refs_term,
            "semantic": semantic,
            "recency": recency,
            "size": size_penalty,
        }
        terms["total"] = sum(terms.values())
        return terms

    def score(
        self,
        item: ScorableItem,
        *,
        now: int,
        goal_vector: list[float] | None = None,
    ) -> float:
        return self.explain(item, now=now, goal_vector=goal_vector)["total"]
