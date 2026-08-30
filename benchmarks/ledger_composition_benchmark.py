"""Frozen semantic contracts for experimental Ledger request composition.

This deliberately measures no elapsed time and makes no retrieval-quality or
prompt-injection-immunity claim.  It exercises only deterministic boundary
properties of the v0.11 host-owned Ledger-to-request bridge.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import Any

from protoprompt import ContextInput, InMemStore, TokenBudgetExceededError
from protoprompt.injector_budgeted import TokenBudgetedContextBuilder
from protoprompt.ledger import (
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import (
    LedgerContextComposer,
    LedgerRecallPlanner,
    LedgerRecallPolicy,
    StaleMemoryPlanError,
)
from protoprompt.scope import MemoryScope
from protoprompt.tokens import RegexTokenCounter


_SUITE_KIND = "ledger_context_composition"
_CANDIDATE_ID = "protoprompt_ledger_context_composer_v0_11"
_DATA_TYPE = "protoprompt.ledger-recall"
_CLOCK = datetime(2036, 4, 5, 6, 7, 8, tzinfo=timezone.utc)
_CASE_KINDS = {
    "strict_raw_exclusion",
    "ledger_lane_budget",
    "tool_dependency",
    "content_free_explain",
    "stale_forget_race",
}


class LedgerCompositionFixtureError(ValueError):
    """Raised when the dedicated v0.2 fixture is malformed."""


class _NoopEmbeddingClient:
    """A deterministic local embedding double for no-document request work."""

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


class _BlockingEmbeddingClient(_NoopEmbeddingClient):
    """Event-gated local embedding double used only for the stale-race case."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        self.started.set()
        await self.release.wait()
        return await super().embed(texts, model=model)


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LedgerCompositionFixtureError(f"{label} must be a non-empty string")
    return value


def _require_non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerCompositionFixtureError(f"{label} must be a non-negative integer")
    return value


def _scope(value: Mapping[str, Any]) -> MemoryScope:
    fields = {
        name: str(value.get(name, ""))
        for name in ("tenant", "user", "thread", "kind")
    }
    scope = MemoryScope(**fields)
    if scope.is_empty:
        raise LedgerCompositionFixtureError("scope must not be empty")
    return scope


def _validate_record(value: object, label: str) -> None:
    if not isinstance(value, Mapping):
        raise LedgerCompositionFixtureError(f"{label} must be an object")
    _require_string(value.get("record_id"), f"{label}.record_id")
    _require_string(value.get("content"), f"{label}.content")


def validate_ledger_composition_suite(suite: Mapping[str, Any]) -> None:
    """Validate the narrow, versioned fixture shape for this benchmark kind."""

    if suite.get("schema_version") != 1:
        raise LedgerCompositionFixtureError("unsupported ledger benchmark schema version")
    if suite.get("suite_version") != "v0.2":
        raise LedgerCompositionFixtureError("ledger composition suite must be v0.2")
    if suite.get("suite_kind") != _SUITE_KIND:
        raise LedgerCompositionFixtureError("unexpected ledger composition suite kind")
    if suite.get("candidate_id") != _CANDIDATE_ID:
        raise LedgerCompositionFixtureError("unexpected ledger composition candidate id")
    if not isinstance(suite.get("scope"), Mapping):
        raise LedgerCompositionFixtureError("scope must be an object")
    _scope(suite["scope"])
    request = suite.get("request")
    if not isinstance(request, Mapping):
        raise LedgerCompositionFixtureError("request must be an object")
    for name in ("query", "system_prompt", "user_message"):
        _require_string(request.get(name), f"request.{name}")
    for name in ("max_tokens", "ledger_token_budget", "ledger_byte_budget"):
        _require_non_negative_int(request.get(name), f"request.{name}")
    if int(request["max_tokens"]) < 1 or int(request["ledger_token_budget"]) < 1:
        raise LedgerCompositionFixtureError("request token budgets must be positive")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise LedgerCompositionFixtureError("cases must be a non-empty list")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise LedgerCompositionFixtureError(f"case {index} must be an object")
        case_id = _require_string(case.get("id"), f"cases[{index}].id")
        if case_id in seen:
            raise LedgerCompositionFixtureError(f"duplicate case id {case_id!r}")
        seen.add(case_id)
        kind = case.get("kind")
        if kind not in _CASE_KINDS:
            raise LedgerCompositionFixtureError(f"case {case_id!r} has unknown kind")
        _validate_record(case.get("admitted"), f"case {case_id}.admitted")
        markers = case.get("private_markers")
        if not isinstance(markers, list) or not markers:
            raise LedgerCompositionFixtureError(
                f"case {case_id}.private_markers must be a non-empty list"
            )
        if not all(isinstance(marker, str) and marker for marker in markers):
            raise LedgerCompositionFixtureError(
                f"case {case_id}.private_markers must contain non-empty strings"
            )
        if kind == "strict_raw_exclusion":
            _validate_record(case.get("raw"), f"case {case_id}.raw")
        if kind == "tool_dependency":
            if not isinstance(case.get("history"), list) or not isinstance(
                case.get("final_messages"), list
            ):
                raise LedgerCompositionFixtureError(
                    f"case {case_id} requires history and final_messages lists"
                )
        if kind == "stale_forget_race":
            _require_string(case.get("forget_event_id"), f"case {case_id}.forget_event_id")
    if (
        len(cases) != len(_CASE_KINDS)
        or set(_CASE_KINDS) != {str(case["kind"]) for case in cases}
    ):
        raise LedgerCompositionFixtureError(
            "v0.2 must contain exactly one case for each composition contract"
        )


def _writer(ledger: SqliteMemoryLedger, scope: MemoryScope) -> MemoryWriter:
    return MemoryWriter(
        ledger,
        scope=scope,
        actor="benchmark-host",
        clock=lambda: _CLOCK,
    )


def _admission_policy() -> MemoryAdmissionPolicy:
    return MemoryAdmissionPolicy(
        policy_id="benchmark-document-admission-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.DOCUMENT,),
        minimum_confidence=0.5,
    )


def _admit_document(writer: MemoryWriter, record: Mapping[str, Any]):
    record_id = _require_string(record.get("record_id"), "admitted.record_id")
    content = _require_string(record.get("content"), "admitted.content")
    gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.DOCUMENT,
        policy=_admission_policy(),
    )
    candidate = gate.ingress(
        kind=MemoryKind.FACT,
        source_ref=f"benchmark:{record_id}",
        evidence_refs=(f"benchmark:{record_id}:evidence",),
        confidence=0.9,
    ).submit(content)
    return gate.confirm(
        gate.review(candidate.record_id),
        event_id=f"benchmark:admit:{record_id}",
    )


def _confirm_raw(writer: MemoryWriter, record: Mapping[str, Any]):
    record_id = _require_string(record.get("record_id"), "raw.record_id")
    content = _require_string(record.get("content"), "raw.content")
    candidate = writer.propose(
        kind=MemoryKind.FACT,
        content=content,
        source_ref=f"benchmark:{record_id}",
        confidence=0.9,
        record_id=record_id,
        event_id=f"benchmark:observed:{record_id}",
    )
    return writer.confirm(
        candidate.record_id,
        expected_revision=candidate.revision,
        event_id=f"benchmark:confirmed:{record_id}",
    )


def _build_composer(
    *,
    writer: MemoryWriter,
    scope: MemoryScope,
    request: Mapping[str, Any],
    counter: RegexTokenCounter,
    llm: _NoopEmbeddingClient | None = None,
    max_tokens: int | None = None,
) -> LedgerContextComposer:
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        llm or _NoopEmbeddingClient(),
        counter=counter,
        max_tokens=int(request["max_tokens"]) if max_tokens is None else max_tokens,
        scope=scope,
    )
    planner = LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy.admission_safe_default(),
        counter=counter,
        clock=lambda: _CLOCK,
    )
    return LedgerContextComposer(builder, planner)


def _context_input(request: Mapping[str, Any], *, include_rag: bool = False) -> ContextInput:
    return ContextInput(
        query=str(request["query"]),
        system_prompt=str(request["system_prompt"]),
        include_rag=include_rag,
        include_session=False,
    )


async def _plan(
    composer: LedgerContextComposer,
    request: Mapping[str, Any],
    *,
    history: list[dict[str, Any]] | None = None,
    final_messages: list[dict[str, Any]] | None = None,
    include_rag: bool = False,
):
    return await composer.plan_messages(
        _context_input(request, include_rag=include_rag),
        history=history,
        user_message=None if final_messages is not None else str(request["user_message"]),
        final_messages=final_messages,
        ledger_token_budget=int(request["ledger_token_budget"]),
        ledger_byte_budget=int(request["ledger_byte_budget"]),
    )


def _data_message(messages: Sequence[Mapping[str, Any]]) -> tuple[int, Mapping[str, Any], dict[str, Any]]:
    for index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, str):
            continue
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and decoded.get("type") == _DATA_TYPE:
            return index, message, decoded
    raise AssertionError("composed request did not contain Ledger JSON data")


def _receipt_reconciles(request, counter: RegexTokenCounter) -> bool:
    receipt = request.receipt
    messages = request.render_messages()
    return (
        receipt.input_tokens == counter.count_messages(messages)
        and receipt.input_tokens + receipt.output_reserve_tokens + receipt.remaining_tokens
        == receipt.max_tokens
        and receipt.context_tokens
        + receipt.history_tokens
        + receipt.final_input_tokens
        + receipt.output_reserve_tokens
        + receipt.remaining_tokens
        == receipt.max_tokens
    )


def _content_free(request, markers: Sequence[str]) -> bool:
    explained = json.dumps(request.explain(), ensure_ascii=False, allow_nan=False)
    return not any(marker in explained for marker in markers)


def _case_result(case_id: str, checks: Mapping[str, bool]) -> dict[str, Any]:
    if not checks or not all(isinstance(value, bool) for value in checks.values()):
        raise AssertionError(f"case {case_id} produced invalid check values")
    return {
        "status": "ok" if all(checks.values()) else "failed",
        "checks": dict(checks),
        "passed_checks": sum(checks.values()),
        "check_count": len(checks),
    }


async def _strict_raw_exclusion(
    case: Mapping[str, Any],
    request: Mapping[str, Any],
    scope: MemoryScope,
) -> dict[str, Any]:
    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        writer = _writer(ledger, scope)
        admitted = _admit_document(writer, case["admitted"])
        _confirm_raw(writer, case["raw"])
        counter = RegexTokenCounter()
        composed = await _plan(
            _build_composer(writer=writer, scope=scope, request=request, counter=counter),
            request,
        )
        messages = composed.render_messages()
        _index, _message, data = _data_message(messages)
        contents = [record["content"] for record in data["records"]]
        raw_content = str(case["raw"]["content"])
        return _case_result(str(case["id"]), {
            "admitted_record_present": admitted.content in contents,
            "raw_unknown_excluded": raw_content not in json.dumps(messages, ensure_ascii=False),
            "receipt_reconciles": _receipt_reconciles(composed, counter),
            "explain_is_content_free": _content_free(
                composed,
                [
                    *case["private_markers"],
                    admitted.record_id,
                    str(case["raw"]["record_id"]),
                    scope.correlation_id(),
                ],
            ),
        })
    finally:
        ledger.close()


async def _ledger_lane_budget(
    case: Mapping[str, Any],
    request: Mapping[str, Any],
    scope: MemoryScope,
) -> dict[str, Any]:
    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        writer = _writer(ledger, scope)
        _admit_document(writer, case["admitted"])
        counter = RegexTokenCounter()
        wide = await _plan(
            _build_composer(writer=writer, scope=scope, request=request, counter=counter),
            request,
        )
        exact = await _plan(
            _build_composer(
                writer=writer,
                scope=scope,
                request=request,
                counter=counter,
                max_tokens=wide.receipt.input_tokens,
            ),
            request,
        )
        lane = wide.composition.data_lane
        assert lane is not None
        final_tokens = counter.count_messages([{
            "role": "user",
            "content": str(request["user_message"]),
        }])
        lane_too_small = lane.input_tokens + final_tokens - 1
        overflow_section: str | None = None
        try:
            await _plan(
                _build_composer(
                    writer=writer,
                    scope=scope,
                    request=request,
                    counter=counter,
                    max_tokens=lane_too_small,
                ),
                request,
            )
        except TokenBudgetExceededError as exc:
            overflow_section = exc.section
        return _case_result(str(case["id"]), {
            "exact_boundary_fits": exact.receipt.remaining_tokens == 0,
            "lane_budget_enforced": overflow_section == "ledger_data",
            "receipt_reconciles": _receipt_reconciles(exact, counter),
            "data_lane_receipt_present": exact.composition.data_lane is not None,
        })
    finally:
        ledger.close()


async def _tool_dependency(
    case: Mapping[str, Any],
    request: Mapping[str, Any],
    scope: MemoryScope,
) -> dict[str, Any]:
    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        writer = _writer(ledger, scope)
        _admit_document(writer, case["admitted"])
        counter = RegexTokenCounter()
        history = [dict(item) for item in case["history"]]
        final_messages = [dict(item) for item in case["final_messages"]]
        composed = await _plan(
            _build_composer(writer=writer, scope=scope, request=request, counter=counter),
            request,
            history=history,
            final_messages=final_messages,
        )
        messages = composed.render_messages()
        data_index, _message, _data = _data_message(messages)
        call_index = next(
            (index for index, item in enumerate(messages) if item.get("tool_calls")),
            None,
        )
        tool_index = next(
            (index for index, item in enumerate(messages) if item.get("role") == "tool"),
            None,
        )
        call_id = (
            messages[call_index]["tool_calls"][0].get("id")
            if call_index is not None
            else None
        )
        tool_id = messages[tool_index].get("tool_call_id") if tool_index is not None else None
        return _case_result(str(case["id"]), {
            "data_precedes_tool_graph": call_index is not None and data_index < call_index,
            "tool_call_output_contiguous": (
                call_index is not None
                and tool_index is not None
                and tool_index == call_index + 1
                and call_id == tool_id
            ),
            "receipt_reconciles": _receipt_reconciles(composed, counter),
        })
    finally:
        ledger.close()


async def _content_free_explain(
    case: Mapping[str, Any],
    request: Mapping[str, Any],
    scope: MemoryScope,
) -> dict[str, Any]:
    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        writer = _writer(ledger, scope)
        active = _admit_document(writer, case["admitted"])
        counter = RegexTokenCounter()
        composed = await _plan(
            _build_composer(writer=writer, scope=scope, request=request, counter=counter),
            request,
        )
        messages = composed.render_messages()
        data_index, _message, data = _data_message(messages)
        payload = str(case["admitted"]["content"])
        system_texts = [
            item.get("content", "")
            for item in messages
            if item.get("role") == "system"
        ]
        return _case_result(str(case["id"]), {
            "payload_decodes_from_user_data": (
                messages[data_index]["role"] == "user"
                and data["records"] == [{"content": payload, "kind": "fact"}]
            ),
            "payload_absent_from_system_messages": all(
                payload not in text for text in system_texts if isinstance(text, str)
            ),
            "explain_is_content_free": _content_free(
                composed,
                [*case["private_markers"], active.record_id, scope.correlation_id()],
            ),
            "receipt_reconciles": _receipt_reconciles(composed, counter),
        })
    finally:
        ledger.close()


async def _stale_forget_race(
    case: Mapping[str, Any],
    request: Mapping[str, Any],
    scope: MemoryScope,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="protoprompt-ledger-benchmark-") as directory:
        path = Path(directory) / "ledger.db"
        first = SqliteMemoryLedger(str(path))
        first.setup()
        second = SqliteMemoryLedger(str(path))
        llm = _BlockingEmbeddingClient()
        try:
            writer = _writer(first, scope)
            concurrent_writer = _writer(second, scope)
            active = _admit_document(writer, case["admitted"])
            counter = RegexTokenCounter()
            composer = _build_composer(
                writer=writer,
                scope=scope,
                request=request,
                counter=counter,
                llm=llm,
            )
            task = asyncio.create_task(_plan(composer, request, include_rag=True))
            await asyncio.wait_for(llm.started.wait(), timeout=1)
            concurrent_writer.forget(
                active.record_id,
                expected_revision=active.revision,
                event_id=str(case["forget_event_id"]),
            )
            llm.release.set()
            stale = False
            try:
                await task
            except StaleMemoryPlanError:
                stale = True
            return _case_result(str(case["id"]), {
                "forget_wins_before_final_validation": stale,
                "payload_is_removed_from_live_ledger": (
                    concurrent_writer.get(active.record_id) is not None
                    and concurrent_writer.get(active.record_id).content is None
                ),
            })
        finally:
            llm.release.set()
            second.close()
            first.close()


async def run_ledger_composition_suite(
    suite: Mapping[str, Any],
    *,
    fixture_sha256: str,
) -> dict[str, Any]:
    """Run the deterministic v0.2 contract suite without timing measurements."""

    validate_ledger_composition_suite(suite)
    request = suite["request"]
    scope = _scope(suite["scope"])
    results: list[dict[str, Any]] = []
    handlers = {
        "strict_raw_exclusion": _strict_raw_exclusion,
        "ledger_lane_budget": _ledger_lane_budget,
        "tool_dependency": _tool_dependency,
        "content_free_explain": _content_free_explain,
        "stale_forget_race": _stale_forget_race,
    }
    for case in suite["cases"]:
        handler = handlers[str(case["kind"])]
        result = await handler(case, request, scope)
        results.append({"id": str(case["id"]), "result": result})
    passing = sum(item["result"]["status"] == "ok" for item in results)
    return {
        "report_schema_version": 1,
        "suite_version": str(suite["suite_version"]),
        "benchmark_kind": _SUITE_KIND,
        "candidate_id": _CANDIDATE_ID,
        "fixture_sha256": fixture_sha256,
        "cases": results,
        "summary": {
            "case_count": len(results),
            "passing_case_count": passing,
            "all_pass": passing == len(results),
        },
    }


def render_ledger_composition_markdown(report: Mapping[str, Any]) -> str:
    """Render a content-free human report for the v0.2 semantic gate."""

    lines = [
        "# ProtoPrompt Ledger Composition Benchmark",
        "",
        f"Suite: `{report['suite_version']}`  ",
        f"Fixture SHA-256: `{report['fixture_sha256']}`  ",
        "Semantic boundary contracts only; no model-quality or timing claim.",
        "",
        "| Case | Status | Passed checks | Check count |",
        "|---|---|---:|---:|",
    ]
    for case in report["cases"]:
        result = case["result"]
        lines.append(
            "| {case_id} | {status} | {passed} | {count} |".format(
                case_id=case["id"],
                status=result["status"],
                passed=result["passed_checks"],
                count=result["check_count"],
            )
        )
    summary = report["summary"]
    lines.extend([
        "",
        "Summary: {passed}/{total} cases passed; all_pass={all_pass}.".format(
            passed=summary["passing_case_count"],
            total=summary["case_count"],
            all_pass=str(summary["all_pass"]).lower(),
        ),
        "",
    ])
    return "\n".join(lines)
