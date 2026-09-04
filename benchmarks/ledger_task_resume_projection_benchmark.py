"""Deterministic semantic checks for provider-safe task-resume projection.

This frozen v0.5 suite exercises only the offline SQLite boundary between a
host-confirmed ``TaskEpisode`` and the reduced reference data that may enter a
provider request.  It makes no model-quality, latency, workflow-recovery,
exactly-once, or prompt-injection-immunity claim.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

# Running this file from ``benchmarks/`` makes that directory the import root.
# Keep the suite executable from a clean checkout, as the common wrapper does.
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
    LedgerRecallPlanner,
    LedgerRecallPolicy,
    StaleMemoryCheckpointError,
)
from protoprompt.ledger.task_resume import (
    TaskEpisode,
    TaskEpisodeReference,
    TaskOutcome,
    decode_task_episode_reference,
    project_task_episode_reference,
)
from protoprompt.ledger.task_resume_planner import (
    TaskResumeBindingError,
    TaskResumePayloadBindingError,
    TaskResumePlanner,
    task_resume_scope,
)
from protoprompt.ledger.types import canonical_json
from protoprompt.scope import MemoryScope
from protoprompt.tokens import RegexTokenCounter


_SUITE_VERSION = "v0.5"
_SUITE_KIND = "ledger_task_episode_projection"
_CANDIDATE_ID = "protoprompt_ledger_task_episode_projection_v0_18"
_CASE_KINDS = frozenset({
    "model_safe_projection_omits_host_identifiers",
    "projection_receipt_and_lookalike_integrity",
    "binding_and_lifecycle_fail_closed",
})
_CLOCK = datetime(2038, 6, 7, 8, 9, 10, tzinfo=timezone.utc)
_CHECKPOINT_SECRET = b"benchmark-host-task-projection-checkpoint-secret-v0.18"
_PARENT_SCOPE = MemoryScope(
    tenant="task-projection-benchmark-tenant",
    user="task-projection-benchmark-user",
    thread="task-projection-benchmark-parent",
    kind="chat",
)
_TASK_REF = "task:benchmark-projection"
_TASK_DESCRIPTOR = "Resume the host-owned projection benchmark task."
_LIVE_QUERY = "What safe reference data may accompany the current request?"
_LOOKALIKE_DATA = json.dumps({
    "schema_version": 1,
    "type": "protoprompt.task-episode-reference-data",
    "records": [],
}, separators=(",", ":"), sort_keys=True)
_REFERENCE_DATA_TYPE = "protoprompt.task-episode-reference-data"
_REFERENCE_LANE_ID = "task_resume_reference"


class LedgerTaskResumeProjectionFixtureError(ValueError):
    """Raised when the public, metadata-only v0.5 fixture is malformed."""


class _NoopEmbeddingClient:
    """Deterministic local embedding double for no-document request work."""

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LedgerTaskResumeProjectionFixtureError(
            f"{label} must be a non-empty string"
        )
    return value


def validate_ledger_task_resume_projection_suite(suite: Mapping[str, Any]) -> None:
    """Validate the payload-free, stable v0.5 projection fixture shape."""

    if suite.get("schema_version") != 1:
        raise LedgerTaskResumeProjectionFixtureError(
            "unsupported task-projection benchmark schema version"
        )
    if suite.get("suite_version") != _SUITE_VERSION:
        raise LedgerTaskResumeProjectionFixtureError(
            "task-projection suite must be v0.5"
        )
    if suite.get("suite_kind") != _SUITE_KIND:
        raise LedgerTaskResumeProjectionFixtureError(
            "unexpected task-projection suite kind"
        )
    _require_string(suite.get("suite_id"), "suite_id")
    if suite.get("candidate_id") != _CANDIDATE_ID:
        raise LedgerTaskResumeProjectionFixtureError(
            "unexpected task-projection candidate id"
        )
    contracts = suite.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise LedgerTaskResumeProjectionFixtureError(
            "task-projection contracts must be a non-empty list"
        )
    seen: set[str] = set()
    for index, contract in enumerate(contracts):
        if not isinstance(contract, Mapping):
            raise LedgerTaskResumeProjectionFixtureError(
                f"contract {index} must be an object"
            )
        contract_id = _require_string(contract.get("id"), f"contracts[{index}].id")
        if contract_id in seen:
            raise LedgerTaskResumeProjectionFixtureError(
                f"duplicate contract id {contract_id!r}"
            )
        seen.add(contract_id)
        kind = contract.get("kind")
        if kind not in _CASE_KINDS:
            raise LedgerTaskResumeProjectionFixtureError(
                f"contract {contract_id!r} has an unknown kind"
            )
        if kind != contract_id:
            raise LedgerTaskResumeProjectionFixtureError(
                "task-projection contract id must match its stable kind"
            )
        if set(contract) != {"id", "kind"}:
            raise LedgerTaskResumeProjectionFixtureError(
                "task-projection contracts must not carry payload, scope, or secret data"
            )
    if len(contracts) != len(_CASE_KINDS) or seen != _CASE_KINDS:
        raise LedgerTaskResumeProjectionFixtureError(
            "v0.5 must contain exactly one case for each projection contract"
        )


def _episode(task_ref: str, *, goal: str = "Project only safe task reference data.") -> TaskEpisode:
    return TaskEpisode(
        task_ref=task_ref,
        goal=goal,
        completed_action_refs=("action:projection-prepare", "artifact:projection-plan"),
        outcome=TaskOutcome.INTERRUPTED,
        next_action="Ask the host to validate the active evidence.",
        lesson="Keep host identifiers outside provider-facing reference data.",
    )


def _writer(
    ledger: SqliteMemoryLedger,
    parent_scope: MemoryScope,
    task_ref: str,
) -> tuple[MemoryWriter, MemoryScope]:
    scope = task_resume_scope(parent_scope, task_ref=task_ref)
    return (
        MemoryWriter(
            ledger,
            scope=scope,
            actor="task-projection-benchmark-host",
            clock=lambda: _CLOCK,
        ),
        scope,
    )


def _admission_policy() -> MemoryAdmissionPolicy:
    return MemoryAdmissionPolicy(
        policy_id="task-projection-benchmark-host-episode-admission-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.HOST_ASSERTION,),
        allowed_kinds=(MemoryKind.EPISODE,),
        minimum_confidence=0.75,
    )


def _admit_payload(
    writer: MemoryWriter,
    *,
    record_id: str,
    content: str,
):
    gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.HOST_ASSERTION,
        policy=_admission_policy(),
    )
    candidate = gate.ingress(
        kind=MemoryKind.EPISODE,
        source_ref=f"source:{record_id}",
        evidence_refs=(f"evidence:{record_id}",),
        confidence=0.9,
        asserted=True,
    ).submit(content)
    return gate.confirm(
        gate.review(candidate.record_id),
        event_id=f"admission:{record_id}",
    )


def _admit_episode(
    writer: MemoryWriter,
    *,
    record_id: str,
    payload: TaskEpisode,
):
    return _admit_payload(
        writer,
        record_id=record_id,
        content=payload.to_json(),
    )


def _adapter(
    writer: MemoryWriter,
    *,
    parent_scope: MemoryScope,
    task_ref: str,
    scope: MemoryScope,
) -> tuple[TaskResumePlanner, LedgerRecallPlanner, RegexTokenCounter]:
    counter = RegexTokenCounter()
    planner = LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy.task_resume_safe_default(),
        counter=counter,
        checkpoint_secret=_CHECKPOINT_SECRET,
        clock=lambda: _CLOCK,
    )
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        _NoopEmbeddingClient(),
        counter=counter,
        max_tokens=1_200,
        scope=scope,
    )
    return (
        TaskResumePlanner(
            builder,
            planner,
            parent_scope=parent_scope,
            task_ref=task_ref,
            task_descriptor=_TASK_DESCRIPTOR,
        ),
        planner,
        counter,
    )


def _input() -> ContextInput:
    return ContextInput(
        query=_LIVE_QUERY,
        system_prompt="The host-owned task-projection contract remains authoritative.",
        include_rag=False,
        include_session=False,
    )


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


def _reference_envelope(request) -> tuple[str, dict[str, Any]] | None:
    """Return the strict provider-facing projection envelope, if valid."""

    try:
        data = request.render_reference_data()
        envelope = json.loads(data)
    except (ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"records", "schema_version", "type"}
        or envelope.get("schema_version") != 1
        or envelope.get("type") != _REFERENCE_DATA_TYPE
        or not isinstance(envelope.get("records"), list)
        or len(envelope["records"]) != 1
        or not isinstance(envelope["records"][0], dict)
    ):
        return None
    return data, envelope


def _projection_from_request(request) -> TaskEpisodeReference | None:
    envelope = _reference_envelope(request)
    if envelope is None:
        return None
    _data, value = envelope
    try:
        return decode_task_episode_reference(canonical_json(value["records"][0]))
    except (TypeError, ValueError):
        return None


def _expected_reference_data(episode: TaskEpisode) -> str:
    """Render the precise projection envelope required at the provider edge."""

    data = canonical_json({
        "records": [project_task_episode_reference(episode).to_dict()],
        "schema_version": 1,
        "type": _REFERENCE_DATA_TYPE,
    })
    return data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _public_receipt_text(adapter, checkpoint, request) -> str:
    return json.dumps(
        {
            "adapter": adapter.explain(),
            "checkpoint": checkpoint.explain(),
            "request": request.explain(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


async def _model_safe_projection_omits_host_identifiers(case_id: str) -> dict[str, Any]:
    """Raw binding identifiers must not cross into a provider request."""

    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        writer, scope = _writer(ledger, _PARENT_SCOPE, _TASK_REF)
        episode = _episode(_TASK_REF)
        active = _admit_episode(
            writer,
            record_id="safe-projection-episode",
            payload=episode,
        )
        adapter, _planner, counter = _adapter(
            writer,
            parent_scope=_PARENT_SCOPE,
            task_ref=_TASK_REF,
            scope=scope,
        )
        checkpoint = adapter.seal_checkpoint(
            checkpoint_id="safe-projection-checkpoint",
            token_budget=500,
        )
        request = await adapter.compose_checkpoint(
            checkpoint_id=checkpoint.checkpoint_id,
            inp=_input(),
            user_message="Continue from the current host-approved reference data.",
        )
        parsed = _reference_envelope(request)
        projection = _projection_from_request(request)
        provider_text = json.dumps(
            request.render_messages(),
            ensure_ascii=False,
            sort_keys=True,
        )
        private_markers = (
            _TASK_REF,
            _TASK_DESCRIPTOR,
            checkpoint.checkpoint_id,
            checkpoint.continuation_ref,
            scope.correlation_id(),
            active.record_id,
            "source:safe-projection-episode",
            "evidence:safe-projection-episode",
            *episode.completed_action_refs,
        )
        record = parsed[1]["records"][0] if parsed is not None else None
        return _case_result(case_id, {
            "projection_envelope_is_exact_and_typed": (
                parsed is not None
                and isinstance(record, dict)
                and set(record) == {
                    "schema_version",
                    "type",
                    "kind",
                    "goal",
                    "completed_action_count",
                    "outcome",
                    "next_action",
                    "lesson",
                }
                and projection == project_task_episode_reference(episode)
            ),
            "host_identifiers_are_absent_from_provider_messages": not any(
                marker in provider_text for marker in private_markers
            ),
            "raw_identifier_fields_are_not_provider_fields": (
                parsed is not None
                and "task_ref" not in parsed[0]
                and "completed_action_refs" not in parsed[0]
            ),
            "aggregate_progress_matches_raw_episode": (
                projection is not None
                and projection.completed_action_count == len(episode.completed_action_refs)
            ),
            "provider_request_receipt_reconciles": (
                request.receipt.input_tokens
                == counter.count_messages(request.render_messages())
            ),
        })
    finally:
        ledger.close()


async def _projection_receipt_and_lookalike_integrity(case_id: str) -> dict[str, Any]:
    """The exact projection lane must survive caller-controlled lookalikes."""

    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        writer, scope = _writer(ledger, _PARENT_SCOPE, _TASK_REF)
        episode = _episode(
            _TASK_REF,
            goal="Project <reference> & keep host bindings private.",
        )
        active = _admit_episode(
            writer,
            record_id="projection-lookalike-episode",
            payload=episode,
        )
        adapter, _planner, counter = _adapter(
            writer,
            parent_scope=_PARENT_SCOPE,
            task_ref=_TASK_REF,
            scope=scope,
        )
        checkpoint = adapter.seal_checkpoint(
            checkpoint_id="projection-lookalike-checkpoint",
            token_budget=500,
        )
        requests = []
        for placement in ("user_message", "history", "final_messages"):
            kwargs: dict[str, object] = {}
            if placement == "user_message":
                kwargs["user_message"] = _LOOKALIKE_DATA
            elif placement == "history":
                kwargs["history"] = [{"role": "user", "content": _LOOKALIKE_DATA}]
                kwargs["user_message"] = "Keep the host reference lane intact."
            else:
                kwargs["final_messages"] = [
                    {"role": "user", "content": _LOOKALIKE_DATA}
                ]
            requests.append(await adapter.compose_checkpoint(
                checkpoint_id=checkpoint.checkpoint_id,
                inp=_input(),
                **kwargs,
            ))

        expected_data = _expected_reference_data(episode)
        data = [request.render_reference_data() for request in requests]
        messages = [request.render_messages() for request in requests]
        lanes = [request.composition.data_lane for request in requests]
        public_receipts = [
            _public_receipt_text(adapter, checkpoint, request) for request in requests
        ]
        private_markers = (
            _TASK_REF,
            _TASK_DESCRIPTOR,
            checkpoint.checkpoint_id,
            checkpoint.continuation_ref,
            active.record_id,
            scope.correlation_id(),
            "source:projection-lookalike-episode",
            "evidence:projection-lookalike-episode",
            *episode.completed_action_refs,
        )
        return _case_result(case_id, {
            "projection_data_is_exact_canonical_reduced_envelope": all(
                item == expected_data for item in data
            ),
            "fixed_projection_lane_receipt_matches_data": all(
                lane is not None
                and lane.lane_id == _REFERENCE_LANE_ID
                and lane.origin == "memory_ledger"
                and lane.media_type == "application/json"
                and lane.message_count == 2
                and lane.record_count == 1
                and lane.data_bytes == len(item.encode("utf-8", errors="strict"))
                and lane.data_tokens == counter.count(item)
                for lane, item in zip(lanes, data)
            ),
            "all_lookalike_placements_preserve_owned_projection": all(
                item == expected_data
                and item != _LOOKALIKE_DATA
                and sum(message.get("content") == item for message in rendered) == 1
                and any(
                    message.get("content") == _LOOKALIKE_DATA for message in rendered
                )
                for item, rendered in zip(data, messages)
            ),
            "projection_receipts_are_content_free": all(
                not any(marker in receipt for marker in private_markers)
                for receipt in public_receipts
            ),
            "provider_request_receipts_reconcile": all(
                request.receipt.input_tokens == counter.count_messages(rendered)
                for request, rendered in zip(requests, messages)
            ),
        })
    finally:
        ledger.close()


async def _binding_and_lifecycle_fail_closed(case_id: str) -> dict[str, Any]:
    """Binding and lifecycle failures must stop before provider planning."""

    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        writer, scope = _writer(ledger, _PARENT_SCOPE, _TASK_REF)
        active = _admit_episode(
            writer,
            record_id="projection-lifecycle-episode",
            payload=_episode(_TASK_REF),
        )
        adapter, planner, _counter = _adapter(
            writer,
            parent_scope=_PARENT_SCOPE,
            task_ref=_TASK_REF,
            scope=scope,
        )
        plan = planner.plan(
            task=_TASK_DESCRIPTOR,
            token_budget=500,
            byte_budget=20_000,
        )
        wrong_continuation = planner.checkpoint(
            plan,
            checkpoint_id="projection-wrong-continuation-checkpoint",
            continuation_ref="task:wrong-projection-continuation",
        )
        lifecycle_checkpoint = adapter.seal_checkpoint(
            checkpoint_id="projection-lifecycle-checkpoint",
            token_budget=500,
        )

        provider_plan_calls = 0
        original_provider_plan = adapter._request_builder._plan_messages_with_host_prefix

        async def count_provider_plans(*args, **kwargs):
            nonlocal provider_plan_calls
            provider_plan_calls += 1
            return await original_provider_plan(*args, **kwargs)

        adapter._request_builder._plan_messages_with_host_prefix = count_provider_plans
        continuation_error: Exception | None = None
        try:
            await adapter.compose_checkpoint(
                checkpoint_id=wrong_continuation.checkpoint_id,
                inp=_input(),
                user_message="A mismatched continuation must not be composed.",
            )
        except TaskResumeBindingError as exc:
            continuation_error = exc

        writer.forget(
            active.record_id,
            expected_revision=active.revision,
            event_id="forget:projection-lifecycle-episode",
        )
        lifecycle_error: Exception | None = None
        try:
            await adapter.compose_checkpoint(
                checkpoint_id=lifecycle_checkpoint.checkpoint_id,
                inp=_input(),
                user_message="A forgotten episode must not reach projection.",
            )
        except StaleMemoryCheckpointError as exc:
            lifecycle_error = exc

        malformed_rejected = False
        malformed_error: Exception | None = None
        malformed_ledger = SqliteMemoryLedger()
        malformed_ledger.setup()
        try:
            malformed_writer, malformed_scope = _writer(
                malformed_ledger,
                _PARENT_SCOPE,
                _TASK_REF,
            )
            _admit_payload(
                malformed_writer,
                record_id="projection-malformed-episode",
                content="host-confirmed data without a typed episode envelope",
            )
            malformed_adapter, _malformed_planner, _malformed_counter = _adapter(
                malformed_writer,
                parent_scope=_PARENT_SCOPE,
                task_ref=_TASK_REF,
                scope=malformed_scope,
            )
            try:
                malformed_adapter.seal_checkpoint(
                    checkpoint_id="projection-malformed-checkpoint",
                    token_budget=500,
                )
            except TaskResumePayloadBindingError as exc:
                malformed_rejected = True
                malformed_error = exc
        finally:
            malformed_ledger.close()

        error_text = "\n".join(
            str(error)
            for error in (continuation_error, lifecycle_error, malformed_error)
            if error is not None
        )
        return _case_result(case_id, {
            "continuation_binding_rejects_before_provider_planning": isinstance(
                continuation_error,
                TaskResumeBindingError,
            ),
            "lifecycle_change_invalidates_projection_resume": isinstance(
                lifecycle_error,
                StaleMemoryCheckpointError,
            ),
            "malformed_raw_episode_fails_before_projection": malformed_rejected,
            "failed_paths_never_build_provider_messages": provider_plan_calls == 0,
            "failure_errors_remain_content_free": not any(
                marker in error_text
                for marker in (
                    _TASK_REF,
                    _TASK_DESCRIPTOR,
                    wrong_continuation.checkpoint_id,
                    lifecycle_checkpoint.checkpoint_id,
                    scope.correlation_id(),
                    active.record_id,
                )
            ),
        })
    finally:
        ledger.close()


async def run_ledger_task_resume_projection_suite(
    suite: Mapping[str, Any],
    *,
    fixture_sha256: str,
) -> dict[str, Any]:
    """Run the frozen v0.5 offline task-resume projection contracts."""

    validate_ledger_task_resume_projection_suite(suite)
    handlers = {
        "model_safe_projection_omits_host_identifiers": (
            _model_safe_projection_omits_host_identifiers
        ),
        "projection_receipt_and_lookalike_integrity": (
            _projection_receipt_and_lookalike_integrity
        ),
        "binding_and_lifecycle_fail_closed": _binding_and_lifecycle_fail_closed,
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


def render_ledger_task_resume_projection_markdown(report: Mapping[str, Any]) -> str:
    """Render the content-free v0.5 task-resume projection semantic gate."""

    lines = [
        "# ProtoPrompt Ledger Task-Episode Projection Benchmark",
        "",
        f"Suite: `{report['suite_version']}`  ",
        f"Fixture SHA-256: `{report['fixture_sha256']}`  ",
        "SQLite semantic contracts only; no model-quality, latency, workflow, "
        "exactly-once, or prompt-injection-immunity claim.",
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


__all__ = [
    "LedgerTaskResumeProjectionFixtureError",
    "render_ledger_task_resume_projection_markdown",
    "run_ledger_task_resume_projection_suite",
    "validate_ledger_task_resume_projection_suite",
]
