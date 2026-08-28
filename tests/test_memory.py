from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from protoprompt.memory import MemoryScorer, ScorerWeights, cosine_similarity


@dataclass
class FakeItem:
    kind_weight: float
    refcount: int
    last_touched: int
    step: int
    tokens: int
    vector: list[float] | None = None


def test_cosine_similarity():
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([0, 0], [1, 0]) == 0.0


def test_scorer_terms_without_vectors():
    scorer = MemoryScorer()
    item = FakeItem(kind_weight=3.0, refcount=2, last_touched=5, step=5, tokens=0)
    terms = scorer.explain(item, now=5)
    assert terms["kind"] == 3.0
    assert terms["refs"] == pytest.approx(math.log1p(2))
    assert terms["semantic"] == 0.0
    assert terms["size"] == 0.0
    assert terms["recency"] == pytest.approx(0.7)  # age 0 → exp(0)=1
    assert terms["total"] == pytest.approx(3.0 + math.log1p(2) + 0.7)


def test_scorer_semantic_term():
    scorer = MemoryScorer()
    item = FakeItem(
        kind_weight=1.0, refcount=0, last_touched=0, step=0, tokens=0,
        vector=[1.0, 0.0],
    )
    terms = scorer.explain(item, now=0, goal_vector=[1.0, 0.0])
    assert terms["semantic"] == pytest.approx(1.0)


def test_scorer_ref_half_life_fades():
    scorer = MemoryScorer(ScorerWeights(ref_half_life=10.0))
    item = FakeItem(kind_weight=1.0, refcount=1, last_touched=0, step=0, tokens=0)
    fresh = scorer.explain(item, now=0)["refs"]
    aged = scorer.explain(item, now=10)["refs"]
    assert aged < fresh
