"""Frozen dual-backend semantic evidence for strict Ledger recall.

This benchmark deliberately measures only deterministic selection semantics in
a synthetic, versioned corpus.  It does not call a model, vector service, or
provider, and it reports neither latency nor throughput.  The explicit
tail-window comparator is a transparent bounded baseline for this fixture; it
is not a claim about model-answer quality or an external framework.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

from protoprompt.ledger import (
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryState,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import LedgerRecallPlanner, LedgerRecallPolicy
from protoprompt.scope import MemoryScope
from protoprompt.tokens import RegexTokenCounter


_SUITE_KIND = "ledger_dual_backend_recall"
_SUITE_VERSION = "v1.0"
_CANDIDATE_ID = "protoprompt_ledger_recall_strict_v1"
_BASELINE_ID = "tail-active-greedy-json-v1"
_DATA_SCHEMA_VERSION = 1
_DATA_TYPE = "protoprompt.ledger-recall"
_CLOCK_START = datetime(2044, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
_PLANNING_TIME = _CLOCK_START + timedelta(days=7)
_BACKEND_IDS = frozenset({"sqlite_v6", "postgres_v6"})
_BUDGETS = (1_000, 2_000, 4_000)
_LIFECYCLES = ("active", "superseded", "retracted", "source_revoked")


class LedgerRecallEvidenceFixtureError(ValueError):
    """Raised when the immutable Ledger recall evidence fixture is malformed."""


class LedgerRecallEvidenceBackendError(RuntimeError):
    """Raised when a requested evidence backend cannot be constructed."""


def canonical_json(value: object) -> str:
    """Serialize report data with the stable representation used by fixtures."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_sha256(value: object) -> str:
    """Return the SHA-256 of the benchmark's canonical JSON representation."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LedgerRecallEvidenceFixtureError(f"{label} must be a non-empty string")
    return value


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LedgerRecallEvidenceFixtureError(f"{label} must be a positive integer")
    return value


def _policy() -> LedgerRecallPolicy:
    """Return the frozen strict recall policy used by both backends."""

    return LedgerRecallPolicy(
        policy_id="ledger-recall-evidence-strict-v1",
        allowed_kinds=(MemoryKind.FACT, MemoryKind.DECISION, MemoryKind.PREFERENCE),
        minimum_confidence=0.5,
        active_read_limit=1_000,
        candidate_limit=1_000,
        candidate_scan_byte_budget=1_048_576,
        relevance_weight=100.0,
        confidence_weight=10.0,
        recency_weight=1.0,
        require_admission_audit=True,
    )


def _admission_policy() -> MemoryAdmissionPolicy:
    return MemoryAdmissionPolicy(
        policy_id="ledger-recall-evidence-document-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.DOCUMENT,),
        minimum_confidence=0.5,
    )


def _expected_case_ids(depths: Sequence[int]) -> set[str]:
    expected: set[str] = set()
    for depth in depths:
        for budget in _BUDGETS:
            expected.add(f"active-depth-{depth}-budget-{budget}")
        expected.add(f"superseded-depth-{depth}-budget-1000")
        expected.add(f"retracted-depth-{depth}-budget-2000")
        expected.add(f"source-revoked-depth-{depth}-budget-4000")
    return expected


def validate_ledger_recall_evidence_suite(suite: Mapping[str, Any]) -> None:
    """Validate the narrow, immutable v1 Ledger recall evidence fixture."""

    if suite.get("schema_version") != 1:
        raise LedgerRecallEvidenceFixtureError("unsupported evidence fixture schema version")
    if suite.get("suite_version") != _SUITE_VERSION:
        raise LedgerRecallEvidenceFixtureError("Ledger recall evidence suite must be v1.0")
    if suite.get("suite_kind") != _SUITE_KIND:
        raise LedgerRecallEvidenceFixtureError("unexpected Ledger recall evidence suite kind")
    if suite.get("candidate_id") != _CANDIDATE_ID:
        raise LedgerRecallEvidenceFixtureError("unexpected Ledger recall candidate id")
    if suite.get("baseline_id") != _BASELINE_ID:
        raise LedgerRecallEvidenceFixtureError("unexpected Ledger recall baseline id")
    _require_string(suite.get("suite_id"), "suite_id")
    if suite.get("counter_id") != "regex-token-counter-v1":
        raise LedgerRecallEvidenceFixtureError("unexpected evidence token counter")
    if suite.get("policy") != _policy().explain():
        raise LedgerRecallEvidenceFixtureError("evidence policy must match the frozen strict policy")

    tail_window = _require_positive_int(
        suite.get("tail_window_records"),
        "tail_window_records",
    )
    if tail_window > 100:
        raise LedgerRecallEvidenceFixtureError("tail_window_records must be at most 100")

    corpus = suite.get("corpus")
    if not isinstance(corpus, Mapping):
        raise LedgerRecallEvidenceFixtureError("corpus must be an object")
    for field in (
        "query",
        "target_template",
        "replacement_template",
        "filler_template",
        "foreign_template",
    ):
        _require_string(corpus.get(field), f"corpus.{field}")

    depths = suite.get("history_depths")
    if not isinstance(depths, list) or not depths:
        raise LedgerRecallEvidenceFixtureError("history_depths must be a non-empty list")
    normalized_depths = tuple(_require_positive_int(value, "history_depth") for value in depths)
    if normalized_depths != (100, 500, 1_000):
        raise LedgerRecallEvidenceFixtureError(
            "v1.0 must cover history depths 100, 500, and 1000"
        )

    cases = suite.get("cases")
    if not isinstance(cases, list) or len(cases) != 18:
        raise LedgerRecallEvidenceFixtureError("v1.0 must contain exactly 18 cases")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise LedgerRecallEvidenceFixtureError(f"case {index} must be an object")
        case_id = _require_string(case.get("id"), f"cases[{index}].id")
        if case_id in seen:
            raise LedgerRecallEvidenceFixtureError(f"duplicate evidence case id {case_id!r}")
        seen.add(case_id)
        depth = _require_positive_int(case.get("history_depth"), f"case {case_id}.history_depth")
        if depth not in normalized_depths:
            raise LedgerRecallEvidenceFixtureError(
                f"case {case_id} has an unsupported history depth"
            )
        lifecycle = case.get("lifecycle")
        if lifecycle not in _LIFECYCLES:
            raise LedgerRecallEvidenceFixtureError(
                f"case {case_id} has an unsupported lifecycle"
            )
        token_budget = _require_positive_int(
            case.get("token_budget"),
            f"case {case_id}.token_budget",
        )
        byte_budget = _require_positive_int(
            case.get("byte_budget"),
            f"case {case_id}.byte_budget",
        )
        if token_budget not in _BUDGETS:
            raise LedgerRecallEvidenceFixtureError(
                f"case {case_id} has an unsupported token budget"
            )
        if byte_budget < token_budget * 8:
            raise LedgerRecallEvidenceFixtureError(
                f"case {case_id} byte budget must preserve an independent byte lane"
            )
        recall_denominator = case.get("recall_denominator")
        if not isinstance(recall_denominator, bool):
            raise LedgerRecallEvidenceFixtureError(
                f"case {case_id}.recall_denominator must be a bool"
            )
        if recall_denominator != (lifecycle == "active"):
            raise LedgerRecallEvidenceFixtureError(
                "only active delayed recall cases belong to the selection-recall denominator"
            )

    if seen != _expected_case_ids(normalized_depths):
        raise LedgerRecallEvidenceFixtureError(
            "v1.0 evidence cases must contain the fixed delay/budget/lifecycle matrix"
        )
    if suite.get("foreign_scope_axes") != {
        "100": "tenant",
        "500": "user",
        "1000": "thread",
    }:
        raise LedgerRecallEvidenceFixtureError(
            "v1.0 must probe tenant, user, and thread isolation across its depths"
        )


def validate_ledger_recall_evidence_manifest(
    manifest: Mapping[str, Any],
    *,
    suite: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    """Check the content-free manifest binding fixture and expected evidence."""

    if manifest.get("schema_version") != 1:
        raise LedgerRecallEvidenceFixtureError("unsupported evidence manifest schema version")
    if manifest.get("suite_version") != _SUITE_VERSION:
        raise LedgerRecallEvidenceFixtureError("manifest suite version does not match v1.0")
    if manifest.get("suite_kind") != _SUITE_KIND:
        raise LedgerRecallEvidenceFixtureError("manifest suite kind does not match evidence")
    if manifest.get("fixture_sha256") != canonical_sha256(suite):
        raise LedgerRecallEvidenceFixtureError("manifest fixture SHA-256 does not match suite")
    if manifest.get("expected_sha256") != canonical_sha256(expected):
        raise LedgerRecallEvidenceFixtureError("manifest expected SHA-256 does not match baseline")
    if manifest.get("backend_ids") != ["postgres_v6", "sqlite_v6"]:
        raise LedgerRecallEvidenceFixtureError("manifest must declare both semantic backends")


class _AdvancingClock:
    """Deterministic host clock making the target older than its tail history."""

    def __init__(self, start: datetime) -> None:
        self._instant = start

    def __call__(self) -> datetime:
        value = self._instant
        self._instant += timedelta(seconds=1)
        return value


@dataclass
class _Scenario:
    writer: MemoryWriter
    gate: MemoryReviewGate
    planner: LedgerRecallPlanner
    depth: int
    query: str
    target_marker: str
    replacement_marker: str
    foreign_marker: str
    target_record_id: str
    replacement_record_id: str | None = None
    revoked_marker: str | None = None
    revoked_record_id: str | None = None


@dataclass(frozen=True)
class _TailContext:
    rendered: str
    used_tokens: int
    used_bytes: int
    record_count: int
    active_record_count: int


def _scope(depth: int, *, foreign_axis: str | None = None) -> MemoryScope:
    """Return one primary scope or an isolated scope differing in one axis."""

    values = {
        "tenant": "ledger-evidence-primary",
        "user": "ledger-evidence-user",
        "thread": f"history-depth-{depth}",
    }
    if foreign_axis is not None:
        if foreign_axis not in values:
            raise AssertionError(f"unsupported foreign scope axis {foreign_axis!r}")
        values[foreign_axis] = f"ledger-evidence-foreign-{foreign_axis}"
    return MemoryScope(**values)


def _render_data(records: Sequence[Any]) -> str:
    """Render the documented public Ledger data-lane envelope independently.

    The comparator intentionally does not import the planner's private renderer.
    It uses the public JSON shape returned by ``LedgerRecallContext.render_data``
    so token and UTF-8-byte accounting has the same visible data envelope.
    """

    payload = {
        "records": [
            {"content": record.content, "kind": record.kind.value}
            for record in records
        ],
        "schema_version": _DATA_SCHEMA_VERSION,
        "type": _DATA_TYPE,
    }
    rendered = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        rendered.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8", errors="strict"))


def _admit_document(
    gate: MemoryReviewGate,
    *,
    content: str,
    source_ref: str,
    event_id: str,
) -> Any:
    candidate = gate.ingress(
        kind=MemoryKind.FACT,
        source_ref=source_ref,
        evidence_refs=(f"evidence-{event_id}",),
        confidence=0.9,
    ).submit(content)
    return gate.confirm(
        gate.review(candidate.record_id),
        event_id=event_id,
    )


def _build_scenario(
    ledger: Any,
    *,
    suite: Mapping[str, Any],
    depth: int,
) -> _Scenario:
    corpus = suite["corpus"]
    assert isinstance(corpus, Mapping)
    clock = _AdvancingClock(_CLOCK_START)
    writer = MemoryWriter(
        ledger,
        scope=_scope(depth),
        actor="ledger-evidence-host",
        clock=clock,
    )
    gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.DOCUMENT,
        policy=_admission_policy(),
    )
    target_marker = f"EVIDENCE_TARGET_{depth}"
    target = _admit_document(
        gate,
        content=str(corpus["target_template"]).format(
            depth=depth,
            marker=target_marker,
        ),
        source_ref=f"target-source-{depth}",
        event_id=f"confirmed-target-{depth}",
    )
    # ``history_depth`` counts the original target too. It therefore remains
    # within the policy's documented 1000-active-record ceiling at depth 1000.
    for index in range(1, depth):
        _admit_document(
            gate,
            content=str(corpus["filler_template"]).format(depth=depth, index=index),
            source_ref=f"filler-source-{depth}-{index:04d}",
            event_id=f"confirmed-filler-{depth}-{index:04d}",
        )

    # A strict document in another host-owned scope may never be visible to
    # this scenario's planner or tail comparator.
    foreign_axis = str(suite["foreign_scope_axes"][str(depth)])
    foreign_writer = MemoryWriter(
        ledger,
        scope=_scope(depth, foreign_axis=foreign_axis),
        actor="ledger-evidence-foreign-host",
        clock=_AdvancingClock(_CLOCK_START),
    )
    foreign_gate = MemoryReviewGate(
        foreign_writer,
        origin=MemoryOrigin.DOCUMENT,
        policy=_admission_policy(),
    )
    foreign_marker = f"EVIDENCE_FOREIGN_{depth}"
    _admit_document(
        foreign_gate,
        content=str(corpus["foreign_template"]).format(
            depth=depth,
            marker=foreign_marker,
        ),
        source_ref=f"foreign-source-{depth}",
        event_id=f"confirmed-foreign-{depth}",
    )

    return _Scenario(
        writer=writer,
        gate=gate,
        planner=LedgerRecallPlanner(
            writer,
            policy=_policy(),
            counter=RegexTokenCounter(),
            clock=lambda: _PLANNING_TIME,
        ),
        depth=depth,
        query=str(corpus["query"]),
        target_marker=target_marker,
        replacement_marker=f"EVIDENCE_REPLACEMENT_{depth}",
        foreign_marker=foreign_marker,
        target_record_id=target.record_id,
    )


def _transition_to_superseded(scenario: _Scenario, suite: Mapping[str, Any]) -> None:
    corpus = suite["corpus"]
    assert isinstance(corpus, Mapping)
    target = scenario.writer.get(scenario.target_record_id)
    assert target is not None
    replacement = _admit_document(
        scenario.gate,
        content=str(corpus["replacement_template"]).format(
            depth=scenario.depth,
            marker=scenario.replacement_marker,
        ),
        source_ref=f"replacement-source-{scenario.depth}",
        event_id=f"confirmed-replacement-{scenario.depth}",
    )
    scenario.writer.supersede(
        target.record_id,
        replacement_record_id=replacement.record_id,
        expected_revision=target.revision,
        expected_replacement_revision=replacement.revision,
        event_id=f"superseded-{scenario.depth}",
    )
    scenario.replacement_record_id = replacement.record_id


def _transition_to_retracted(scenario: _Scenario) -> None:
    if scenario.replacement_record_id is None:
        raise AssertionError("supersession must create a replacement before retraction")
    replacement = scenario.writer.get(scenario.replacement_record_id)
    assert replacement is not None
    scenario.writer.retract(
        replacement.record_id,
        expected_revision=replacement.revision,
        reason_code="evidence_retract",
        event_id=f"retracted-{scenario.depth}",
    )


def _transition_to_source_revoked(scenario: _Scenario, suite: Mapping[str, Any]) -> None:
    corpus = suite["corpus"]
    assert isinstance(corpus, Mapping)
    marker = f"EVIDENCE_REVOKED_{scenario.depth}"
    revoked = _admit_document(
        scenario.gate,
        content=str(corpus["target_template"]).format(
            depth=scenario.depth,
            marker=marker,
        ),
        source_ref=f"revoked-source-{scenario.depth}",
        event_id=f"confirmed-revoked-{scenario.depth}",
    )
    receipts = scenario.writer.forget_by_source(f"revoked-source-{scenario.depth}")
    if len(receipts) != 1:
        raise AssertionError("source revocation must affect its one fixture record")
    scenario.revoked_marker = marker
    scenario.revoked_record_id = revoked.record_id


def _tail_context(
    scenario: _Scenario,
    *,
    tail_window_records: int,
    token_budget: int,
    byte_budget: int,
) -> _TailContext:
    """Pack the transparent recency baseline in the public data-lane shape."""

    counter = RegexTokenCounter()
    active = scenario.writer.list_active(
        now=_PLANNING_TIME,
        limit=_policy().active_read_limit,
    )
    ordered = sorted(
        active,
        key=lambda record: (-record.updated_at.timestamp(), record.record_id),
    )
    selected: list[Any] = []
    empty = _render_data([])
    if counter.count(empty) > token_budget or _utf8_size(empty) > byte_budget:
        raise AssertionError("fixture budget must fit the mandatory data envelope")
    for record in ordered[:tail_window_records]:
        candidate = [*selected, record]
        rendered = _render_data(candidate)
        if counter.count(rendered) <= token_budget and _utf8_size(rendered) <= byte_budget:
            selected.append(record)
    rendered = _render_data(selected)
    return _TailContext(
        rendered=rendered,
        used_tokens=counter.count(rendered),
        used_bytes=_utf8_size(rendered),
        record_count=len(selected),
        active_record_count=len(active),
    )


def _selected_contents(rendered: str) -> list[str]:
    payload = json.loads(rendered)
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != _DATA_SCHEMA_VERSION
        or payload.get("type") != _DATA_TYPE
        or not isinstance(payload.get("records"), list)
    ):
        raise AssertionError("data lane did not render the documented Ledger envelope")
    contents: list[str] = []
    for item in payload["records"]:
        if not isinstance(item, Mapping) or not isinstance(item.get("content"), str):
            raise AssertionError("data lane contains an invalid record shape")
        contents.append(str(item["content"]))
    return contents


def _evaluate_case(
    scenario: _Scenario,
    *,
    case: Mapping[str, Any],
    tail_window_records: int,
) -> dict[str, Any]:
    lifecycle = str(case["lifecycle"])
    token_budget = int(case["token_budget"])
    byte_budget = int(case["byte_budget"])
    plan = scenario.planner.plan(
        task=scenario.query,
        token_budget=token_budget,
        byte_budget=byte_budget,
    )
    context = scenario.planner.resolve(plan)
    tail = _tail_context(
        scenario,
        tail_window_records=tail_window_records,
        token_budget=token_budget,
        byte_budget=byte_budget,
    )
    strict_rendered = context.render_data()
    strict_contents = _selected_contents(strict_rendered)
    tail_contents = _selected_contents(tail.rendered)
    active = scenario.writer.list_active(
        now=_PLANNING_TIME,
        limit=_policy().active_read_limit,
    )
    active_contents = {record.content for record in active if record.content is not None}
    plan_receipt = plan.explain()
    context_receipt = context.explain()
    explain_text = canonical_json({"plan": plan_receipt, "context": context_receipt})

    strict_has_target = any(scenario.target_marker in value for value in strict_contents)
    strict_has_replacement = any(
        scenario.replacement_marker in value for value in strict_contents
    )
    tail_has_target = any(scenario.target_marker in value for value in tail_contents)
    revoked_selected = bool(scenario.revoked_marker) and any(
        scenario.revoked_marker in value for value in [*strict_contents, *tail_contents]
    )
    foreign_selected = any(
        scenario.foreign_marker in value for value in [*strict_contents, *tail_contents]
    )
    if lifecycle == "active":
        strict_expected = strict_has_target and not strict_has_replacement
        tail_expected = not tail_has_target
    elif lifecycle == "superseded":
        strict_expected = strict_has_replacement and not strict_has_target
        tail_expected = not tail_has_target
    else:
        strict_expected = not strict_has_target and not strict_has_replacement
        tail_expected = not tail_has_target

    counter = RegexTokenCounter()
    receipt_reconciles = (
        context.used_tokens == plan.used_tokens == counter.count(strict_rendered)
        and context.used_bytes == plan.used_bytes == _utf8_size(strict_rendered)
        and context.token_budget == plan.token_budget == token_budget
        and context.byte_budget == plan.byte_budget == byte_budget
        and context_receipt["used_tokens"] == plan_receipt["used_tokens"]
        and context_receipt["used_bytes"] == plan_receipt["used_bytes"]
        and context_receipt["record_count"] == context.record_count
    )
    whole_records = (
        set(strict_contents).issubset(active_contents)
        and set(tail_contents).issubset(active_contents)
    )
    revoked_record = (
        scenario.writer.get(scenario.revoked_record_id)
        if scenario.revoked_record_id is not None
        else None
    )
    source_revocation_enforced = lifecycle != "source_revoked" or (
        revoked_record is not None
        and revoked_record.state is MemoryState.RETRACTED
        and revoked_record.content is None
        and revoked_record.source_refs == ()
        and not revoked_selected
        and not any(
            (scenario.revoked_marker or "") in value for value in active_contents
        )
    )
    private_markers = (
        scenario.query,
        scenario.target_marker,
        scenario.replacement_marker,
        scenario.foreign_marker,
        scenario.writer.scope.tenant,
        scenario.writer.scope.user,
        scenario.writer.scope.thread,
        scenario.target_record_id,
    )
    if scenario.revoked_marker is not None and scenario.revoked_record_id is not None:
        private_markers += (scenario.revoked_marker, scenario.revoked_record_id)
    checks = {
        "strict_lifecycle_selection": strict_expected,
        "tail_lifecycle_selection": tail_expected,
        "strict_budget_fits": (
            context.used_tokens <= token_budget and context.used_bytes <= byte_budget
        ),
        "tail_budget_fits": (
            tail.used_tokens <= token_budget and tail.used_bytes <= byte_budget
        ),
        "strict_receipt_reconciles": receipt_reconciles,
        "same_active_eligible_universe": (
            plan.active_record_count == len(active)
            and plan.eligible_record_count == len(active)
            and tail.active_record_count == len(active)
        ),
        "whole_records_only": whole_records,
        "foreign_scope_excluded": not foreign_selected,
        "source_revocation_enforced": source_revocation_enforced,
        "explain_is_content_free": not any(
            marker in explain_text for marker in private_markers
        ),
    }
    passed = sum(checks.values())
    return {
        "id": str(case["id"]),
        "result": {
            "status": "ok" if passed == len(checks) else "failed",
            "check_count": len(checks),
            "passed_checks": passed,
            "checks": checks,
            "strict": {
                "selected_count": context.record_count,
                "used_tokens": context.used_tokens,
                "used_bytes": context.used_bytes,
            },
            "tail": {
                "selected_count": tail.record_count,
                "used_tokens": tail.used_tokens,
                "used_bytes": tail.used_bytes,
            },
            "recall_denominator": bool(case["recall_denominator"]),
            "ledger_target_available": strict_has_target,
            "tail_target_available": tail_has_target,
        },
    }


def _run_backend(
    suite: Mapping[str, Any],
    *,
    backend_id: str,
    ledger_factory: Callable[[], Any],
    fixture_sha256: str,
) -> dict[str, Any]:
    if backend_id not in _BACKEND_IDS:
        raise LedgerRecallEvidenceBackendError(f"unsupported evidence backend {backend_id!r}")
    validate_ledger_recall_evidence_suite(suite)
    ledger = ledger_factory()
    ledger.setup()
    try:
        cases_by_depth: dict[int, list[Mapping[str, Any]]] = {
            int(depth): [] for depth in suite["history_depths"]
        }
        for case in suite["cases"]:
            assert isinstance(case, Mapping)
            cases_by_depth[int(case["history_depth"])].append(case)
        results: list[dict[str, Any]] = []
        for depth in suite["history_depths"]:
            normalized_depth = int(depth)
            scenario = _build_scenario(ledger, suite=suite, depth=normalized_depth)
            group = {str(case["lifecycle"]): [] for case in cases_by_depth[normalized_depth]}
            for case in cases_by_depth[normalized_depth]:
                group[str(case["lifecycle"])].append(case)
            for case in sorted(group["active"], key=lambda value: int(value["token_budget"])):
                results.append(
                    _evaluate_case(
                        scenario,
                        case=case,
                        tail_window_records=int(suite["tail_window_records"]),
                    )
                )
            _transition_to_superseded(scenario, suite)
            for case in group["superseded"]:
                results.append(
                    _evaluate_case(
                        scenario,
                        case=case,
                        tail_window_records=int(suite["tail_window_records"]),
                    )
                )
            _transition_to_retracted(scenario)
            for case in group["retracted"]:
                results.append(
                    _evaluate_case(
                        scenario,
                        case=case,
                        tail_window_records=int(suite["tail_window_records"]),
                    )
                )
            _transition_to_source_revoked(scenario, suite)
            for case in group["source_revoked"]:
                results.append(
                    _evaluate_case(
                        scenario,
                        case=case,
                        tail_window_records=int(suite["tail_window_records"]),
                    )
                )
    finally:
        ledger.close()

    denominator = [item["result"] for item in results if item["result"]["recall_denominator"]]
    ledger_hits = sum(item["ledger_target_available"] for item in denominator)
    tail_hits = sum(item["tail_target_available"] for item in denominator)
    denominator_count = len(denominator)
    all_pass = all(item["result"]["status"] == "ok" for item in results)
    return {
        "report_schema_version": 1,
        "suite_version": str(suite["suite_version"]),
        "benchmark_kind": str(suite["suite_kind"]),
        "candidate_id": str(suite["candidate_id"]),
        "baseline_id": str(suite["baseline_id"]),
        "fixture_sha256": fixture_sha256,
        "backend_id": backend_id,
        "cases": results,
        "summary": {
            "case_count": len(results),
            "passing_case_count": sum(
                item["result"]["status"] == "ok" for item in results
            ),
            "all_pass": all_pass,
            "selection_recall_in_frozen_synthetic_corpus": {
                "denominator": denominator_count,
                "ledger_hits": ledger_hits,
                "tail_hits": tail_hits,
                "ledger_percent": round(100.0 * ledger_hits / denominator_count, 6),
                "tail_percent": round(100.0 * tail_hits / denominator_count, 6),
                "delta_pp": round(100.0 * (ledger_hits - tail_hits) / denominator_count, 6),
            },
        },
    }


def run_sqlite_ledger_recall_evidence_suite(
    suite: Mapping[str, Any],
    *,
    fixture_sha256: str,
) -> dict[str, Any]:
    """Run the frozen evidence suite on a disposable durable SQLite Ledger."""

    with tempfile.TemporaryDirectory(prefix="protoprompt-ledger-evidence-") as directory:
        database = Path(directory) / "ledger.sqlite3"
        return _run_backend(
            suite,
            backend_id="sqlite_v6",
            ledger_factory=lambda: SqliteMemoryLedger(str(database)),
            fixture_sha256=fixture_sha256,
        )


def _drop_postgres_schema(dsn: str, schema: str) -> None:
    import psycopg

    identifier = '"' + schema.replace('"', '""') + '"'
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f"DROP SCHEMA IF EXISTS {identifier} CASCADE")


def run_postgres_ledger_recall_evidence_suite(
    suite: Mapping[str, Any],
    *,
    fixture_sha256: str,
    dsn: str | None = None,
) -> dict[str, Any]:
    """Run the same public contract on a fresh, disposable PostgreSQL schema."""

    resolved_dsn = dsn or os.environ.get("PROTOPROMPT_POSTGRES_DSN")
    if not resolved_dsn:
        raise LedgerRecallEvidenceBackendError(
            "set PROTOPROMPT_POSTGRES_DSN to run PostgreSQL Ledger evidence"
        )
    try:
        import psycopg  # noqa: F401 - validates the optional backend boundary
        from protoprompt.ledger.postgres import PostgresMemoryLedger
    except ModuleNotFoundError as exc:  # pragma: no cover - environment boundary
        raise LedgerRecallEvidenceBackendError(
            "install protoprompt[postgres] to run PostgreSQL Ledger evidence"
        ) from exc
    schema = "pp_ledger_evidence_" + uuid4().hex
    try:
        return _run_backend(
            suite,
            backend_id="postgres_v6",
            ledger_factory=lambda: PostgresMemoryLedger(resolved_dsn, schema=schema),
            fixture_sha256=fixture_sha256,
        )
    finally:
        _drop_postgres_schema(resolved_dsn, schema)


def run_ledger_recall_evidence_suite(
    suite: Mapping[str, Any],
    *,
    fixture_sha256: str,
    backend: str,
) -> dict[str, Any]:
    """Run one or both evidence backends and return a content-free report."""

    if backend not in {"sqlite", "postgres", "all"}:
        raise LedgerRecallEvidenceBackendError(
            "ledger backend must be one of sqlite, postgres, or all"
        )
    reports: dict[str, dict[str, Any]] = {}
    if backend in {"sqlite", "all"}:
        reports["sqlite_v6"] = run_sqlite_ledger_recall_evidence_suite(
            suite,
            fixture_sha256=fixture_sha256,
        )
    if backend in {"postgres", "all"}:
        reports["postgres_v6"] = run_postgres_ledger_recall_evidence_suite(
            suite,
            fixture_sha256=fixture_sha256,
        )
    return {
        "report_schema_version": 1,
        "suite_version": str(suite["suite_version"]),
        "benchmark_kind": str(suite["suite_kind"]),
        "candidate_id": str(suite["candidate_id"]),
        "baseline_id": str(suite["baseline_id"]),
        "fixture_sha256": fixture_sha256,
        "backend_reports": reports,
    }


def render_ledger_recall_evidence_markdown(report: Mapping[str, Any]) -> str:
    """Render a compact content-free human view of the semantic evidence."""

    lines = [
        "# ProtoPrompt Ledger Recall Evidence v1",
        "",
        f"Suite: `{report['suite_version']}`  ",
        f"Fixture SHA-256: `{report['fixture_sha256']}`  ",
        "Synthetic selection semantics only; no model-quality, latency, or throughput claim.",
        "",
    ]
    backend_reports = report.get("backend_reports")
    if not isinstance(backend_reports, Mapping):
        raise LedgerRecallEvidenceFixtureError("evidence report must contain backend_reports")
    for backend_id in sorted(backend_reports):
        backend = backend_reports[backend_id]
        if not isinstance(backend, Mapping):
            raise LedgerRecallEvidenceFixtureError("evidence backend report must be an object")
        summary = backend["summary"]
        assert isinstance(summary, Mapping)
        recall = summary["selection_recall_in_frozen_synthetic_corpus"]
        assert isinstance(recall, Mapping)
        lines.extend([
            f"## {backend_id}",
            "",
            "| Case | Status | Passed checks | Strict records | Tail records |",
            "|---|---|---:|---:|---:|",
        ])
        for case in backend["cases"]:
            result = case["result"]
            lines.append(
                "| {case_id} | {status} | {passed}/{count} | {strict} | {tail} |".format(
                    case_id=case["id"],
                    status=result["status"],
                    passed=result["passed_checks"],
                    count=result["check_count"],
                    strict=result["strict"]["selected_count"],
                    tail=result["tail"]["selected_count"],
                )
            )
        lines.extend([
            "",
            "Selection recall in the frozen synthetic corpus: "
            "Ledger {ledger}/{denominator}; tail {tail}/{denominator}; "
            "delta {delta} pp.".format(
                ledger=recall["ledger_hits"],
                tail=recall["tail_hits"],
                denominator=recall["denominator"],
                delta=recall["delta_pp"],
            ),
            "",
        ])
    lines.append(
        "The delta is fixture-local target availability, not a model-answer, "
        "external-framework, production-quality, or performance result."
    )
    lines.append("")
    return "\n".join(lines)
