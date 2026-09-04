"""Regression coverage for bounded full-corpus Ledger recall reads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

import pytest

from protoprompt.ledger.recall import planner as recall_planner
from protoprompt.ledger import (
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import (
    LedgerRecallPlanner,
    LedgerRecallPolicy,
    StaleMemoryPlanError,
)
from protoprompt.scope import MemoryScope
from protoprompt.tokens import RegexTokenCounter


_NOW = datetime(2041, 1, 1, tzinfo=timezone.utc)


def _pre_fast_path_terms(value: str) -> frozenset[str]:
    """Mirror the original lexical normalization for equivalence coverage."""

    term = re.compile(r"[^\W_]+", re.UNICODE)
    return frozenset(match.group(0).casefold() for match in term.finditer(value))


class _DelegatingRegexCounter:
    """Force the conservative counter path while preserving Regex semantics."""

    def __init__(self) -> None:
        self._delegate = RegexTokenCounter()

    def count(self, text: str) -> int:
        return self._delegate.count(text)


@pytest.mark.parametrize(
    "value",
    [
        "ASCII terms, punctuation, 1234 and under_score",
        "JSON-like: {\"content\": \"Exact ASCII recall\"}",
        "Кириллица и mixed ASCII terms",
        "漢字かなカナ and latin terms",
        "Straße and \u0130stanbul are Unicode casefold boundary cases",
    ],
)
def test_lexical_term_specialization_matches_the_original_unicode_aware_loop(value: str):
    """Fast ASCII term extraction must preserve lexical selection semantics."""

    assert recall_planner._terms(value) == _pre_fast_path_terms(value)


@pytest.fixture
def ledger() -> SqliteMemoryLedger:
    value = SqliteMemoryLedger()
    value.setup()
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def writer(ledger: SqliteMemoryLedger) -> MemoryWriter:
    return MemoryWriter(
        ledger,
        scope=MemoryScope(tenant="scaling", user="operator", thread="recall"),
        actor="scaling-host",
        clock=lambda: _NOW,
    )


def _admit_fact(writer: MemoryWriter, *, index: int, content: str) -> None:
    gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.HOST_ASSERTION,
        policy=MemoryAdmissionPolicy.safe_default(),
    )
    candidate = gate.ingress(
        kind=MemoryKind.FACT,
        source_ref=f"scaling-source-{index}",
        evidence_refs=(f"scaling-evidence-{index}",),
        confidence=0.95,
        asserted=True,
    ).submit(content)
    gate.confirm(gate.review(candidate.record_id), event_id=f"scaling-confirm-{index}")


def test_active_read_batches_strict_sidecars_instead_of_issuing_per_record_reads(
    ledger: SqliteMemoryLedger,
    writer: MemoryWriter,
):
    """One active snapshot uses one query per sidecar collection batch."""

    for index in range(5):
        _admit_fact(
            writer,
            index=index,
            content=f"strict admitted recall record {index}",
        )

    statements: list[str] = []
    ledger._conn.set_trace_callback(statements.append)
    try:
        records = writer.list_active(limit=10_000)
    finally:
        ledger._conn.set_trace_callback(None)

    normalized = [" ".join(statement.casefold().split()) for statement in statements]
    assert len(records) == 5
    assert sum("from memory_records as r" in statement for statement in normalized) == 1
    assert sum("from memory_relations" in statement for statement in normalized) == 1
    assert sum("from memory_review_audits as a" in statement for statement in normalized) == 1
    with pytest.raises(ValueError, match="1 to 10000"):
        writer.list_active(limit=10_001)


def test_owned_regex_incremental_packing_matches_the_conservative_full_render_path(
    writer: MemoryWriter,
):
    """The fast path is byte/token-identical to the pre-existing safe path."""

    _admit_fact(
        writer,
        index=1,
        content="Кириллица <ledger> & punctuation must remain exact.",
    )
    _admit_fact(
        writer,
        index=2,
        content="漢字 and emoji 🚀 are whole recall records, not token fragments.",
    )
    _admit_fact(
        writer,
        index=3,
        content="Plain lexical recall data keeps the selected envelope stable.",
    )
    policy = LedgerRecallPolicy(
        policy_id="scaling-packing-equivalence-v1",
        active_read_limit=10,
        candidate_limit=10,
    )
    optimized = LedgerRecallPlanner(
        writer,
        policy=policy,
        counter=RegexTokenCounter(),
        counter_id="scaling-regex-counter-v1",
        clock=lambda: _NOW,
    )
    conservative = LedgerRecallPlanner(
        writer,
        policy=policy,
        counter=_DelegatingRegexCounter(),
        counter_id="scaling-regex-counter-v1",
        clock=lambda: _NOW,
    )

    optimized_plan = optimized.plan(
        task="ledger punctuation lexical recall",
        token_budget=500,
        byte_budget=10_000,
    )
    conservative_plan = conservative.plan(
        task="ledger punctuation lexical recall",
        token_budget=500,
        byte_budget=10_000,
    )

    assert optimized_plan.explain() == conservative_plan.explain()
    assert optimized.resolve(optimized_plan).render_data() == conservative.resolve(
        conservative_plan
    ).render_data()


def test_resolve_revalidates_only_its_sealed_selection_not_the_full_active_window(
    ledger: SqliteMemoryLedger,
    writer: MemoryWriter,
):
    """Final lifecycle checks stay strict without another 10k-style scan."""

    for index in range(5):
        _admit_fact(
            writer,
            index=index,
            content=f"strict recall resolve selection record {index}",
        )
    planner = LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy(
            policy_id="scaling-targeted-resolve-v1",
            active_read_limit=5,
            candidate_limit=5,
        ),
        counter=RegexTokenCounter(),
        clock=lambda: _NOW,
    )
    plan = planner.plan(
        task="strict recall resolve selection",
        token_budget=55,
        byte_budget=10_000,
    )
    assert 0 < plan.selected_count < plan.active_record_count

    statements: list[str] = []
    ledger._conn.set_trace_callback(statements.append)
    try:
        context = planner.resolve(plan)
    finally:
        ledger._conn.set_trace_callback(None)

    active_record_reads = [
        " ".join(statement.casefold().split())
        for statement in statements
        if "from memory_records as r" in statement.casefold()
    ]
    assert context.record_count == plan.selected_count
    # Resolve checks the top-N membership window twice (before rendering and
    # at final linearization), then reads only sealed selected rows twice. It
    # must never deserialize the full active window to validate the plan.
    targeted_reads = [
        statement for statement in active_record_reads if "r.record_id in (" in statement
    ]
    window_reads = [
        statement for statement in active_record_reads if "r.record_id in (" not in statement
    ]
    assert len(targeted_reads) == 2
    assert all(" limit " not in statement for statement in targeted_reads)
    assert len(window_reads) == 2
    assert all(" limit " in statement for statement in window_reads)


def test_resolve_empty_selection_does_not_scan_an_unrelated_active_window(
    ledger: SqliteMemoryLedger,
    writer: MemoryWriter,
):
    """An empty plan needs no top-N membership proof before returning empty data."""

    for index in range(5):
        _admit_fact(
            writer,
            index=index,
            content=f"strict recall empty selection record {index}",
        )
    planner = LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy(
            policy_id="scaling-empty-targeted-resolve-v1",
            allowed_kinds=(MemoryKind.EPISODE,),
            active_read_limit=5,
            candidate_limit=5,
        ),
        counter=RegexTokenCounter(),
        clock=lambda: _NOW,
    )
    plan = planner.plan(
        task="strict recall empty selection",
        token_budget=500,
        byte_budget=10_000,
    )
    assert plan.active_record_count == 5
    assert plan.selected_count == 0

    statements: list[str] = []
    ledger._conn.set_trace_callback(statements.append)
    try:
        context = planner.resolve(plan)
    finally:
        ledger._conn.set_trace_callback(None)

    active_record_reads = [
        " ".join(statement.casefold().split())
        for statement in statements
        if "from memory_records as r" in statement.casefold()
    ]
    assert context.record_count == 0
    assert active_record_reads == []


def test_resolve_fails_when_a_newer_active_record_pushes_selection_out_of_window(
    ledger: SqliteMemoryLedger,
    writer: MemoryWriter,
):
    """Targeted rereads retain the original bounded-active-window semantics."""

    _admit_fact(
        writer,
        index=1,
        content="initial selected record for active window validation",
    )
    planner = LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy(
            policy_id="scaling-window-membership-v1",
            active_read_limit=1,
            candidate_limit=1,
        ),
        counter=RegexTokenCounter(),
        clock=lambda: _NOW,
    )
    plan = planner.plan(
        task="initial selected active window record",
        token_budget=500,
        byte_budget=10_000,
    )
    assert plan.selected_count == 1

    newer_writer = MemoryWriter(
        ledger,
        scope=writer.scope,
        actor="scaling-newer-host",
        clock=lambda: _NOW + timedelta(seconds=1),
    )
    _admit_fact(
        newer_writer,
        index=2,
        content="newer record displaces the old selection from the top one window",
    )

    with pytest.raises(StaleMemoryPlanError, match="changed or is no longer recallable"):
        planner.resolve(plan)
