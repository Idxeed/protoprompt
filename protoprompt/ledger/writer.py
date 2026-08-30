"""Host-owned facade for the experimental memory ledger.

The important constraint is structural: none of the mutating methods accept a
tenant, user, thread, trust level or lifecycle event supplied by a model.  A
host creates one writer for one scope and explicitly confirms candidates after
its own policy/review step.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Callable, Iterable

from protoprompt.ledger.sqlite import SqliteMemoryLedger
from protoprompt.ledger.types import (
    ErasureReceipt,
    MemoryAdmissionAudit,
    MemoryOrigin,
    MemoryKind,
    MemoryRecord,
    coerce_datetime,
    scope_dict,
    utc_now,
    validate_identifier,
)
from protoprompt.scope import MemoryScope

if TYPE_CHECKING:
    from protoprompt.ledger.admission import MemoryReview


def _references(values: Iterable[str], *, field: str) -> tuple[str, ...]:
    """Snapshot iterable references without turning one raw string into chars."""
    if isinstance(values, str):
        raise TypeError(f"{field} must be an iterable of opaque IDs, not a string")
    return tuple(values)


class MemoryWriter:
    """Scope-pinned, host-confirmed mutator and safe active-memory reader."""

    def __init__(
        self,
        ledger: SqliteMemoryLedger,
        *,
        scope: MemoryScope,
        actor: str = "host",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(ledger, SqliteMemoryLedger):
            raise TypeError("ledger must be a SqliteMemoryLedger")
        scope_dict(scope)
        self._ledger = ledger
        self._scope = scope
        self._actor = validate_identifier(actor, field="actor")
        self._clock = clock or utc_now

    @property
    def scope(self) -> MemoryScope:
        """Return the immutable host-provided scope for this writer."""
        return self._scope

    def propose(
        self,
        *,
        kind: MemoryKind | str,
        content: str,
        source_ref: str,
        evidence_refs: Iterable[str] = (),
        confidence: float = 0.5,
        record_id: str | None = None,
        retention_policy: str = "default",
        valid_from: datetime | str | None = None,
        valid_until: datetime | str | None = None,
        event_id: str | None = None,
    ) -> MemoryRecord:
        """Create an untrusted candidate from user/model/tool/PDF input."""
        return self._ledger.observe(
            self._scope,
            kind=kind,
            content=content,
            source_ref=source_ref,
            evidence_refs=_references(evidence_refs, field="evidence_refs"),
            confidence=confidence,
            record_id=record_id,
            retention_policy=retention_policy,
            valid_from=valid_from,
            valid_until=valid_until,
            actor=self._actor,
            event_id=event_id,
            occurred_at=self._clock(),
        )

    def _propose_with_origin(
        self,
        *,
        origin: MemoryOrigin,
        kind: MemoryKind | str,
        content: str,
        source_ref: str,
        evidence_refs: Iterable[str] = (),
        confidence: float = 0.5,
        record_id: str | None = None,
        retention_policy: str = "default",
        valid_from: datetime | str | None = None,
        valid_until: datetime | str | None = None,
        event_id: str | None = None,
    ) -> MemoryRecord:
        """Internal origin-pinned ingress used by ``MemoryReviewGate`` only."""

        return self._ledger._observe_with_origin(
            self._scope,
            origin=origin,
            kind=kind,
            content=content,
            source_ref=source_ref,
            evidence_refs=_references(evidence_refs, field="evidence_refs"),
            confidence=confidence,
            record_id=record_id,
            retention_policy=retention_policy,
            valid_from=valid_from,
            valid_until=valid_until,
            actor=self._actor,
            event_id=event_id,
            occurred_at=self._clock(),
        )

    def assert_candidate(
        self,
        *,
        kind: MemoryKind | str,
        content: str,
        source_ref: str,
        evidence_refs: Iterable[str] = (),
        confidence: float = 0.5,
        record_id: str | None = None,
        retention_policy: str = "default",
        valid_from: datetime | str | None = None,
        valid_until: datetime | str | None = None,
        event_id: str | None = None,
    ) -> MemoryRecord:
        """Create a host assertion that still requires explicit confirmation."""
        return self._ledger.assert_candidate(
            self._scope,
            kind=kind,
            content=content,
            source_ref=source_ref,
            evidence_refs=_references(evidence_refs, field="evidence_refs"),
            confidence=confidence,
            record_id=record_id,
            retention_policy=retention_policy,
            valid_from=valid_from,
            valid_until=valid_until,
            actor=self._actor,
            event_id=event_id,
            occurred_at=self._clock(),
        )

    def _assert_candidate_with_origin(
        self,
        *,
        origin: MemoryOrigin,
        kind: MemoryKind | str,
        content: str,
        source_ref: str,
        evidence_refs: Iterable[str] = (),
        confidence: float = 0.5,
        record_id: str | None = None,
        retention_policy: str = "default",
        valid_from: datetime | str | None = None,
        valid_until: datetime | str | None = None,
        event_id: str | None = None,
    ) -> MemoryRecord:
        """Internal origin-pinned assertion used by ``MemoryReviewGate`` only."""

        return self._ledger._assert_candidate_with_origin(
            self._scope,
            origin=origin,
            kind=kind,
            content=content,
            source_ref=source_ref,
            evidence_refs=_references(evidence_refs, field="evidence_refs"),
            confidence=confidence,
            record_id=record_id,
            retention_policy=retention_policy,
            valid_from=valid_from,
            valid_until=valid_until,
            actor=self._actor,
            event_id=event_id,
            occurred_at=self._clock(),
        )

    def confirm(
        self,
        record_id: str,
        *,
        expected_revision: int,
        event_id: str | None = None,
    ) -> MemoryRecord:
        """Host-confirm one reviewed candidate for active recall eligibility."""
        return self._ledger.confirm(
            self._scope,
            record_id,
            expected_revision=expected_revision,
            actor=self._actor,
            event_id=event_id,
            occurred_at=self._clock(),
        )

    def quarantine(
        self,
        record_id: str,
        *,
        expected_revision: int,
        reason_code: str,
        event_id: str | None = None,
    ) -> MemoryRecord:
        """Exclude a potentially unsafe record pending review."""
        return self._ledger.quarantine(
            self._scope,
            record_id,
            expected_revision=expected_revision,
            reason_code=reason_code,
            actor=self._actor,
            event_id=event_id,
            occurred_at=self._clock(),
        )

    def expire(
        self,
        record_id: str,
        *,
        expected_revision: int,
        event_id: str | None = None,
    ) -> MemoryRecord:
        """Explicitly expire a candidate or active record."""
        return self._ledger.expire(
            self._scope,
            record_id,
            expected_revision=expected_revision,
            actor=self._actor,
            event_id=event_id,
            occurred_at=self._clock(),
        )

    def expire_due(self, *, now: datetime | str | None = None) -> list[MemoryRecord]:
        """Expire all records whose validity ended at or before ``now``."""
        return self._ledger.expire_due(
            self._scope,
            now=self._clock() if now is None else now,
            actor=self._actor,
        )

    def supersede(
        self,
        record_id: str,
        *,
        replacement_record_id: str,
        expected_revision: int,
        expected_replacement_revision: int,
        event_id: str | None = None,
    ) -> MemoryRecord:
        """Replace one active record with a separately confirmed one."""
        return self._ledger.supersede(
            self._scope,
            record_id,
            replacement_record_id=replacement_record_id,
            expected_revision=expected_revision,
            expected_replacement_revision=expected_replacement_revision,
            actor=self._actor,
            event_id=event_id,
            occurred_at=self._clock(),
        )

    def retract(
        self,
        record_id: str,
        *,
        expected_revision: int,
        reason_code: str,
        event_id: str | None = None,
    ) -> MemoryRecord:
        """Exclude a record while retaining its payload for local review."""
        return self._ledger.retract(
            self._scope,
            record_id,
            expected_revision=expected_revision,
            reason_code=reason_code,
            actor=self._actor,
            event_id=event_id,
            occurred_at=self._clock(),
        )

    def forget(
        self,
        record_id: str,
        *,
        expected_revision: int,
        reason_code: str = "user_requested",
        event_id: str | None = None,
    ) -> ErasureReceipt:
        """Exclude and remove local plaintext/source payload for one record."""
        return self._ledger.forget(
            self._scope,
            record_id,
            expected_revision=expected_revision,
            reason_code=reason_code,
            actor=self._actor,
            event_id=event_id,
            occurred_at=self._clock(),
        )

    def forget_by_source(
        self,
        source_ref: str,
        *,
        reason_code: str = "source_revoked",
    ) -> list[ErasureReceipt]:
        """Atomically forget and permanently revoke one scoped opaque source."""
        return self._ledger.forget_by_source(
            self._scope,
            source_ref,
            reason_code=reason_code,
            actor=self._actor,
            occurred_at=self._clock(),
        )

    def erase(
        self,
        record_id: str,
        *,
        expected_revision: int,
        event_id: str | None = None,
    ) -> ErasureReceipt:
        """Hard-delete live local rows and scrub dependent event references."""
        return self._ledger.erase(
            self._scope,
            record_id,
            expected_revision=expected_revision,
            event_id=event_id,
        )

    def get(self, record_id: str) -> MemoryRecord | None:
        """Read one record in the writer's scope, including non-recallable states."""
        return self._ledger.get(self._scope, record_id)

    def list_active(
        self,
        *,
        now: datetime | str | None = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        """Read only active, valid and payload-present records."""
        return self._ledger.list_active(
            self._scope,
            now=self._clock() if now is None else now,
            limit=limit,
        )

    def _validate_active_snapshot(
        self,
        *,
        now: datetime | str | None,
        limit: int,
        selections: Iterable[tuple[str, int, str, MemoryKind]],
    ) -> bool:
        """Validate private recall markers under one short ledger boundary.

        This is reserved for ledger-owned read paths that need a final
        lifecycle linearization point after rendering outside the SQLite write
        lock. Model/plugin code should receive neither this helper nor the
        underlying writer.
        """

        return self._ledger._validate_active_snapshot(
            self._scope,
            now=self._clock() if now is None else now,
            limit=limit,
            selections=selections,
        )

    def _create_recall_checkpoint(
        self,
        *,
        checkpoint_id: str,
        continuation_ref: str,
        policy_id: str,
        policy_fingerprint: str,
        counter_id: str,
        token_budget: int,
        byte_budget: int,
        used_tokens: int,
        used_bytes: int,
        selections: Iterable[tuple[str, int, str, MemoryKind]],
        active_read_limit: int,
        created_at: datetime,
        integrity_tag: str,
    ) -> dict[str, object]:
        """Persist one private recall snapshot under the writer's scope.

        This capability is reserved for the matching strict recall planner.
        It accepts only opaque metadata and selection markers; no plaintext
        Ledger payload or provider request crosses this boundary.
        """

        return self._ledger._create_recall_checkpoint(
            self._scope,
            checkpoint_id=checkpoint_id,
            continuation_ref=continuation_ref,
            policy_id=policy_id,
            policy_fingerprint=policy_fingerprint,
            counter_id=counter_id,
            token_budget=token_budget,
            byte_budget=byte_budget,
            used_tokens=used_tokens,
            used_bytes=used_bytes,
            selections=selections,
            active_read_limit=active_read_limit,
            created_at=created_at,
            integrity_tag=integrity_tag,
        )

    def _load_recall_checkpoint(self, checkpoint_id: str) -> dict[str, object]:
        """Load one private durable checkpoint manifest from the pinned scope."""

        return self._ledger._load_recall_checkpoint(self._scope, checkpoint_id)

    def _invalidate_recall_checkpoint(
        self,
        checkpoint_id: str,
        *,
        reason_code: str,
        occurred_at: datetime,
    ) -> bool:
        """Fail-close one checkpoint and remove its derived selection metadata."""

        return self._ledger._invalidate_recall_checkpoint(
            self._scope,
            checkpoint_id,
            reason_code=reason_code,
            occurred_at=occurred_at,
        )

    def _apply_admission_review(
        self,
        *,
        review: "MemoryReview",
        event_id: str,
        occurred_at: datetime,
    ) -> MemoryRecord | ErasureReceipt:
        """Apply one gate-validated review inside the Ledger's write boundary.

        ``occurred_at`` is sampled by the owning gate before it asks SQLite for
        its write lock.  This preserves a host's injected clock without ever
        invoking host code while ``BEGIN IMMEDIATE`` is held.
        """

        return self._ledger._apply_admission_review(
            self._scope,
            record_id=review._record_id,
            expected_revision=review._candidate_revision,
            expected_content_hash=review._content_hash,
            origin=review._origin,
            policy_id=review.policy_id,
            policy_version=review.policy_version,
            policy_fingerprint=review.policy_fingerprint,
            action=review.action,
            reason_code=review.reason_code,
            actor=review._reviewer_actor,
            event_id=event_id,
            occurred_at=occurred_at,
        )

    def _sample_admission_timestamp(self) -> datetime:
        """Read the host clock before an admission transaction begins.

        This private helper intentionally keeps arbitrary host callbacks out
        of the Ledger's write transaction.  It also makes admission actions
        use the same deterministic clock as the writer's ordinary lifecycle
        operations.
        """

        timestamp = coerce_datetime(self._clock(), field="clock")
        assert timestamp is not None
        return timestamp

    def events(self, record_id: str):
        """Return content-free lifecycle receipts for one record."""
        return self._ledger.events(self._scope, record_id)

    def admission_audits(self, record_id: str) -> list[MemoryAdmissionAudit]:
        """Return content-free admission receipts for one record."""

        return self._ledger.admission_audits(self._scope, record_id)

    def export(self, *, include_content: bool = False) -> dict:
        """Create a deliberate local export; plaintext remains opt-in."""
        return self._ledger.export(self._scope, include_content=include_content)
