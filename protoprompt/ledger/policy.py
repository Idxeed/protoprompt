"""Versioned composition contract for an admitted durable-memory lane.

``MemoryAdmissionPolicy`` answers whether a concrete host-owned ingress may
be confirmed. ``LedgerRecallPolicy`` answers how already active records are
selected. They intentionally remain distinct: treating one as an alias for
the other would make an admission decision look like a retrieval decision.

``MemoryPolicy`` is the small v1-candidate contract that binds those two
independent policy phases without adding a workflow, model, or automatic
ingress. A host still creates a :class:`MemoryReviewGate` and a
``LedgerRecallPlanner`` explicitly, passing the matching components from one
immutable policy instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from protoprompt.ledger.admission import MemoryAdmissionPolicy
from protoprompt.ledger.recall.policy import LedgerRecallPolicy
from protoprompt.ledger.types import MemoryKind, MemoryOrigin, command_hash, validate_identifier


MEMORY_POLICY_SCHEMA_VERSION = 1
_UNVERIFIED_ORIGINS = frozenset(
    {
        MemoryOrigin.UNKNOWN,
        MemoryOrigin.LEGACY_UNKNOWN,
    }
)


class MemoryPolicyError(ValueError):
    """Raised when admission and recall components cannot form one contract."""


def _safe_admission_policy() -> MemoryAdmissionPolicy:
    """Return the one default admission component for :class:`MemoryPolicy`."""

    return MemoryAdmissionPolicy.safe_default()


def _safe_recall_policy() -> LedgerRecallPolicy:
    """Return the matching audited-only recall component.

    This is intentionally stricter than ``LedgerRecallPolicy.safe_default``:
    the latter preserves an experimental compatibility mode that can read
    legacy records without admission evidence. A composed ``MemoryPolicy``
    never silently adopts that compatibility lane.
    """

    return LedgerRecallPolicy(
        policy_id="memory-policy-safe-recall-v1",
        allowed_kinds=(
            MemoryKind.FACT,
            MemoryKind.DECISION,
            MemoryKind.PREFERENCE,
        ),
        allowed_origins=(MemoryOrigin.HOST_ASSERTION,),
        minimum_confidence=0.75,
        require_admission_audit=True,
    )


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    """One explicit, content-free contract across admission and recall.

    The constructor rejects a recall component that could select a record the
    paired admission component would not allow. This gives an integration one
    reviewable policy identity without broadening authority: neither component
    performs a write, retrieval, model call, or provider composition by
    itself.

    Existing standalone ``MemoryAdmissionPolicy`` and ``LedgerRecallPolicy``
    usages remain supported as experimental compatibility APIs. This wrapper
    is additive and is the candidate stable policy surface for the v1 API
    freeze.
    """

    schema_version: int = MEMORY_POLICY_SCHEMA_VERSION
    policy_id: str = "memory-policy-safe-v1"
    policy_version: str = "1"
    admission: MemoryAdmissionPolicy = field(default_factory=_safe_admission_policy)
    recall: LedgerRecallPolicy = field(default_factory=_safe_recall_policy)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != MEMORY_POLICY_SCHEMA_VERSION
        ):
            raise MemoryPolicyError("unsupported memory policy schema version")
        object.__setattr__(
            self,
            "policy_id",
            validate_identifier(self.policy_id, field="policy_id"),
        )
        object.__setattr__(
            self,
            "policy_version",
            validate_identifier(self.policy_version, field="policy_version"),
        )
        if not isinstance(self.admission, MemoryAdmissionPolicy):
            raise TypeError("admission must be a MemoryAdmissionPolicy")
        if not isinstance(self.recall, LedgerRecallPolicy):
            raise TypeError("recall must be a LedgerRecallPolicy")
        if not self.recall.require_admission_audit:
            raise MemoryPolicyError(
                "recall must require immutable admission audit evidence"
            )
        if self.recall.allowed_origins is None:
            raise MemoryPolicyError(
                "recall must declare concrete origins instead of legacy compatibility recall"
            )

        admission_origins = set(self.admission.allowed_origins)
        recall_origins = set(self.recall.allowed_origins)
        if recall_origins.intersection(_UNVERIFIED_ORIGINS):
            raise MemoryPolicyError(
                "recall allowed_origins must exclude unknown and legacy_unknown origins"
            )
        if not recall_origins.issubset(admission_origins):
            raise MemoryPolicyError(
                "recall allowed_origins must be a subset of admission allowed_origins"
            )
        if not set(self.recall.allowed_kinds).issubset(self.admission.allowed_kinds):
            raise MemoryPolicyError(
                "recall allowed_kinds must be a subset of admission allowed_kinds"
            )
        if self.recall.minimum_confidence < self.admission.minimum_confidence:
            raise MemoryPolicyError(
                "recall minimum_confidence must not be lower than admission minimum_confidence"
            )

    @classmethod
    def safe_default(cls) -> "MemoryPolicy":
        """Return the strict host-assertion default.

        It does not ingest data automatically. A trusted host must still pin
        its ingress origin and create a review gate before confirmation.
        """

        return cls()

    @property
    def fingerprint(self) -> str:
        """Return a deterministic content-free identity for this exact policy."""

        return command_hash(self._shape())

    def explain(self) -> dict[str, Any]:
        """Return a fresh, JSON-safe policy receipt without memory payloads."""

        result = self._shape()
        result["fingerprint"] = self.fingerprint
        return result

    def _shape(self) -> dict[str, Any]:
        """Return the canonical pre-fingerprint public policy representation."""

        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "admission": self.admission.explain(),
            "recall": self.recall.explain(),
        }


__all__ = [
    "MEMORY_POLICY_SCHEMA_VERSION",
    "MemoryPolicy",
    "MemoryPolicyError",
]
