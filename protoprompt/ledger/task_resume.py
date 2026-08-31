"""Typed, host-side reference data for experimental task-resume memory.

The payloads in this module are deterministic data contracts.  They do not
write to a Ledger, call a model, dispatch tools, or grant authority to execute
their text.  Hosts must treat decoded goals, lessons, next actions, and
procedure steps as untrusted reference data, not tool instructions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any, ClassVar, Mapping, TypeAlias

from protoprompt.ledger.types import (
    MAX_REFERENCE_COUNT,
    MemoryKind,
    canonical_json,
    validate_content,
    validate_reference,
    validate_references,
)


TASK_RESUME_SCHEMA_VERSION = 1
"""The only supported wire schema for task-resume reference data."""

MAX_TASK_GOAL_CHARS = 2_048
MAX_TASK_NEXT_ACTION_CHARS = 2_048
MAX_TASK_LESSON_CHARS = 4_096
MAX_PROCEDURE_STEP_COUNT = MAX_REFERENCE_COUNT
MAX_PROCEDURE_STEP_CHARS = 2_048


class TaskResumePayloadError(ValueError):
    """Raised when a task-resume JSON payload is malformed or unsupported."""


class TaskOutcome(StrEnum):
    """Host-recorded outcome of one task episode."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


def _schema_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("schema_version must be an integer")
    if value != TASK_RESUME_SCHEMA_VERSION:
        raise ValueError("unsupported task-resume schema_version")
    return value


def _bounded_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = validate_content(value)
    if len(normalized) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return normalized


def _optional_bounded_text(value: object, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field=field, maximum=maximum)


def _references(
    value: object,
    *,
    field: str,
    minimum_count: int = 0,
) -> tuple[str, ...]:
    """Normalize one ordered, deterministic opaque-reference sequence."""

    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{field} must be a list or tuple of opaque references")
    normalized = validate_references(value, field=field)
    if len(normalized) < minimum_count:
        raise ValueError(f"{field} must contain at least {minimum_count} reference")
    return normalized


def _steps(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError("steps must be a list or tuple of strings")
    if not value:
        raise ValueError("steps must contain at least one step")
    if len(value) > MAX_PROCEDURE_STEP_COUNT:
        raise ValueError(
            f"steps must contain at most {MAX_PROCEDURE_STEP_COUNT} steps"
        )
    return tuple(
        _bounded_text(
            step,
            field=f"steps[{index}]",
            maximum=MAX_PROCEDURE_STEP_CHARS,
        )
        for index, step in enumerate(value)
    )


@dataclass(frozen=True, slots=True)
class TaskEpisode:
    """One host-recorded task attempt represented as non-executable data."""

    task_ref: str
    goal: str
    completed_action_refs: tuple[str, ...]
    outcome: TaskOutcome | str
    next_action: str | None = None
    lesson: str | None = None
    schema_version: int = TASK_RESUME_SCHEMA_VERSION

    KIND: ClassVar[str] = MemoryKind.EPISODE.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(
            self,
            "task_ref",
            validate_reference(self.task_ref, field="task_ref"),
        )
        object.__setattr__(
            self,
            "goal",
            _bounded_text(self.goal, field="goal", maximum=MAX_TASK_GOAL_CHARS),
        )
        object.__setattr__(
            self,
            "completed_action_refs",
            _references(self.completed_action_refs, field="completed_action_refs"),
        )
        object.__setattr__(self, "outcome", TaskOutcome(self.outcome))
        object.__setattr__(
            self,
            "next_action",
            _optional_bounded_text(
                self.next_action,
                field="next_action",
                maximum=MAX_TASK_NEXT_ACTION_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "lesson",
            _optional_bounded_text(
                self.lesson,
                field="lesson",
                maximum=MAX_TASK_LESSON_CHARS,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the exact JSON-safe reference-data envelope."""

        return {
            "schema_version": self.schema_version,
            "kind": self.KIND,
            "task_ref": self.task_ref,
            "goal": self.goal,
            "completed_action_refs": list(self.completed_action_refs),
            "outcome": self.outcome.value,
            "next_action": self.next_action,
            "lesson": self.lesson,
        }

    def to_json(self) -> str:
        """Encode this record as canonical non-executable reference data."""

        return encode_task_resume_payload(self)


@dataclass(frozen=True, slots=True)
class TaskProcedure:
    """A host-approved reusable procedure represented as reference data only."""

    task_ref: str
    procedure_ref: str
    steps: tuple[str, ...]
    supporting_episode_refs: tuple[str, ...]
    schema_version: int = TASK_RESUME_SCHEMA_VERSION

    KIND: ClassVar[str] = MemoryKind.PROCEDURE.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(
            self,
            "task_ref",
            validate_reference(self.task_ref, field="task_ref"),
        )
        object.__setattr__(
            self,
            "procedure_ref",
            validate_reference(self.procedure_ref, field="procedure_ref"),
        )
        object.__setattr__(self, "steps", _steps(self.steps))
        object.__setattr__(
            self,
            "supporting_episode_refs",
            _references(
                self.supporting_episode_refs,
                field="supporting_episode_refs",
                minimum_count=1,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the exact JSON-safe reference-data envelope."""

        return {
            "schema_version": self.schema_version,
            "kind": self.KIND,
            "task_ref": self.task_ref,
            "procedure_ref": self.procedure_ref,
            "steps": list(self.steps),
            "supporting_episode_refs": list(self.supporting_episode_refs),
        }

    def to_json(self) -> str:
        """Encode this record as canonical non-executable reference data."""

        return encode_task_resume_payload(self)


TaskResumePayload: TypeAlias = TaskEpisode | TaskProcedure


_EPISODE_FIELDS = frozenset({
    "schema_version",
    "kind",
    "task_ref",
    "goal",
    "completed_action_refs",
    "outcome",
    "next_action",
    "lesson",
})
_PROCEDURE_FIELDS = frozenset({
    "schema_version",
    "kind",
    "task_ref",
    "procedure_ref",
    "steps",
    "supporting_episode_refs",
})


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TaskResumePayloadError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise TaskResumePayloadError(f"non-finite JSON constant {value!r} is not allowed")


def _decode_json_object(value: str) -> Mapping[str, Any]:
    if not isinstance(value, str):
        raise TypeError("task-resume payload must be a JSON string")
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except json.JSONDecodeError as exc:
        raise TaskResumePayloadError("invalid task-resume JSON") from exc
    if not isinstance(decoded, dict):
        raise TaskResumePayloadError("task-resume payload must be a JSON object")
    return decoded


def _require_exact_fields(value: Mapping[str, Any], *, expected: frozenset[str]) -> None:
    actual = frozenset(value)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise TaskResumePayloadError(
            "task-resume payload is missing required field(s): "
            + ", ".join(sorted(missing))
        )
    if unknown:
        raise TaskResumePayloadError(
            "task-resume payload contains unknown field(s): "
            + ", ".join(sorted(unknown))
        )


def encode_task_resume_payload(payload: TaskResumePayload) -> str:
    """Return canonical JSON for one typed, non-executable resume payload."""

    if not isinstance(payload, (TaskEpisode, TaskProcedure)):
        raise TypeError("payload must be a TaskEpisode or TaskProcedure")
    return canonical_json(payload.to_dict())


def decode_task_resume_payload(value: str) -> TaskResumePayload:
    """Strictly decode canonical-or-equivalent JSON into a typed payload.

    Unknown fields, duplicate fields, unsupported schemas, a mismatched
    ``kind``/field shape, and malformed values all fail closed.  Decoding only
    constructs immutable data; it never persists a record or executes a step.
    """

    decoded = _decode_json_object(value)
    kind = decoded.get("kind")
    if not isinstance(kind, str):
        raise TaskResumePayloadError("task-resume payload kind must be a string")
    if kind == TaskEpisode.KIND:
        _require_exact_fields(decoded, expected=_EPISODE_FIELDS)
        try:
            return TaskEpisode(
                schema_version=decoded["schema_version"],
                task_ref=decoded["task_ref"],
                goal=decoded["goal"],
                completed_action_refs=decoded["completed_action_refs"],
                outcome=decoded["outcome"],
                next_action=decoded["next_action"],
                lesson=decoded["lesson"],
            )
        except (TypeError, ValueError) as exc:
            raise TaskResumePayloadError("invalid task episode payload") from exc
    if kind == TaskProcedure.KIND:
        _require_exact_fields(decoded, expected=_PROCEDURE_FIELDS)
        try:
            return TaskProcedure(
                schema_version=decoded["schema_version"],
                task_ref=decoded["task_ref"],
                procedure_ref=decoded["procedure_ref"],
                steps=decoded["steps"],
                supporting_episode_refs=decoded["supporting_episode_refs"],
            )
        except (TypeError, ValueError) as exc:
            raise TaskResumePayloadError("invalid task procedure payload") from exc
    raise TaskResumePayloadError("unsupported task-resume payload kind")


__all__ = [
    "MAX_PROCEDURE_STEP_CHARS",
    "MAX_PROCEDURE_STEP_COUNT",
    "MAX_TASK_GOAL_CHARS",
    "MAX_TASK_LESSON_CHARS",
    "MAX_TASK_NEXT_ACTION_CHARS",
    "TASK_RESUME_SCHEMA_VERSION",
    "TaskEpisode",
    "TaskOutcome",
    "TaskProcedure",
    "TaskResumePayload",
    "TaskResumePayloadError",
    "decode_task_resume_payload",
    "encode_task_resume_payload",
]
