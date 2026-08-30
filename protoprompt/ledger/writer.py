"""Host-owned facade for the experimental memory ledger.

The important constraint is structural: none of the mutating methods accept a
tenant, user, thread, trust level or lifecycle event supplied by a model.  A
host creates one writer for one scope and explicitly confirms candidates after
its own policy/review step.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable

from protoprompt.ledger.sqlite import SqliteMemoryLedger
from protoprompt.ledger.types import (
    ErasureReceipt,
    MemoryKind,
    MemoryRecord,
    scope_dict,
    utc_now,
    validate_identifier,
)
from protoprompt.scope import MemoryScope


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

    def events(self, record_id: str):
        """Return content-free lifecycle receipts for one record."""
        return self._ledger.events(self._scope, record_id)

    def export(self, *, include_content: bool = False) -> dict:
        """Create a deliberate local export; plaintext remains opt-in."""
        return self._ledger.export(self._scope, include_content=include_content)
