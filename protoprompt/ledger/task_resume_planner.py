"""Narrow, host-only task-episode resume adapter for Ledger Recall.

This module deliberately composes existing Ledger primitives rather than
creating another selector or a workflow engine.  A host binds an opaque task
reference to a task-specific ``MemoryScope``; only host-confirmed, admitted,
typed ``TaskEpisode`` records in that exact scope can enter the bounded recall
lane.  A sealed Ledger checkpoint binds the same task reference as its opaque
continuation reference.

The adapter does not durably persist a task descriptor or execute an episode's
text.  Its in-memory host capability retains the descriptor so callers cannot
replace it on each resume.  The host must retain its own stable
``task_ref -> checkpoint_id, descriptor`` mapping across restart. Procedures,
task dependencies, conflict resolution, and exactly-once execution
intentionally remain outside this v1 adapter.
"""

from __future__ import annotations

import json
from typing import Any

from protoprompt.context import ContextInput
from protoprompt.injector_budgeted import TokenBudgetedContextBuilder
from protoprompt.ledger.recall.composer import (
    LedgerComposedRequest,
    LedgerContextComposer,
)
from protoprompt.ledger.recall.planner import LedgerRecallPlanner
from protoprompt.ledger.recall.policy import LedgerRecallPolicy
from protoprompt.ledger.recall.types import LedgerRecallCheckpoint, LedgerRecallPlan
from protoprompt.ledger.task_resume import (
    TaskEpisode,
    TaskResumePayloadError,
    decode_task_resume_payload,
)
from protoprompt.ledger.types import MemoryKind, MemoryOrigin, validate_identifier
from protoprompt.scope import MemoryScope


TASK_RESUME_ADAPTER_SCHEMA_VERSION = 1
"""The supported host-only task-episode resume adapter contract."""

TASK_RESUME_SCOPE_KIND = "task_resume"
"""The fixed ``MemoryScope.kind`` separating task-resume records."""

_MAX_TASK_DESCRIPTOR_CHARS = 16_000
_LEDGER_RECALL_DATA_TYPE = "protoprompt.ledger-recall"


class TaskResumeError(RuntimeError):
    """Base error for host-side task-episode resume boundaries."""


class TaskResumeConfigurationError(TaskResumeError, ValueError):
    """Raised when a host tries to construct a widened adapter boundary."""


class TaskResumeBindingError(TaskResumeError, ValueError):
    """Raised when task, scope, checkpoint, or input bindings do not agree."""


class TaskResumeSelectionError(TaskResumeError):
    """Raised when no safe typed task episode can form a resume checkpoint."""


class TaskResumePayloadBindingError(TaskResumeError, ValueError):
    """Raised when selected Ledger content violates the typed task contract."""


def task_resume_scope(parent_scope: MemoryScope, *, task_ref: str) -> MemoryScope:
    """Derive one isolated scope for a host-minted opaque task reference.

    ``task_ref`` must be unique inside the parent host namespace.  A task gets
    its own backend-level physical namespace, but the parent scope's opaque
    correlation marker is part of that namespace too.  Consequently an equal
    task reference in two parent threads/kinds cannot cross-read or resume.
    """

    if not isinstance(parent_scope, MemoryScope):
        raise TypeError("parent_scope must be a MemoryScope")
    normalized_ref = validate_identifier(task_ref, field="task_ref")
    if not parent_scope.tenant.strip() or not parent_scope.user.strip():
        raise TaskResumeConfigurationError(
            "parent_scope must include non-empty tenant and user for task isolation"
        )
    return MemoryScope(
        tenant=parent_scope.tenant,
        user=parent_scope.user,
        thread=f"task:{parent_scope.correlation_id()}:{normalized_ref}",
        kind=TASK_RESUME_SCOPE_KIND,
    )


def _normalize_task_descriptor(value: object) -> str:
    """Mirror the bounded public Ledger task input contract locally."""

    if not isinstance(value, str):
        raise TypeError("task_descriptor must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("task_descriptor must not be empty")
    if len(normalized) > _MAX_TASK_DESCRIPTOR_CHARS:
        raise ValueError(
            f"task_descriptor must be at most {_MAX_TASK_DESCRIPTOR_CHARS} characters"
        )
    return normalized


def _assert_task_episode_policy(policy: LedgerRecallPolicy) -> None:
    """Reject any selection policy broader than the adapter's contract."""

    if not policy.require_admission_audit:
        raise TaskResumeConfigurationError(
            "task resume requires immutable admission evidence"
        )
    if policy.allowed_origins != (MemoryOrigin.HOST_ASSERTION,):
        raise TaskResumeConfigurationError(
            "task resume requires exactly the host_assertion origin"
        )
    if policy.allowed_kinds != (MemoryKind.EPISODE,):
        raise TaskResumeConfigurationError(
            "task resume requires exactly the episode memory kind"
        )
    minimum = LedgerRecallPolicy.task_resume_safe_default().minimum_confidence
    if policy.minimum_confidence < minimum:
        raise TaskResumeConfigurationError(
            "task resume confidence must not be lower than the safe default"
        )


class TaskResumePlanner:
    """Compose one task-specific strict Ledger checkpoint into a request.

    The adapter is host-facing.  Callers construct a normal
    :class:`~protoprompt.ledger.recall.LedgerRecallPlanner` and a normal
    :class:`~protoprompt.injector_budgeted.TokenBudgetedContextBuilder` against
    the derived task scope, then give both to this class.  It creates the
    existing fixed-shape ``LedgerContextComposer`` internally so a checkpoint
    can never be composed through a different recall planner boundary.
    """

    def __init__(
        self,
        request_builder: TokenBudgetedContextBuilder,
        recall_planner: LedgerRecallPlanner,
        *,
        parent_scope: MemoryScope,
        task_ref: str,
        task_descriptor: str,
    ) -> None:
        if not isinstance(request_builder, TokenBudgetedContextBuilder):
            raise TypeError("request_builder must be a TokenBudgetedContextBuilder")
        if not isinstance(recall_planner, LedgerRecallPlanner):
            raise TypeError("recall_planner must be a LedgerRecallPlanner")
        normalized_ref = validate_identifier(task_ref, field="task_ref")
        derived_scope = task_resume_scope(parent_scope, task_ref=normalized_ref)
        if recall_planner.scope != derived_scope:
            raise TaskResumeConfigurationError(
                "recall_planner must use the exact derived task-resume scope"
            )
        if request_builder.scope != derived_scope:
            raise TaskResumeConfigurationError(
                "request_builder must use the exact derived task-resume scope"
            )
        _assert_task_episode_policy(recall_planner.policy)

        self._task_ref = normalized_ref
        self._task_descriptor = _normalize_task_descriptor(task_descriptor)
        self._scope = derived_scope
        self._recall_planner = recall_planner
        self._composer = LedgerContextComposer(request_builder, recall_planner)

    @property
    def task_ref(self) -> str:
        """Return the opaque host task reference for this adapter boundary."""

        return self._task_ref

    @property
    def scope(self) -> MemoryScope:
        """Return the exact task-specific scope pinned to this adapter."""

        return self._scope

    def explain(self) -> dict[str, object]:
        """Return a content-, task-, scope-, and checkpoint-free contract receipt."""

        return {
            "schema_version": TASK_RESUME_ADAPTER_SCHEMA_VERSION,
            "contract_id": "ledger-task-episode-resume-v1",
            "scope_binding": "derived_task_specific",
            "payload_kinds": [TaskEpisode.KIND],
            "continuation_binding": "required",
            "final_validation": "ledger_context_composer",
            "recall_policy": self._recall_planner.policy.explain(),
        }

    def seal_checkpoint(
        self,
        *,
        checkpoint_id: str,
        token_budget: int,
        byte_budget: int = 32_768,
    ) -> LedgerRecallCheckpoint:
        """Seal a non-empty, typed task-episode selection for later resume.

        ``checkpoint_id`` is an opaque value from the host's durable mapping,
        never a model/tool parameter.  The frozen descriptor was bound when
        this host adapter was constructed and is intentionally not stored in
        the Ledger checkpoint.
        """

        plan = self._recall_planner.plan(
            task=self._task_descriptor,
            token_budget=token_budget,
            byte_budget=byte_budget,
        )
        if plan.selected_count == 0:
            raise TaskResumeSelectionError(
                "task resume requires at least one selected host-confirmed episode"
            )
        self._assert_typed_episode_plan(plan)
        return self._recall_planner.checkpoint(
            plan,
            checkpoint_id=checkpoint_id,
            continuation_ref=self._task_ref,
        )

    async def compose_checkpoint(
        self,
        *,
        checkpoint_id: str,
        inp: ContextInput,
        history: list[dict[str, Any]] | None = None,
        user_message: str | None = None,
        final_messages: list[dict[str, Any]] | None = None,
        output_reserve: int | None = None,
    ) -> LedgerComposedRequest:
        """Freshly resume and compose one host-owned task checkpoint.

        The descriptor bound at adapter construction remains the frozen host
        recall task, while ``ContextInput.query`` stays the current
        request/RAG query.  The adapter passes the former through the
        composer's explicit host-only recall-task boundary, so a current user
        request cannot silently repurpose a task checkpoint.
        """

        if not isinstance(inp, ContextInput):
            raise TypeError("inp must be a ContextInput")
        resume = self._recall_planner.resume_checkpoint(
            checkpoint_id,
            task=self._task_descriptor,
        )
        if resume.continuation_ref != self._task_ref:
            raise TaskResumeBindingError(
                "checkpoint continuation_ref does not match this task_ref"
            )
        self._assert_typed_episode_rendered_data(
            self._recall_planner.resolve_resume(
                resume,
                task=self._task_descriptor,
            ).render_data()
        )
        request = await self._composer.plan_checkpoint_messages(
            resume,
            inp,
            history,
            user_message,
            final_messages=final_messages,
            output_reserve=output_reserve,
            recall_task=self._task_descriptor,
        )
        self._assert_typed_episode_rendered_data(self._ledger_data_from_composed_request(request))
        return request

    def _assert_typed_episode_plan(self, plan: LedgerRecallPlan) -> None:
        """Resolve a public plan and reject malformed/misbinding payloads."""

        context = self._recall_planner.resolve(plan)
        self._assert_typed_episode_rendered_data(context.render_data())

    def _ledger_data_from_composed_request(self, request: LedgerComposedRequest) -> str:
        """Extract exactly one fixed Ledger data lane or fail closed.

        ``LedgerComposedRequest`` owns the data lane and returns it through a
        dedicated public accessor.  Do not scan provider messages here: their
        history and final turns are caller-controlled and can legitimately
        contain JSON that resembles Ledger data.
        """

        lane = request.composition.data_lane
        if (
            lane is None
            or lane.lane_id != "ledger_recall"
            or lane.origin != "memory_ledger"
            or lane.media_type != "application/json"
            or lane.message_count != 2
        ):
            raise TaskResumePayloadBindingError(
                "composed request is missing the fixed Ledger task-resume lane receipt"
            )

        try:
            data = request.render_ledger_data()
        except ValueError as exc:
            raise TaskResumePayloadBindingError(
                "composed request is missing the fixed Ledger task-resume data"
            ) from exc
        try:
            envelope = json.loads(data)
        except (json.JSONDecodeError, RecursionError) as exc:  # pragma: no cover
            raise TaskResumePayloadBindingError(
                "fixed Ledger task-resume data lane is not valid JSON"
            ) from exc
        if not isinstance(envelope, dict) or not isinstance(envelope.get("records"), list):
            raise TaskResumePayloadBindingError(
                "fixed Ledger task-resume data lane has an invalid envelope"
            )
        if (
            len(data.encode("utf-8", errors="strict")) != lane.data_bytes
            or len(envelope["records"]) != lane.record_count
        ):
            raise TaskResumePayloadBindingError(
                "fixed Ledger task-resume data lane does not match its receipt"
            )
        return data

    def _assert_typed_episode_rendered_data(self, rendered_data: str) -> None:
        """Require every selected record to be a matching typed task episode."""

        try:
            envelope = json.loads(rendered_data)
        except (json.JSONDecodeError, RecursionError) as exc:  # pragma: no cover
            raise TaskResumePayloadBindingError(
                "Ledger task-resume data lane is not valid JSON"
            ) from exc
        if (
            not isinstance(envelope, dict)
            or envelope.get("schema_version") != 1
            or envelope.get("type") != _LEDGER_RECALL_DATA_TYPE
            or not isinstance(envelope.get("records"), list)
            or not envelope["records"]
        ):
            raise TaskResumePayloadBindingError(
                "Ledger task-resume data lane has an invalid fixed envelope"
            )
        for record in envelope["records"]:
            if not isinstance(record, dict) or set(record) != {"content", "kind"}:
                raise TaskResumePayloadBindingError(
                    "Ledger task-resume record has an invalid fixed shape"
                )
            if record["kind"] != MemoryKind.EPISODE.value:
                raise TaskResumePayloadBindingError(
                    "Ledger task-resume record is not an episode"
                )
            content = record["content"]
            if not isinstance(content, str):
                raise TaskResumePayloadBindingError(
                    "Ledger task-resume episode content must be a JSON string"
                )
            try:
                payload = decode_task_resume_payload(content)
            except (TaskResumePayloadError, TypeError) as exc:
                raise TaskResumePayloadBindingError(
                    "Ledger task-resume episode violates the typed payload contract"
                ) from exc
            if not isinstance(payload, TaskEpisode):
                raise TaskResumePayloadBindingError(
                    "Ledger task-resume episode payload kind does not match the record"
                )
            if payload.task_ref != self._task_ref:
                raise TaskResumePayloadBindingError(
                    "Ledger task-resume episode task_ref does not match this boundary"
                )


__all__ = [
    "TASK_RESUME_ADAPTER_SCHEMA_VERSION",
    "TASK_RESUME_SCOPE_KIND",
    "TaskResumeBindingError",
    "TaskResumeConfigurationError",
    "TaskResumeError",
    "TaskResumePayloadBindingError",
    "TaskResumePlanner",
    "TaskResumeSelectionError",
    "task_resume_scope",
]
