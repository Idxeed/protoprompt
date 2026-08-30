"""Immutable, content-safe receipts for experimental ledger recall."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import math
from typing import Literal

from protoprompt.ledger.types import MemoryKind, coerce_datetime, validate_identifier
from protoprompt.scope import MemoryScope


RecallDecision = Literal["selected", "excluded"]


class LedgerRecallError(RuntimeError):
    """Base error for experimental ledger recall planning."""


class LedgerRecallBudgetError(LedgerRecallError, ValueError):
    """Raised when even the mandatory JSON data envelope cannot fit."""


class StaleMemoryPlanError(LedgerRecallError):
    """Raised when selected memory changed before it could be rendered."""


class LedgerCheckpointError(LedgerRecallError):
    """Base error for a durable, strict Ledger recall checkpoint."""


class CheckpointContractMismatchError(LedgerCheckpointError):
    """Raised when a restart uses a different policy/counter contract."""


class StaleMemoryCheckpointError(LedgerCheckpointError):
    """Raised when a sealed checkpoint can no longer be resumed safely."""


@dataclass(frozen=True, slots=True)
class LedgerRecallDecision:
    """A content-free choice made by a :class:`LedgerRecallPlanner`.

    Record identifiers, scope, task text, source/evidence references, and
    memory content intentionally never appear here.  The internal selection
    snapshot needed for pre-send validation lives only on ``LedgerRecallPlan``.
    """

    kind: MemoryKind | str
    decision: RecallDecision
    reason: str
    token_cost: int = 0
    byte_cost: int = 0
    candidate_tokens: int | None = None
    candidate_bytes: int | None = None
    score: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", MemoryKind(self.kind))
        if self.decision not in {"selected", "excluded"}:
            raise ValueError("decision must be 'selected' or 'excluded'")
        object.__setattr__(
            self,
            "reason",
            validate_identifier(self.reason, field="reason"),
        )
        for field_name in ("token_cost", "byte_cost"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        for field_name in ("candidate_tokens", "candidate_bytes"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer or None")
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(self.score, (float, int)):
                raise TypeError("score must be a finite number or None")
            if not math.isfinite(float(self.score)):
                raise ValueError("score must be a finite number or None")

    def explain(self) -> dict[str, object]:
        """Return JSON-safe metadata with no candidate content."""

        result: dict[str, object] = {
            "kind": self.kind.value,
            "decision": self.decision,
            "reason": self.reason,
            "token_cost": self.token_cost,
            "byte_cost": self.byte_cost,
        }
        if self.candidate_tokens is not None:
            result["candidate_tokens"] = self.candidate_tokens
        if self.candidate_bytes is not None:
            result["candidate_bytes"] = self.candidate_bytes
        if self.score is not None:
            result["score"] = self.score
        return result


@dataclass(frozen=True, slots=True)
class _RecallSelection:
    """Private metadata used to validate a selection immediately before send."""

    record_id: str = field(repr=False)
    revision: int = field(repr=False)
    content_hash: str = field(repr=False)
    kind: MemoryKind = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_id",
            validate_identifier(self.record_id, field="record_id"),
        )
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if not isinstance(self.content_hash, str) or len(self.content_hash) != 64:
            raise ValueError("content_hash must be a 64-character operational marker")
        object.__setattr__(self, "kind", MemoryKind(self.kind))


@dataclass(frozen=True, slots=True)
class LedgerRecallPlan:
    """A content-free immutable selection snapshot awaiting fresh resolution."""

    schema_version: int
    policy_id: str
    policy_fingerprint: str
    counter_id: str
    planned_at: datetime
    token_budget: int
    byte_budget: int
    used_tokens: int
    used_bytes: int
    active_record_count: int
    active_read_limit_reached: bool
    eligible_record_count: int
    candidate_count: int
    scanned_count: int
    unscanned_count: int
    candidate_limit_reached: bool
    decisions: tuple[LedgerRecallDecision, ...] = ()
    _selections: tuple[_RecallSelection, ...] = field(
        default=(), repr=False, compare=False
    )
    _policy_explain_json: str = field(repr=False, compare=False, default="")
    _owner_token: object | None = field(repr=False, compare=False, default=None)
    _integrity_tag: str = field(repr=False, compare=False, default="")
    _scope: MemoryScope = field(repr=False, compare=False, default_factory=MemoryScope)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported ledger recall plan schema version")
        object.__setattr__(
            self,
            "policy_id",
            validate_identifier(self.policy_id, field="policy_id"),
        )
        if not isinstance(self.policy_fingerprint, str) or len(self.policy_fingerprint) != 64:
            raise ValueError("policy_fingerprint must be a 64-character digest")
        object.__setattr__(
            self,
            "counter_id",
            validate_identifier(self.counter_id, field="counter_id"),
        )
        planned_at = coerce_datetime(self.planned_at, field="planned_at")
        assert planned_at is not None
        object.__setattr__(self, "planned_at", planned_at)
        for field_name in (
            "token_budget",
            "byte_budget",
            "used_tokens",
            "used_bytes",
            "active_record_count",
            "eligible_record_count",
            "candidate_count",
            "scanned_count",
            "unscanned_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.used_tokens > self.token_budget or self.used_bytes > self.byte_budget:
            raise ValueError("ledger recall plan exceeds its configured budget")
        if self.scanned_count + self.unscanned_count != self.candidate_count:
            raise ValueError("candidate scan counts must reconcile")
        if self.eligible_record_count < self.candidate_count:
            raise ValueError("eligible_record_count must cover candidate_count")
        if self.active_record_count < self.eligible_record_count:
            raise ValueError("active_record_count must cover eligible_record_count")
        if not isinstance(self.active_read_limit_reached, bool):
            raise TypeError("active_read_limit_reached must be a bool")
        if not isinstance(self.candidate_limit_reached, bool):
            raise TypeError("candidate_limit_reached must be a bool")
        if self.candidate_limit_reached != (self.eligible_record_count > self.candidate_count):
            raise ValueError("candidate_limit_reached must match candidate counts")
        decisions = tuple(self.decisions)
        if not all(isinstance(decision, LedgerRecallDecision) for decision in decisions):
            raise TypeError("decisions must contain LedgerRecallDecision values")
        selections = tuple(self._selections)
        if not all(isinstance(selection, _RecallSelection) for selection in selections):
            raise TypeError("selections must contain private recall snapshots")
        if sum(decision.decision == "selected" for decision in decisions) != len(selections):
            raise ValueError("selected decisions must match private selection count")
        object.__setattr__(self, "decisions", decisions)
        object.__setattr__(self, "_selections", selections)
        if not isinstance(self._policy_explain_json, str):
            raise TypeError("plan requires a JSON policy receipt")
        try:
            policy = json.loads(self._policy_explain_json)
        except json.JSONDecodeError as exc:
            raise ValueError("plan requires a valid JSON policy receipt") from exc
        if not isinstance(policy, dict) or policy.get("policy_id") != self.policy_id:
            raise ValueError("plan policy receipt must match policy_id")
        if self._owner_token is None:
            raise ValueError("plan requires a private owner token")
        if not isinstance(self._integrity_tag, str) or len(self._integrity_tag) != 64:
            raise ValueError("plan requires a 64-character private integrity tag")
        if not isinstance(self._scope, MemoryScope) or self._scope.is_empty:
            raise ValueError("ledger recall plan requires a non-empty MemoryScope")

    @property
    def selected_count(self) -> int:
        """Return the number of whole memory records selected for resolution."""

        return len(self._selections)

    @property
    def remaining_tokens(self) -> int:
        """Return unused allocation in the selected token lane."""

        return self.token_budget - self.used_tokens

    @property
    def remaining_bytes(self) -> int:
        """Return unused allocation in the selected UTF-8 byte lane."""

        return self.byte_budget - self.used_bytes

    def explain(self) -> dict[str, object]:
        """Return a fresh content-free planning receipt."""

        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_fingerprint": self.policy_fingerprint,
            "policy": json.loads(self._policy_explain_json),
            "counter_id": self.counter_id,
            "planned_at": self.planned_at.isoformat().replace("+00:00", "Z"),
            "token_budget": self.token_budget,
            "byte_budget": self.byte_budget,
            "used_tokens": self.used_tokens,
            "used_bytes": self.used_bytes,
            "remaining_tokens": self.remaining_tokens,
            "remaining_bytes": self.remaining_bytes,
            "active_record_count": self.active_record_count,
            "active_read_limit_reached": self.active_read_limit_reached,
            "eligible_record_count": self.eligible_record_count,
            "candidate_count": self.candidate_count,
            "scanned_count": self.scanned_count,
            "unscanned_count": self.unscanned_count,
            "candidate_limit_reached": self.candidate_limit_reached,
            "selected_count": self.selected_count,
            "decisions": [decision.explain() for decision in self.decisions],
        }


@dataclass(frozen=True, slots=True)
class LedgerRecallContext:
    """Freshly resolved JSON data for one selected memory lane.

    The data remains separate from a system prompt.  It is intentionally not
    retained in ``LedgerRecallPlan`` so a forgotten record is not copied into a
    durable planning receipt before the caller resolves it.
    """

    schema_version: int
    policy_id: str
    used_tokens: int
    used_bytes: int
    token_budget: int
    byte_budget: int
    record_count: int
    _rendered_data: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported ledger recall context schema version")
        object.__setattr__(
            self,
            "policy_id",
            validate_identifier(self.policy_id, field="policy_id"),
        )
        for field_name in (
            "used_tokens",
            "used_bytes",
            "token_budget",
            "byte_budget",
            "record_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.used_tokens > self.token_budget or self.used_bytes > self.byte_budget:
            raise ValueError("ledger recall context exceeds its configured budget")
        if not isinstance(self._rendered_data, str):
            raise TypeError("rendered data must be a string")

    @property
    def remaining_tokens(self) -> int:
        """Return the remaining ledger allocation in tokens."""

        return self.token_budget - self.used_tokens

    @property
    def remaining_bytes(self) -> int:
        """Return the remaining ledger allocation in UTF-8 bytes."""

        return self.byte_budget - self.used_bytes

    def render_data(self) -> str:
        """Return the JSON data envelope for a separately trusted data lane."""

        return self._rendered_data

    def explain(self) -> dict[str, object]:
        """Return a content-free render receipt."""

        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "token_budget": self.token_budget,
            "byte_budget": self.byte_budget,
            "used_tokens": self.used_tokens,
            "used_bytes": self.used_bytes,
            "remaining_tokens": self.remaining_tokens,
            "remaining_bytes": self.remaining_bytes,
            "record_count": self.record_count,
        }


@dataclass(frozen=True, slots=True)
class LedgerRecallCheckpoint:
    """Content-free receipt for one durable strict-recall checkpoint.

    The host retains ``checkpoint_id`` and ``continuation_ref`` as opaque
    identifiers.  They deliberately do not appear in :meth:`explain`, which
    also excludes scope, task, selected record IDs/hashes, and memory payload.
    A checkpoint is not a portable :class:`LedgerRecallPlan`: resume always
    produces a fresh plan under the current Ledger lifecycle boundary.
    """

    schema_version: int
    policy_id: str
    policy_fingerprint: str
    counter_id: str
    token_budget: int
    byte_budget: int
    used_tokens: int
    used_bytes: int
    selected_count: int
    created_at: datetime
    _checkpoint_id: str = field(repr=False)
    _continuation_ref: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported ledger recall checkpoint schema version")
        for field_name in ("policy_id", "counter_id"):
            object.__setattr__(
                self,
                field_name,
                validate_identifier(getattr(self, field_name), field=field_name),
            )
        for field_name in ("policy_fingerprint",):
            value = getattr(self, field_name)
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError(f"{field_name} must be a 64-character digest")
        for field_name in (
            "token_budget",
            "byte_budget",
            "used_tokens",
            "used_bytes",
            "selected_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.used_tokens > self.token_budget or self.used_bytes > self.byte_budget:
            raise ValueError("checkpoint receipt exceeds its configured budget")
        created_at = coerce_datetime(self.created_at, field="created_at")
        assert created_at is not None
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(
            self,
            "_checkpoint_id",
            validate_identifier(self._checkpoint_id, field="checkpoint_id"),
        )
        object.__setattr__(
            self,
            "_continuation_ref",
            validate_identifier(self._continuation_ref, field="continuation_ref"),
        )

    @property
    def checkpoint_id(self) -> str:
        """Return the host-owned opaque identifier for this checkpoint."""

        return self._checkpoint_id

    @property
    def continuation_ref(self) -> str:
        """Return the opaque host reference to continuation state, if any."""

        return self._continuation_ref

    def explain(self) -> dict[str, object]:
        """Return a receipt without checkpoint identity or retained payload."""

        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_fingerprint": self.policy_fingerprint,
            "counter_id": self.counter_id,
            "token_budget": self.token_budget,
            "byte_budget": self.byte_budget,
            "used_tokens": self.used_tokens,
            "used_bytes": self.used_bytes,
            "selected_count": self.selected_count,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
        }


@dataclass(frozen=True, slots=True)
class LedgerRecallResume:
    """One fresh, planner-owned revalidation of a durable checkpoint.

    Only the matching :class:`LedgerRecallPlanner` may turn this result into a
    provider request.  The private plan is intentionally not a portable resume
    artifact; it is a normal process-local plan freshly created after loading
    and verifying the durable checkpoint manifest.
    """

    checkpoint: LedgerRecallCheckpoint
    _recall_plan: LedgerRecallPlan = field(repr=False, compare=False)
    _owner_token: object | None = field(repr=False, compare=False, default=None)
    _task_integrity_tag: str = field(repr=False, compare=False, default="")

    def __post_init__(self) -> None:
        if not isinstance(self.checkpoint, LedgerRecallCheckpoint):
            raise TypeError("checkpoint must be a LedgerRecallCheckpoint")
        if not isinstance(self._recall_plan, LedgerRecallPlan):
            raise TypeError("resume requires a fresh LedgerRecallPlan")
        if self._owner_token is None:
            raise ValueError("resume requires a private planner owner token")
        if (
            not isinstance(self._task_integrity_tag, str)
            or len(self._task_integrity_tag) != 64
            or any(character not in "0123456789abcdef" for character in self._task_integrity_tag)
        ):
            raise ValueError("resume requires a private task integrity tag")

    @property
    def continuation_ref(self) -> str:
        """Return the detached opaque host continuation reference."""

        return self.checkpoint.continuation_ref

    def explain(self) -> dict[str, object]:
        """Return content-free checkpoint and fresh-plan receipts."""

        return {
            "checkpoint": self.checkpoint.explain(),
            "ledger_recall": self._recall_plan.explain(),
        }
