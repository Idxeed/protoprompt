"""Deterministic semantic checks for sealed Ledger recall checkpoints.

This is deliberately not a model, latency, throughput, or retrieval-quality
benchmark.  It exercises only the offline v0.12 checkpoint contract: a sealed
selection can survive a SQLite restart, a modified manifest is rejected, a
selected record's lifecycle change invalidates its checkpoint, and a resumed
selection cannot be composed for an unrelated query.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any

# Running this file from ``benchmarks/`` makes that directory the import root.
# Keep the benchmark executable from a clean checkout without an editable
# install, just like the repository's documented benchmark wrapper.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from protoprompt import ContextInput, InMemStore
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
    LedgerCheckpointError,
    LedgerContextComposer,
    LedgerRecallPlanner,
    LedgerRecallPolicy,
    StaleMemoryCheckpointError,
)
from protoprompt.ledger.types import canonical_json
from protoprompt.scope import MemoryScope
from protoprompt.tokens import RegexTokenCounter


_SUITE_VERSION = "v0.3"
_SUITE_KIND = "ledger_sealed_checkpoint"
_CANDIDATE_ID = "protoprompt_ledger_sealed_checkpoint_v0_12"
_CASE_KINDS = frozenset({
    "restart_success",
    "tamper_rejected",
    "lifecycle_invalidated",
    "resume_query_binding_and_composition_boundary",
})
_LEDGER_DATA_TYPE = "protoprompt.ledger-recall"
_CLOCK = datetime(2038, 6, 7, 8, 9, 10, tzinfo=timezone.utc)
_CHECKPOINT_SECRET = b"benchmark-host-sealed-checkpoint-secret-v0.12"
_TASK = "How should the host resume the sealed checkpoint safely?"
_PAYLOAD = "CHECKPOINT_BENCHMARK_PRIVATE_PAYLOAD durable selected fact"
_SCOPE = MemoryScope(
    tenant="checkpoint-benchmark-tenant",
    user="checkpoint-benchmark-user",
    thread="checkpoint-benchmark-thread",
)


class LedgerCheckpointFixtureError(ValueError):
    """Raised when the frozen v0.3 checkpoint contract fixture is malformed."""


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LedgerCheckpointFixtureError(f"{label} must be a non-empty string")
    return value


def validate_ledger_checkpoint_suite(suite: Mapping[str, Any]) -> None:
    """Validate the public, payload-free v0.3 checkpoint contract fixture."""

    if suite.get("schema_version") != 1:
        raise LedgerCheckpointFixtureError("unsupported checkpoint benchmark schema version")
    if suite.get("suite_version") != _SUITE_VERSION:
        raise LedgerCheckpointFixtureError("sealed checkpoint suite must be v0.3")
    if suite.get("suite_kind") != _SUITE_KIND:
        raise LedgerCheckpointFixtureError("unexpected sealed checkpoint suite kind")
    _require_string(suite.get("suite_id"), "suite_id")
    if suite.get("candidate_id") != _CANDIDATE_ID:
        raise LedgerCheckpointFixtureError("unexpected sealed checkpoint candidate id")
    contracts = suite.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise LedgerCheckpointFixtureError("contracts must be a non-empty list")
    seen: set[str] = set()
    for index, contract in enumerate(contracts):
        if not isinstance(contract, Mapping):
            raise LedgerCheckpointFixtureError(f"contract {index} must be an object")
        contract_id = _require_string(contract.get("id"), f"contracts[{index}].id")
        if contract_id in seen:
            raise LedgerCheckpointFixtureError(f"duplicate contract id {contract_id!r}")
        seen.add(contract_id)
        kind = contract.get("kind")
        if kind not in _CASE_KINDS:
            raise LedgerCheckpointFixtureError(
                f"contract {contract_id!r} has an unknown kind"
            )
        if kind != contract_id:
            raise LedgerCheckpointFixtureError(
                "checkpoint contract id must match its stable kind"
            )
        if set(contract) != {"id", "kind"}:
            raise LedgerCheckpointFixtureError(
                "checkpoint contracts must not carry payload, scope, or secret data"
            )
    if len(contracts) != len(_CASE_KINDS) or seen != _CASE_KINDS:
        raise LedgerCheckpointFixtureError(
            "v0.3 must contain exactly one case for each checkpoint contract"
        )


class _NoopEmbeddingClient:
    """Deterministic local embedding double for request composition."""

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


def _writer(ledger: SqliteMemoryLedger) -> MemoryWriter:
    return MemoryWriter(
        ledger,
        scope=_SCOPE,
        actor="checkpoint-benchmark-host",
        clock=lambda: _CLOCK,
    )


def _admission_policy() -> MemoryAdmissionPolicy:
    return MemoryAdmissionPolicy(
        policy_id="checkpoint-benchmark-document-admission-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.DOCUMENT,),
        minimum_confidence=0.5,
    )


def _admit_document(writer: MemoryWriter):
    """Add one deterministic, strict-admitted record to the test ledger."""

    gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.DOCUMENT,
        policy=_admission_policy(),
    )
    candidate = gate.ingress(
        kind=MemoryKind.FACT,
        source_ref="benchmark:sealed-checkpoint",
        evidence_refs=("benchmark:sealed-checkpoint:evidence",),
        confidence=0.9,
    ).submit(_PAYLOAD)
    return gate.confirm(
        gate.review(candidate.record_id),
        event_id="benchmark:admit-sealed-checkpoint",
    )


def _planner(writer: MemoryWriter, counter: RegexTokenCounter) -> LedgerRecallPlanner:
    return LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy.admission_safe_default(),
        counter=counter,
        checkpoint_secret=_CHECKPOINT_SECRET,
        clock=lambda: _CLOCK,
    )


def _seal(
    planner: LedgerRecallPlanner,
    *,
    checkpoint_id: str,
    continuation_ref: str,
) -> None:
    plan = planner.plan(task=_TASK, token_budget=300, byte_budget=10_000)
    planner.checkpoint(
        plan,
        checkpoint_id=checkpoint_id,
        continuation_ref=continuation_ref,
    )


def _input(query: str = _TASK) -> ContextInput:
    return ContextInput(
        query=query,
        system_prompt="The host-owned checkpoint contract remains authoritative.",
        include_rag=False,
        include_session=False,
    )


def _composer(
    planner: LedgerRecallPlanner,
    counter: RegexTokenCounter,
) -> LedgerContextComposer:
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        _NoopEmbeddingClient(),
        counter=counter,
        max_tokens=500,
        scope=_SCOPE,
    )
    return LedgerContextComposer(builder, planner)


def _ledger_data_message(
    messages: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, str):
            continue
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and decoded.get("type") == _LEDGER_DATA_TYPE:
            return decoded
    return None


def _case_result(case_id: str, checks: Mapping[str, bool]) -> dict[str, Any]:
    if not checks or not all(isinstance(value, bool) for value in checks.values()):
        raise AssertionError(f"case {case_id} produced invalid check values")
    return {
        "id": case_id,
        "result": {
            "status": "ok" if all(checks.values()) else "failed",
            "checks": dict(checks),
            "passed_checks": sum(checks.values()),
            "check_count": len(checks),
        },
    }


async def _restart_success(case_id: str) -> dict[str, Any]:
    """A sealed selection is re-planned and safely composed after restart."""

    with tempfile.TemporaryDirectory(prefix="protoprompt-checkpoint-benchmark-") as directory:
        path = Path(directory) / "ledger.db"
        first = SqliteMemoryLedger(str(path))
        first.setup()
        try:
            initial_counter = RegexTokenCounter()
            selected = _admit_document(_writer(first))
            _seal(
                _planner(_writer(first), initial_counter),
                checkpoint_id="restart-checkpoint",
                continuation_ref="restart-continuation",
            )
        finally:
            first.close()

        restarted = SqliteMemoryLedger(str(path))
        restarted.setup()
        try:
            counter = RegexTokenCounter()
            planner = _planner(_writer(restarted), counter)
            resume = planner.resume_checkpoint("restart-checkpoint", task=_TASK)
            composed = await _composer(planner, counter).plan_checkpoint_messages(
                resume,
                _input(),
                user_message="Apply the sealed checkpoint contract.",
            )
            messages = composed.render_messages()
            data = _ledger_data_message(messages)
            explained = json.dumps(
                {"resume": resume.explain(), "request": composed.explain()},
                ensure_ascii=False,
                sort_keys=True,
            )
            return _case_result(case_id, {
                "resume_succeeds_after_sqlite_restart": resume.continuation_ref
                == "restart-continuation",
                "selected_payload_reappears_only_in_data_lane": data == {
                    "records": [{"content": _PAYLOAD, "kind": "fact"}],
                    "schema_version": 1,
                    "type": _LEDGER_DATA_TYPE,
                }
                and all(
                    _PAYLOAD not in str(message.get("content", ""))
                    for message in messages
                    if message.get("role") == "system"
                ),
                "public_receipts_are_content_free": not any(
                    marker in explained
                    for marker in (
                        _PAYLOAD,
                        selected.record_id,
                        "restart-checkpoint",
                        "restart-continuation",
                        _SCOPE.correlation_id(),
                    )
                ),
                "request_receipt_reconciles": composed.receipt.input_tokens
                == counter.count_messages(messages),
            })
        finally:
            restarted.close()


async def _tamper_rejected(case_id: str) -> dict[str, Any]:
    """A direct SQLite mutation cannot forge a checkpoint HMAC seal."""

    with tempfile.TemporaryDirectory(prefix="protoprompt-checkpoint-benchmark-") as directory:
        path = Path(directory) / "ledger.db"
        ledger = SqliteMemoryLedger(str(path))
        ledger.setup()
        try:
            _admit_document(_writer(ledger))
            _seal(
                _planner(_writer(ledger), RegexTokenCounter()),
                checkpoint_id="tamper-checkpoint",
                continuation_ref="original-continuation",
            )
        finally:
            ledger.close()

        connection = sqlite3.connect(path)
        try:
            changed = connection.execute(
                "UPDATE memory_recall_checkpoints "
                "SET continuation_ref = ? WHERE checkpoint_id = ?",
                ("tampered-continuation", "tamper-checkpoint"),
            ).rowcount
            connection.commit()
        finally:
            connection.close()

        restarted = SqliteMemoryLedger(str(path))
        restarted.setup()
        try:
            rejected = False
            try:
                _planner(_writer(restarted), RegexTokenCounter()).resume_checkpoint(
                    "tamper-checkpoint",
                    task=_TASK,
                )
            except LedgerCheckpointError:
                rejected = True
            return _case_result(case_id, {
                "manifest_row_was_mutated": changed == 1,
                "hmac_tamper_is_rejected_on_resume": rejected,
            })
        finally:
            restarted.close()


async def _lifecycle_invalidated(case_id: str) -> dict[str, Any]:
    """Forgetting the selected record invalidates and scrubs the checkpoint."""

    with tempfile.TemporaryDirectory(prefix="protoprompt-checkpoint-benchmark-") as directory:
        path = Path(directory) / "ledger.db"
        ledger = SqliteMemoryLedger(str(path))
        ledger.setup()
        try:
            writer = _writer(ledger)
            selected = _admit_document(writer)
            _seal(
                _planner(writer, RegexTokenCounter()),
                checkpoint_id="lifecycle-checkpoint",
                continuation_ref="lifecycle-continuation",
            )
            writer.forget(
                selected.record_id,
                expected_revision=selected.revision,
                event_id="benchmark:forget-sealed-checkpoint",
            )
        finally:
            ledger.close()

        connection = sqlite3.connect(path)
        try:
            state = connection.execute(
                "SELECT state FROM memory_recall_checkpoints WHERE checkpoint_id = ?",
                ("lifecycle-checkpoint",),
            ).fetchone()
            selection_count = connection.execute(
                "SELECT COUNT(*) FROM memory_recall_checkpoint_selections "
                "WHERE checkpoint_id = ?",
                ("lifecycle-checkpoint",),
            ).fetchone()
        finally:
            connection.close()

        restarted = SqliteMemoryLedger(str(path))
        restarted.setup()
        try:
            rejected = False
            try:
                _planner(_writer(restarted), RegexTokenCounter()).resume_checkpoint(
                    "lifecycle-checkpoint",
                    task=_TASK,
                )
            except StaleMemoryCheckpointError:
                rejected = True
            return _case_result(case_id, {
                "selected_lifecycle_change_invalidates_checkpoint": state
                == ("invalidated",),
                "selection_markers_are_scrubbed": selection_count == (0,),
                "invalidated_checkpoint_cannot_resume": rejected,
            })
        finally:
            restarted.close()


async def _query_binding_composition_boundary(case_id: str) -> dict[str, Any]:
    """A resume is task-bound and can compose only through the fixed lane."""

    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        writer = _writer(ledger)
        _admit_document(writer)
        counter = RegexTokenCounter()
        planner = _planner(writer, counter)
        _seal(
            planner,
            checkpoint_id="binding-checkpoint",
            continuation_ref="binding-continuation",
        )
        resume = planner.resume_checkpoint("binding-checkpoint", task=_TASK)
        composer = _composer(planner, counter)

        unrelated_query_rejected = False
        try:
            await composer.plan_checkpoint_messages(
                resume,
                _input("What unrelated action should the host take?"),
                user_message="Do not reuse the selected data.",
            )
        except LedgerCheckpointError:
            unrelated_query_rejected = True

        composed = await composer.plan_checkpoint_messages(
            resume,
            _input(),
            user_message="Apply the matching resume task.",
        )
        messages = composed.render_messages()
        data = _ledger_data_message(messages)
        return _case_result(case_id, {
            "unrelated_query_is_rejected": unrelated_query_rejected,
            "matching_query_composes": data is not None,
            "payload_is_not_in_system_lane": all(
                _PAYLOAD not in str(message.get("content", ""))
                for message in messages
                if message.get("role") == "system"
            ),
            "request_receipt_reconciles": composed.receipt.input_tokens
            == counter.count_messages(messages),
        })
    finally:
        ledger.close()


async def run_ledger_checkpoint_suite(
    suite: Mapping[str, Any],
    *,
    fixture_sha256: str,
) -> dict[str, Any]:
    """Run the frozen v0.3 checkpoint contracts without timing measurements."""

    validate_ledger_checkpoint_suite(suite)
    handlers = {
        "restart_success": _restart_success,
        "tamper_rejected": _tamper_rejected,
        "lifecycle_invalidated": _lifecycle_invalidated,
        "resume_query_binding_and_composition_boundary": (
            _query_binding_composition_boundary
        ),
    }
    cases = [
        await handlers[str(contract["kind"])](str(contract["id"]))
        for contract in suite["contracts"]
    ]
    passing = sum(case["result"]["status"] == "ok" for case in cases)
    return {
        "report_schema_version": 1,
        "suite_version": str(suite["suite_version"]),
        "benchmark_kind": str(suite["suite_kind"]),
        "candidate_id": str(suite["candidate_id"]),
        "fixture_sha256": fixture_sha256,
        "cases": cases,
        "summary": {
            "case_count": len(cases),
            "passing_case_count": passing,
            "all_pass": passing == len(cases),
        },
    }


def render_ledger_checkpoint_markdown(report: Mapping[str, Any]) -> str:
    """Render a content-free human view of the v0.3 semantic gate."""

    lines = [
        "# ProtoPrompt Ledger Sealed Checkpoint Benchmark",
        "",
        f"Suite: `{report['suite_version']}`  ",
        f"Fixture SHA-256: `{report['fixture_sha256']}`  ",
        "Semantic checkpoint contracts only; no model-quality or timing claim.",
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


def _load_default_suite() -> dict[str, Any]:
    """Load the same frozen v0.3 fixture used by the common benchmark CLI."""

    path = _ROOT / "benchmarks" / "fixtures" / _SUITE_VERSION / "suite.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LedgerCheckpointFixtureError("default checkpoint suite must be an object")
    return value


async def run_ledger_checkpoint_benchmark() -> dict[str, Any]:
    """Run the default frozen v0.3 checkpoint suite directly."""

    suite = _load_default_suite()
    validate_ledger_checkpoint_suite(suite)
    fixture_sha256 = hashlib.sha256(
        canonical_json(suite).encode("utf-8")
    ).hexdigest()
    return await run_ledger_checkpoint_suite(suite, fixture_sha256=fixture_sha256)


def main() -> int:
    """Print a machine-readable report and signal failed semantic checks."""

    report = asyncio.run(run_ledger_checkpoint_benchmark())
    print(canonical_json(report))
    return 0 if report["summary"]["all_pass"] else 1


if __name__ == "__main__":  # pragma: no cover - executable benchmark entry point
    raise SystemExit(main())
