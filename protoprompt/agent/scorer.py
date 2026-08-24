"""Significance scoring: the "what is worth keeping" formula.

score = w_kind·kind + w_refs·log1p(refcount) + w_semantic·cos(item, goal)
        + w_recency·exp(−age/half_life) − w_size·√tokens/100

Everything is cheap and deterministic except the semantic term, which is
computed only when both the item and the goal have embedding vectors.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from protoprompt.agent.types import MemoryItem


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


def _cosine(a: list[float], b: list[float]) -> float:
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
        item: MemoryItem,
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
            semantic = w.semantic * max(0.0, _cosine(goal_vector, item.vector))

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
        item: MemoryItem,
        *,
        now: int,
        goal_vector: list[float] | None = None,
    ) -> float:
        return self.explain(item, now=now, goal_vector=goal_vector)["total"]
