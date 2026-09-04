"""Deterministic, local policy for experimental ledger recall planning."""

from __future__ import annotations

from dataclasses import dataclass
import math

from protoprompt.ledger.types import MemoryKind, MemoryOrigin, validate_identifier


_DEFAULT_KINDS = (
    MemoryKind.FACT,
    MemoryKind.DECISION,
    MemoryKind.PREFERENCE,
)

# A recall plan remains deliberately bounded even when a host elects to scan
# the full 10k evidence corpus.  Defaults stay conservative (1,000 active
# records / 100 candidates); 10k is an explicit policy choice for the raw
# performance protocol and any host that has budgeted for it.
MAX_ACTIVE_READ_LIMIT = 10_000
MAX_CANDIDATE_LIMIT = 10_000


def _finite_non_negative(value: float, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise TypeError(f"{field} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return normalized


def _positive_int(value: int, *, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 1 or (maximum is not None and value > maximum):
        suffix = f" from 1 to {maximum}" if maximum is not None else " at least 1"
        raise ValueError(f"{field} must be an integer{suffix}")
    return value


@dataclass(frozen=True, slots=True)
class LedgerRecallPolicy:
    """A versioned, deterministic policy for one local recall lane.

    This is a *selection* policy, not an admission policy.  It cannot confirm,
    mutate, or otherwise make a candidate recallable; the host-owned
    :class:`~protoprompt.ledger.MemoryWriter` lifecycle remains authoritative.
    """

    schema_version: int = 1
    policy_id: str = "ledger-recall-safe-v1"
    allowed_kinds: tuple[MemoryKind | str, ...] = _DEFAULT_KINDS
    minimum_confidence: float = 0.5
    active_read_limit: int = 1000
    candidate_limit: int = 100
    candidate_scan_byte_budget: int = 1_048_576
    relevance_weight: float = 100.0
    confidence_weight: float = 10.0
    recency_weight: float = 1.0
    require_admission_audit: bool = False
    # ``None`` deliberately preserves the v1 unrestricted-origin contract.
    # It also keeps existing policy receipts/fingerprints stable for durable
    # v6 checkpoint compatibility.  A concrete tuple is an explicit filter.
    allowed_origins: tuple[MemoryOrigin | str, ...] | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported ledger recall policy schema version")
        object.__setattr__(
            self,
            "policy_id",
            validate_identifier(self.policy_id, field="policy_id"),
        )
        kinds = tuple(MemoryKind(kind) for kind in self.allowed_kinds)
        if not kinds:
            raise ValueError("allowed_kinds must not be empty")
        if len(set(kinds)) != len(kinds):
            raise ValueError("allowed_kinds must not contain duplicates")
        object.__setattr__(self, "allowed_kinds", kinds)
        confidence = _finite_non_negative(
            self.minimum_confidence,
            field="minimum_confidence",
        )
        if confidence > 1.0:
            raise ValueError("minimum_confidence must be between 0 and 1")
        object.__setattr__(self, "minimum_confidence", confidence)
        object.__setattr__(
            self,
            "active_read_limit",
            _positive_int(
                self.active_read_limit,
                field="active_read_limit",
                maximum=MAX_ACTIVE_READ_LIMIT,
            ),
        )
        object.__setattr__(
            self,
            "candidate_limit",
            _positive_int(
                self.candidate_limit,
                field="candidate_limit",
                maximum=MAX_CANDIDATE_LIMIT,
            ),
        )
        if self.candidate_limit > self.active_read_limit:
            raise ValueError("candidate_limit must not exceed active_read_limit")
        object.__setattr__(
            self,
            "candidate_scan_byte_budget",
            _positive_int(
                self.candidate_scan_byte_budget,
                field="candidate_scan_byte_budget",
            ),
        )
        for field in ("relevance_weight", "confidence_weight", "recency_weight"):
            object.__setattr__(
                self,
                field,
                _finite_non_negative(getattr(self, field), field=field),
            )
        if (
            self.relevance_weight == 0
            and self.confidence_weight == 0
            and self.recency_weight == 0
        ):
            raise ValueError("at least one recall ranking weight must be non-zero")
        if not isinstance(self.require_admission_audit, bool):
            raise TypeError("require_admission_audit must be a bool")
        if self.allowed_origins is not None:
            origins = tuple(MemoryOrigin(origin) for origin in self.allowed_origins)
            if not origins:
                raise ValueError("allowed_origins must not be empty when configured")
            if len(set(origins)) != len(origins):
                raise ValueError("allowed_origins must not contain duplicates")
            object.__setattr__(self, "allowed_origins", origins)

    @classmethod
    def safe_default(cls) -> "LedgerRecallPolicy":
        """Return the conservative v1 policy.

        Episodes and procedures are deliberately excluded until their separate
        provenance and host-review contracts are available.
        """

        return cls()

    @classmethod
    def admission_safe_default(cls) -> "LedgerRecallPolicy":
        """Return the composition-safe policy for explicitly admitted memory.

        This leaves the v0.9 compatibility default unchanged: raw trusted-host
        and migrated legacy records can still use the standalone experimental
        recall lane. A request-composition host must opt into this stricter
        policy, which excludes ``unknown`` and ``legacy_unknown`` origins.
        Concrete origins are then checked by the Ledger's audited active-read
        invariant before a record reaches this planner.
        """

        return cls(
            policy_id="ledger-recall-admission-safe-v1",
            require_admission_audit=True,
        )

    @classmethod
    def task_resume_safe_default(cls) -> "LedgerRecallPolicy":
        """Return the strict host-confirmed Episode resume policy.

        This remains a bounded Ledger-memory selection contract, not a
        workflow checkpoint.  It selects only concrete host assertions for
        individual task episodes that already passed the immutable
        admission-review path; generic recall defaults remain unchanged.

        Procedures intentionally require a separate dependency- and
        conflict-aware planner.  Including them here would make a generic
        lexical selector look like a workflow executor.
        """

        return cls(
            policy_id="ledger-recall-task-resume-safe-v1",
            allowed_kinds=(MemoryKind.EPISODE,),
            allowed_origins=(MemoryOrigin.HOST_ASSERTION,),
            minimum_confidence=0.75,
            require_admission_audit=True,
        )

    def explain(self) -> dict[str, object]:
        """Return the public, content-free policy shape."""

        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "allowed_kinds": [kind.value for kind in self.allowed_kinds],
            "minimum_confidence": self.minimum_confidence,
            "active_read_limit": self.active_read_limit,
            "candidate_limit": self.candidate_limit,
            "candidate_scan_byte_budget": self.candidate_scan_byte_budget,
            "relevance_weight": self.relevance_weight,
            "confidence_weight": self.confidence_weight,
            "recency_weight": self.recency_weight,
            "require_admission_audit": self.require_admission_audit,
        }
        # Do not add a no-op field to unrestricted v1 policies: their exact
        # explain payload is fingerprinted by existing sealed checkpoints.
        if self.allowed_origins is not None:
            result["allowed_origins"] = [origin.value for origin in self.allowed_origins]
        return result
