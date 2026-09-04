"""Deterministic semantic checks for the host-confirmed task-resume boundary.

This frozen v0.4 suite exercises only the offline SQLite contract for the
experimental ``TaskResumePlanner``.  It deliberately makes no claim about
model quality, latency, workflow recovery, exactly-once execution, or
procedure conflict resolution.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
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
    TaskProcedure,
    decode_task_episode_reference,
    decode_task_resume_payload,
    project_task_episode_reference,
)
from protoprompt.ledger.task_resume_planner import (
    TaskResumeBindingError,
    TaskResumePayloadBindingError,
    TaskResumePlanner,
    task_resume_scope,
)
from protoprompt.scope import MemoryScope, scoped_doc_id
from protoprompt.tokens import RegexTokenCounter


_SUITE_VERSION = "v0.4"
_SUITE_KIND = "ledger_task_episode_resume"
_CANDIDATE_ID = "protoprompt_ledger_task_episode_resume_v0_17"
_CASE_KINDS = frozenset({
    "restart_mapping_live_query_rag",
    "strict_host_episode_typed_enforcement",
    "task_and_parent_scope_isolation",
    "continuation_and_lifecycle_fail_closed",
    "receipt_redaction_and_composer_owned_lane",
})
_CLOCK = datetime(2038, 6, 7, 8, 9, 10, tzinfo=timezone.utc)
_CHECKPOINT_SECRET = b"benchmark-host-task-resume-checkpoint-secret-v0.17"
_PARENT_SCOPE = MemoryScope(
    tenant="task-resume-benchmark-tenant",
    user="task-resume-benchmark-user",
    thread="task-resume-benchmark-parent",
    kind="chat",
)
_TASK_REF = "task:benchmark-resume"
_TASK_DESCRIPTOR = "Resume the host-owned deployment task from its episode."
_LIVE_QUERY = "What does the current PDF evidence require before deployment continues?"
_LIVE_RAG_TEXT = "LIVE_RAG_BENCHMARK: inspect the current signed deployment evidence."
_EPISODE_GOAL = "Resume only after the host validates the deployment evidence."
_EPISODE_NEXT_ACTION = "Ask the host to inspect the signed deployment evidence."
_EPISODE_LESSON = "Keep task references in the host mapping across restarts."
_LOOKALIKE_DATA = json.dumps({
    "schema_version": 1,
    "type": "protoprompt.ledger-recall",
    "records": [],
}, separators=(",", ":"), sort_keys=True)
_LEDGER_DATA_TYPE = "protoprompt.ledger-recall"


class LedgerTaskResumeFixtureError(ValueError):
    """Raised when the public, metadata-only v0.4 fixture is malformed."""


class _RecordingEmbeddingClient:
    """Deterministic local embedding double that records current RAG queries."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        self.calls.extend(texts)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LedgerTaskResumeFixtureError(f"{label} must be a non-empty string")
    return value


def validate_ledger_task_resume_suite(suite: Mapping[str, Any]) -> None:
    """Validate the payload-free, stable v0.4 task-resume fixture shape."""

    if suite.get("schema_version") != 1:
        raise LedgerTaskResumeFixtureError(
            "unsupported task-resume benchmark schema version"
        )
    if suite.get("suite_version") != _SUITE_VERSION:
        raise LedgerTaskResumeFixtureError("task-resume suite must be v0.4")
    if suite.get("suite_kind") != _SUITE_KIND:
        raise LedgerTaskResumeFixtureError("unexpected task-resume suite kind")
    _require_string(suite.get("suite_id"), "suite_id")
    if suite.get("candidate_id") != _CANDIDATE_ID:
        raise LedgerTaskResumeFixtureError("unexpected task-resume candidate id")
    contracts = suite.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise LedgerTaskResumeFixtureError("contracts must be a non-empty list")
    seen: set[str] = set()
    for index, contract in enumerate(contracts):
        if not isinstance(contract, Mapping):
            raise LedgerTaskResumeFixtureError(f"contract {index} must be an object")
        contract_id = _require_string(contract.get("id"), f"contracts[{index}].id")
        if contract_id in seen:
            raise LedgerTaskResumeFixtureError(f"duplicate contract id {contract_id!r}")
        seen.add(contract_id)
        kind = contract.get("kind")
        if kind not in _CASE_KINDS:
            raise LedgerTaskResumeFixtureError(
                f"contract {contract_id!r} has an unknown kind"
            )
        if kind != contract_id:
            raise LedgerTaskResumeFixtureError(
                "task-resume contract id must match its stable kind"
            )
        if set(contract) != {"id", "kind"}:
            raise LedgerTaskResumeFixtureError(
                "task-resume contracts must not carry payload, scope, or secret data"
            )
    if len(contracts) != len(_CASE_KINDS) or seen != _CASE_KINDS:
        raise LedgerTaskResumeFixtureError(
            "v0.4 must contain exactly one case for each task-resume contract"
        )


def _episode(task_ref: str, *, goal: str = _EPISODE_GOAL) -> TaskEpisode:
    return TaskEpisode(
        task_ref=task_ref,
        goal=goal,
        completed_action_refs=("action:benchmark-prepare", "artifact:benchmark-plan"),
        outcome=TaskOutcome.INTERRUPTED,
        next_action=_EPISODE_NEXT_ACTION,
        lesson=_EPISODE_LESSON,
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
            actor="task-resume-benchmark-host",
            clock=lambda: _CLOCK,
        ),
        scope,
    )


def _admission_policy(
    *,
    origin: MemoryOrigin,
    kind: MemoryKind,
) -> MemoryAdmissionPolicy:
    return MemoryAdmissionPolicy(
        policy_id=f"task-resume-benchmark-{origin.value}-{kind.value}-admission-v1",
        policy_version="1",
        allowed_origins=(origin,),
        allowed_kinds=(kind,),
        minimum_confidence=0.75,
    )


def _admit_payload(
    writer: MemoryWriter,
    *,
    record_id: str,
    content: str,
    origin: MemoryOrigin = MemoryOrigin.HOST_ASSERTION,
    kind: MemoryKind = MemoryKind.EPISODE,
):
    gate = MemoryReviewGate(
        writer,
        origin=origin,
        policy=_admission_policy(origin=origin, kind=kind),
    )
    candidate = gate.ingress(
        kind=kind,
        source_ref=f"source:{record_id}",
        evidence_refs=(f"evidence:{record_id}",),
        confidence=0.9,
        asserted=origin is MemoryOrigin.HOST_ASSERTION,
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
    origin: MemoryOrigin = MemoryOrigin.HOST_ASSERTION,
):
    return _admit_payload(
        writer,
        record_id=record_id,
        content=payload.to_json(),
        origin=origin,
        kind=MemoryKind.EPISODE,
    )


def _adapter(
    writer: MemoryWriter,
    *,
    parent_scope: MemoryScope,
    task_ref: str,
    scope: MemoryScope,
    store: InMemStore | None = None,
    embeddings: _RecordingEmbeddingClient | None = None,
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
        store or InMemStore(),
        embeddings or _RecordingEmbeddingClient(),
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


def _input(query: str = _LIVE_QUERY, *, include_rag: bool = False) -> ContextInput:
    return ContextInput(
        query=query,
        system_prompt="The host-owned task-resume contract remains authoritative.",
        include_rag=include_rag,
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


def _projection_from_request(request) -> TaskEpisodeReference | None:
    """Read only the adapter-owned, provider-safe episode projection.

    The v0.4 report remains frozen around the semantic fact that one typed
    episode reaches the composer-owned lane.  The adapter now deliberately
    projects that raw host record before provider composition, so this helper
    checks the equivalent reduced contract instead of decoding the raw Ledger
    payload from provider-facing data.
    """

    try:
        envelope = json.loads(request.render_reference_data())
    except (ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema_version") != 1
        or envelope.get("type") != "protoprompt.task-episode-reference-data"
    ):
        return None
    records = envelope.get("records")
    if not isinstance(records, list) or len(records) != 1:
        return None
    record = records[0]
    if not isinstance(record, dict):
        return None
    try:
        decoded = decode_task_episode_reference(json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ))
    except (TypeError, ValueError):
        return None
    return decoded


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


async def _restart_mapping_live_query_rag(case_id: str) -> dict[str, Any]:
    """Reconstruct from a host mapping while RAG uses a live distinct query."""

    host_mapping = {
        "task_ref": _TASK_REF,
        "checkpoint_id": "restart-task-resume-checkpoint",
        "task_descriptor": _TASK_DESCRIPTOR,
    }
    episode = _episode(_TASK_REF)
    with tempfile.TemporaryDirectory(prefix="protoprompt-task-resume-benchmark-") as directory:
        path = Path(directory) / "ledger.db"
        first = SqliteMemoryLedger(str(path))
        first.setup()
        try:
            writer, scope = _writer(first, _PARENT_SCOPE, _TASK_REF)
            selected = _admit_episode(
                writer,
                record_id="restart-task-episode",
                payload=episode,
            )
            adapter, _planner, _counter = _adapter(
                writer,
                parent_scope=_PARENT_SCOPE,
                task_ref=_TASK_REF,
                scope=scope,
            )
            checkpoint = adapter.seal_checkpoint(
                checkpoint_id=host_mapping["checkpoint_id"],
                token_budget=500,
            )
        finally:
            first.close()

        restarted = SqliteMemoryLedger(str(path))
        restarted.setup()
        try:
            writer, scope = _writer(
                restarted,
                _PARENT_SCOPE,
                host_mapping["task_ref"],
            )
            store = InMemStore()
            embeddings = _RecordingEmbeddingClient()
            store.add(
                scoped_doc_id("live-task-evidence", scope),
                [_LIVE_RAG_TEXT],
                [[1.0, 0.0, 0.0, 0.0]],
                metadata=scope.merge_metadata({"kind": "document"}),
            )
            adapter, _planner, counter = _adapter(
                writer,
                parent_scope=_PARENT_SCOPE,
                task_ref=host_mapping["task_ref"],
                scope=scope,
                store=store,
                embeddings=embeddings,
            )
            request = await adapter.compose_checkpoint(
                checkpoint_id=host_mapping["checkpoint_id"],
                inp=_input(include_rag=True),
                user_message="Continue only from the host mapping and current evidence.",
            )
            messages = request.render_messages()
            projection = _projection_from_request(request)
            public_receipts = _public_receipt_text(adapter, checkpoint, request)
            return _case_result(case_id, {
                "host_mapping_reconstructs_after_sqlite_restart": (
                    checkpoint.checkpoint_id == host_mapping["checkpoint_id"]
                    and adapter.task_ref == host_mapping["task_ref"]
                ),
                "live_query_drives_rag_without_rebinding_descriptor": (
                    embeddings.calls == [_LIVE_QUERY]
                    and any(
                        _LIVE_RAG_TEXT in str(message.get("content", ""))
                        for message in messages
                        if message.get("role") == "system"
                    )
                    and all(
                        _TASK_DESCRIPTOR not in str(message.get("content", ""))
                        for message in messages
                    )
                ),
                "typed_episode_returns_from_composer_owned_lane": (
                    projection == project_task_episode_reference(episode)
                ),
                "public_receipts_are_content_free": not any(
                    marker in public_receipts
                    for marker in (
                        selected.record_id,
                        host_mapping["checkpoint_id"],
                        host_mapping["task_ref"],
                        host_mapping["task_descriptor"],
                        scope.correlation_id(),
                        episode.goal,
                        episode.next_action or "",
                        episode.lesson or "",
                        *episode.completed_action_refs,
                    )
                ),
                "request_receipt_reconciles": request.receipt.input_tokens
                == counter.count_messages(messages),
            })
        finally:
            restarted.close()


async def _strict_host_episode_typed_enforcement(case_id: str) -> dict[str, Any]:
    """Select only typed host Episodes and fail closed on invalid episode data."""

    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        writer, scope = _writer(ledger, _PARENT_SCOPE, _TASK_REF)
        valid = _episode(_TASK_REF)
        _admit_episode(writer, record_id="strict-host-episode", payload=valid)
        _admit_episode(
            writer,
            record_id="strict-document-episode",
            payload=valid,
            origin=MemoryOrigin.DOCUMENT,
        )
        procedure = TaskProcedure(
            task_ref=_TASK_REF,
            procedure_ref="procedure:strict-not-selected",
            steps=("Procedures are not part of this narrow planner.",),
            supporting_episode_refs=("episode:strict-host-episode",),
        )
        _admit_payload(
            writer,
            record_id="strict-host-procedure",
            content=procedure.to_json(),
            kind=MemoryKind.PROCEDURE,
        )
        adapter, planner, _counter = _adapter(
            writer,
            parent_scope=_PARENT_SCOPE,
            task_ref=_TASK_REF,
            scope=scope,
        )
        strict_plan = planner.plan(
            task=_TASK_DESCRIPTOR,
            token_budget=500,
            byte_budget=20_000,
        )
        strict_data = json.loads(planner.resolve(strict_plan).render_data())
        sealed = adapter.seal_checkpoint(
            checkpoint_id="strict-host-episode-checkpoint",
            token_budget=500,
        )
    finally:
        ledger.close()

    malformed_rejected = False
    cross_task_rejected = False
    malformed_ledger = SqliteMemoryLedger()
    malformed_ledger.setup()
    try:
        writer, scope = _writer(malformed_ledger, _PARENT_SCOPE, _TASK_REF)
        _admit_payload(
            writer,
            record_id="malformed-host-episode",
            content="host-confirmed data without a typed episode envelope",
        )
        adapter, _planner, _counter = _adapter(
            writer,
            parent_scope=_PARENT_SCOPE,
            task_ref=_TASK_REF,
            scope=scope,
        )
        try:
            adapter.seal_checkpoint(
                checkpoint_id="malformed-host-episode-checkpoint",
                token_budget=500,
            )
        except TaskResumePayloadBindingError:
            malformed_rejected = True
    finally:
        malformed_ledger.close()

    cross_task_ledger = SqliteMemoryLedger()
    cross_task_ledger.setup()
    try:
        writer, scope = _writer(cross_task_ledger, _PARENT_SCOPE, _TASK_REF)
        _admit_episode(
            writer,
            record_id="cross-task-host-episode",
            payload=_episode("task:other-boundary"),
        )
        adapter, _planner, _counter = _adapter(
            writer,
            parent_scope=_PARENT_SCOPE,
            task_ref=_TASK_REF,
            scope=scope,
        )
        try:
            adapter.seal_checkpoint(
                checkpoint_id="cross-task-host-episode-checkpoint",
                token_budget=500,
            )
        except TaskResumePayloadBindingError:
            cross_task_rejected = True
    finally:
        cross_task_ledger.close()

    return _case_result(case_id, {
        "strict_policy_selects_only_host_assertion_episode": strict_data == {
            "schema_version": 1,
            "type": _LEDGER_DATA_TYPE,
            "records": [{"kind": "episode", "content": valid.to_json()}],
        },
        "nonhost_and_procedure_records_are_excluded": strict_plan.selected_count == 1,
        "matching_typed_host_episode_seals": sealed.continuation_ref == _TASK_REF,
        "malformed_episode_fails_closed_before_checkpoint": malformed_rejected,
        "cross_task_episode_fails_closed_before_checkpoint": cross_task_rejected,
    })


async def _task_and_parent_scope_isolation(case_id: str) -> dict[str, Any]:
    """Exercise physical task and parent-derived namespace isolation."""

    sibling_parent = MemoryScope(
        tenant=_PARENT_SCOPE.tenant,
        user=_PARENT_SCOPE.user,
        thread="task-resume-benchmark-sibling-parent",
        kind=_PARENT_SCOPE.kind,
    )
    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        task_a = "task:scope-a"
        task_b = "task:scope-b"
        shared_task = "task:shared-parent-isolation"
        writer_a, scope_a = _writer(ledger, _PARENT_SCOPE, task_a)
        writer_b, scope_b = _writer(ledger, _PARENT_SCOPE, task_b)
        writer_parent, scope_parent = _writer(ledger, _PARENT_SCOPE, shared_task)
        writer_sibling, scope_sibling = _writer(ledger, sibling_parent, shared_task)
        _admit_episode(
            writer_b,
            record_id="task-scope-b-episode",
            payload=_episode(task_b),
        )
        _admit_episode(
            writer_sibling,
            record_id="sibling-parent-episode",
            payload=_episode(shared_task),
        )
        adapter_a, _planner_a, _counter_a = _adapter(
            writer_a,
            parent_scope=_PARENT_SCOPE,
            task_ref=task_a,
            scope=scope_a,
        )
        adapter_b, _planner_b, _counter_b = _adapter(
            writer_b,
            parent_scope=_PARENT_SCOPE,
            task_ref=task_b,
            scope=scope_b,
        )
        adapter_parent, _planner_parent, _counter_parent = _adapter(
            writer_parent,
            parent_scope=_PARENT_SCOPE,
            task_ref=shared_task,
            scope=scope_parent,
        )
        adapter_sibling, _planner_sibling, _counter_sibling = _adapter(
            writer_sibling,
            parent_scope=sibling_parent,
            task_ref=shared_task,
            scope=scope_sibling,
        )
        checkpoint_b = adapter_b.seal_checkpoint(
            checkpoint_id="task-scope-b-checkpoint",
            token_budget=500,
        )
        checkpoint_sibling = adapter_sibling.seal_checkpoint(
            checkpoint_id="sibling-parent-checkpoint",
            token_budget=500,
        )
        task_cross_rejected = False
        parent_cross_rejected = False
        try:
            await adapter_a.compose_checkpoint(
                checkpoint_id=checkpoint_b.checkpoint_id,
                inp=_input(),
                user_message="A task must not read another task checkpoint.",
            )
        except KeyError:
            task_cross_rejected = True
        try:
            await adapter_parent.compose_checkpoint(
                checkpoint_id=checkpoint_sibling.checkpoint_id,
                inp=_input(),
                user_message="A parent scope must not read a sibling checkpoint.",
            )
        except KeyError:
            parent_cross_rejected = True
        return _case_result(case_id, {
            "different_task_refs_derive_distinct_scopes": scope_a != scope_b,
            "same_task_ref_in_different_parents_derives_distinct_scopes": (
                scope_parent != scope_sibling
            ),
            "task_checkpoint_cross_read_is_rejected": task_cross_rejected,
            "parent_scope_checkpoint_cross_read_is_rejected": parent_cross_rejected,
        })
    finally:
        ledger.close()


async def _continuation_and_lifecycle_fail_closed(case_id: str) -> dict[str, Any]:
    """Reject an altered continuation and a lifecycle-invalidated checkpoint."""

    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        writer, scope = _writer(ledger, _PARENT_SCOPE, _TASK_REF)
        active = _admit_episode(
            writer,
            record_id="continuation-lifecycle-episode",
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
            checkpoint_id="wrong-continuation-checkpoint",
            continuation_ref="task:wrong-continuation",
        )
        continuation_rejected = False
        try:
            await adapter.compose_checkpoint(
                checkpoint_id=wrong_continuation.checkpoint_id,
                inp=_input(),
                user_message="Do not compose an unmatched continuation.",
            )
        except TaskResumeBindingError:
            continuation_rejected = True

        lifecycle_checkpoint = adapter.seal_checkpoint(
            checkpoint_id="lifecycle-task-resume-checkpoint",
            token_budget=500,
        )
        writer.forget(
            active.record_id,
            expected_revision=active.revision,
            event_id="forget:continuation-lifecycle-episode",
        )
        lifecycle_rejected = False
        try:
            await adapter.compose_checkpoint(
                checkpoint_id=lifecycle_checkpoint.checkpoint_id,
                inp=_input(),
                user_message="Forgotten task data must not be resumed.",
            )
        except StaleMemoryCheckpointError:
            lifecycle_rejected = True
        return _case_result(case_id, {
            "continuation_mismatch_is_rejected_before_composition": continuation_rejected,
            "lifecycle_change_invalidates_and_blocks_resume": lifecycle_rejected,
        })
    finally:
        ledger.close()


async def _receipt_redaction_and_composer_owned_lane(case_id: str) -> dict[str, Any]:
    """Caller JSON lookalikes cannot replace the fixed composer-owned lane."""

    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        writer, scope = _writer(ledger, _PARENT_SCOPE, _TASK_REF)
        episode = _episode(_TASK_REF)
        active = _admit_episode(
            writer,
            record_id="lookalike-task-episode",
            payload=episode,
        )
        adapter, _planner, counter = _adapter(
            writer,
            parent_scope=_PARENT_SCOPE,
            task_ref=_TASK_REF,
            scope=scope,
        )
        checkpoint = adapter.seal_checkpoint(
            checkpoint_id="lookalike-task-resume-checkpoint",
            token_budget=500,
        )
        requests = []
        for placement in ("user_message", "history", "final_messages"):
            kwargs: dict[str, object] = {}
            if placement == "user_message":
                kwargs["user_message"] = _LOOKALIKE_DATA
            elif placement == "history":
                kwargs["history"] = [{"role": "user", "content": _LOOKALIKE_DATA}]
                kwargs["user_message"] = "Continue from the host task boundary."
            else:
                kwargs["final_messages"] = [
                    {"role": "user", "content": _LOOKALIKE_DATA}
                ]
            request = await adapter.compose_checkpoint(
                checkpoint_id=checkpoint.checkpoint_id,
                inp=_input(),
                **kwargs,
            )
            requests.append(request)

        messages = [request.render_messages() for request in requests]
        data = [request.render_ledger_data() for request in requests]
        projections = [_projection_from_request(request) for request in requests]
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
            episode.goal,
            episode.next_action or "",
            episode.lesson or "",
            *episode.completed_action_refs,
            "source:lookalike-task-episode",
            "evidence:lookalike-task-episode",
        )
        return _case_result(case_id, {
            "all_lookalike_placements_compose": len(requests) == 3,
            "composer_owned_data_lane_remains_fixed": all(
                lane is not None
                and lane.lane_id == "task_resume_reference"
                and lane.origin == "memory_ledger"
                and lane.media_type == "application/json"
                and lane.message_count == 2
                and lane.record_count == 1
                for lane in lanes
            ),
            "lookalikes_do_not_replace_typed_episode_data": all(
                projection == project_task_episode_reference(episode)
                and item != _LOOKALIKE_DATA
                and sum(
                    message.get("content") == item
                    for message in rendered
                ) == 1
                and any(
                    message.get("content") == _LOOKALIKE_DATA
                    for message in rendered
                )
                for projection, item, rendered in zip(projections, data, messages)
            ),
            "public_receipts_are_redacted": all(
                not any(marker in receipt for marker in private_markers)
                for receipt in public_receipts
            ),
            "request_receipts_reconcile": all(
                request.receipt.input_tokens == counter.count_messages(rendered)
                for request, rendered in zip(requests, messages)
            ),
        })
    finally:
        ledger.close()


async def run_ledger_task_resume_suite(
    suite: Mapping[str, Any],
    *,
    fixture_sha256: str,
) -> dict[str, Any]:
    """Run the frozen v0.4 offline task-resume semantic contracts."""

    validate_ledger_task_resume_suite(suite)
    handlers = {
        "restart_mapping_live_query_rag": _restart_mapping_live_query_rag,
        "strict_host_episode_typed_enforcement": _strict_host_episode_typed_enforcement,
        "task_and_parent_scope_isolation": _task_and_parent_scope_isolation,
        "continuation_and_lifecycle_fail_closed": _continuation_and_lifecycle_fail_closed,
        "receipt_redaction_and_composer_owned_lane": (
            _receipt_redaction_and_composer_owned_lane
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


def render_ledger_task_resume_markdown(report: Mapping[str, Any]) -> str:
    """Render the content-free v0.4 task-resume semantic gate."""

    lines = [
        "# ProtoPrompt Ledger Task-Episode Resume Benchmark",
        "",
        f"Suite: `{report['suite_version']}`  ",
        f"Fixture SHA-256: `{report['fixture_sha256']}`  ",
        "SQLite semantic contracts only; no model-quality, latency, workflow, "
        "exactly-once, or procedure-conflict claim.",
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
    "LedgerTaskResumeFixtureError",
    "render_ledger_task_resume_markdown",
    "run_ledger_task_resume_suite",
    "validate_ledger_task_resume_suite",
]
