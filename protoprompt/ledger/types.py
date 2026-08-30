"""Typed, scope-pinned primitives for the experimental memory ledger.

The ledger deliberately separates a record's operational history from its
plaintext payload. Events contain only opaque identifiers, lifecycle changes,
and metadata-only command fingerprints; the payload can therefore be removed
on a real ``forget`` request without leaving a second copy in an append-only
event log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any, Iterable, Mapping

from protoprompt.scope import MemoryScope


LEDGER_SCHEMA_VERSION = 1
MAX_CONTENT_CHARS = 16_000
MAX_REFERENCE_COUNT = 32
MAX_REFERENCE_CHARS = 512
MAX_IDENTIFIER_CHARS = 128


class LedgerError(RuntimeError):
    """Base exception for ledger failures."""


class LedgerNotReadyError(LedgerError):
    """Raised when an explicit ledger setup has not been performed."""


class LedgerConflictError(LedgerError):
    """Raised when an idempotency key or optimistic revision conflicts."""


class LedgerStateError(LedgerError):
    """Raised when a requested lifecycle transition is not allowed."""


class MemoryKind(StrEnum):
    """Semantic class of a durable memory record."""

    FACT = "fact"
    DECISION = "decision"
    PREFERENCE = "preference"
    EPISODE = "episode"
    PROCEDURE = "procedure"


class MemoryState(StrEnum):
    """Lifecycle state used by the materialized record view."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    EXPIRED = "expired"
    QUARANTINED = "quarantined"


class MemoryTrust(StrEnum):
    """Trust is assigned by the host, never by ingestion text."""

    UNTRUSTED = "untrusted"
    HOST_CONFIRMED = "host_confirmed"


class MemoryOrigin(StrEnum):
    """Closed ingress category assigned by trusted host code.

    Origin describes where a candidate entered the Ledger; it is deliberately
    separate from ``MemoryTrust`` and never promotes a record by itself.
    ``UNKNOWN`` is used by the low-level trusted writer escape hatch, while
    ``LEGACY_UNKNOWN`` is reserved for payload-bearing records migrated from
    pre-admission Ledger schemas.
    """

    UNKNOWN = "unknown"
    LEGACY_UNKNOWN = "legacy_unknown"
    USER_INPUT = "user_input"
    DOCUMENT = "document"
    TOOL_OUTPUT = "tool_output"
    MODEL_EXTRACTION = "model_extraction"
    HOST_ASSERTION = "host_assertion"


class MemoryAdmissionAction(StrEnum):
    """One host policy outcome for an untrusted Ledger candidate."""

    ALLOW = "allow"
    QUARANTINE = "quarantine"
    REJECT = "reject"


class MemoryEventType(StrEnum):
    """Append-only lifecycle event names."""

    OBSERVED = "observed"
    ASSERTED = "asserted"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    FORGOTTEN = "forgotten"
    EXPIRED = "expired"
    QUARANTINED = "quarantined"


class MemoryRelationType(StrEnum):
    """Relations reserved for the first ledger schema."""

    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived_from"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


def utc_now() -> datetime:
    """Return an aware UTC timestamp with microsecond precision."""
    return datetime.now(timezone.utc)


def coerce_datetime(value: datetime | str | None, *, field: str) -> datetime | None:
    """Parse one optional ISO-8601 timestamp into aware UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime, ISO-8601 string, or None")
    if value.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    """Serialize one aware timestamp into the stable SQLite representation."""
    normalized = coerce_datetime(value, field="timestamp")
    assert normalized is not None
    return normalized.isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str, *, field: str) -> datetime:
    """Parse a timestamp previously written by :func:`format_timestamp`."""
    parsed = coerce_datetime(value, field=field)
    assert parsed is not None
    return parsed


def validate_identifier(value: str, *, field: str = "identifier") -> str:
    """Validate a host-generated opaque identifier.

    Identifiers intentionally allow common opaque forms such as ``turn:42``
    and UUIDs, but reject whitespace/control characters so raw text is not
    accidentally placed into lifecycle metadata.
    """
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > MAX_IDENTIFIER_CHARS:
        raise ValueError(f"{field} must be at most {MAX_IDENTIFIER_CHARS} characters")
    if any(char.isspace() or ord(char) < 32 for char in normalized):
        raise ValueError(f"{field} must be an opaque identifier without whitespace")
    return normalized


def validate_reference(value: str, *, field: str = "reference") -> str:
    """Validate one host-minted opaque source or evidence reference."""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > MAX_REFERENCE_CHARS:
        raise ValueError(f"{field} must be at most {MAX_REFERENCE_CHARS} characters")
    if any(char.isspace() or ord(char) < 32 for char in normalized):
        raise ValueError(f"{field} must be an opaque identifier without whitespace")
    return normalized


def validate_references(
    values: Iterable[str] | None,
    *,
    field: str,
) -> tuple[str, ...]:
    """Validate and de-duplicate an ordered set of opaque references."""
    if values is None:
        return ()
    if isinstance(values, str):
        raise TypeError(f"{field} must be an iterable of strings, not a string")
    normalized = tuple(validate_reference(value, field=field) for value in values)
    if len(normalized) > MAX_REFERENCE_COUNT:
        raise ValueError(f"{field} supports at most {MAX_REFERENCE_COUNT} values")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} must not contain duplicates")
    return normalized


def validate_content(value: str) -> str:
    """Apply bounded payload validation before persistence."""
    if not isinstance(value, str):
        raise TypeError("content must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("content must not be empty")
    if len(normalized) > MAX_CONTENT_CHARS:
        raise ValueError(f"content must be at most {MAX_CONTENT_CHARS} characters")
    return normalized


def scope_dict(scope: MemoryScope) -> dict[str, str]:
    """Return the full canonical scope, including deliberately empty fields."""
    if not isinstance(scope, MemoryScope):
        raise TypeError("scope must be a MemoryScope")
    if scope.is_empty:
        raise ValueError("memory ledger requires a non-empty host MemoryScope")
    return {
        "tenant": scope.tenant,
        "user": scope.user,
        "thread": scope.thread,
        "kind": scope.kind,
    }


def canonical_json(value: Mapping[str, Any] | list[Any] | tuple[Any, ...]) -> str:
    """Render JSON in a deterministic form suitable for a command digest."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(scope: MemoryScope, content: str) -> str:
    """Return a scope-separated content fingerprint.

    It is an operational dedupe/provenance fingerprint, *not* a password hash
    or tamper-evident audit primitive.  It is intentionally never sent through
    telemetry by this module.
    """
    canonical_scope = canonical_json(scope_dict(scope)).encode("utf-8")
    key = hashlib.blake2b(
        canonical_scope,
        digest_size=32,
        person=b"pp-ledger-scope",
    ).digest()
    return hashlib.blake2b(
        validate_content(content).encode("utf-8"),
        key=key,
        digest_size=32,
        person=b"pp-ledger-data",
    ).hexdigest()


def command_hash(payload: Mapping[str, Any]) -> str:
    """Hash command metadata without persisting raw memory text in events."""
    return hashlib.blake2b(
        canonical_json(payload).encode("utf-8"),
        digest_size=32,
        person=b"pp-ledger-cmd",
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class MemoryRelation:
    """A typed relation from this record to another record in the same scope."""

    relation: MemoryRelationType
    record_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "relation", MemoryRelationType(self.relation))
        object.__setattr__(
            self,
            "record_id",
            validate_identifier(self.record_id, field="relation record_id"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"relation": self.relation.value, "record_id": self.record_id}


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One local lifecycle projection scoped to a host-owned namespace."""

    record_id: str
    scope: MemoryScope
    kind: MemoryKind
    state: MemoryState
    trust: MemoryTrust
    content: str | None
    content_hash: str
    source_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    confidence: float
    created_at: datetime
    updated_at: datetime
    valid_from: datetime | None
    valid_until: datetime | None
    retention_policy: str
    revision: int
    last_event_sequence: int
    relations: tuple[MemoryRelation, ...] = ()
    superseded_by: str | None = None
    retracted_at: datetime | None = None
    expired_at: datetime | None = None
    schema_version: int = LEDGER_SCHEMA_VERSION
    origin: MemoryOrigin | str = MemoryOrigin.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", validate_identifier(self.record_id, field="record_id"))
        scope_dict(self.scope)
        object.__setattr__(self, "kind", MemoryKind(self.kind))
        object.__setattr__(self, "state", MemoryState(self.state))
        object.__setattr__(self, "trust", MemoryTrust(self.trust))
        object.__setattr__(self, "origin", MemoryOrigin(self.origin))
        if (
            self.state is MemoryState.ACTIVE
            and self.trust is not MemoryTrust.HOST_CONFIRMED
        ):
            raise ValueError("an active memory record must be host_confirmed")
        if self.content is not None:
            object.__setattr__(self, "content", validate_content(self.content))
        if not isinstance(self.content_hash, str) or len(self.content_hash) != 64:
            raise ValueError("content_hash must be a 64-character operational marker")
        object.__setattr__(
            self,
            "source_refs",
            validate_references(self.source_refs, field="source_refs"),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            validate_references(self.evidence_refs, field="evidence_refs"),
        )
        if not isinstance(self.confidence, (float, int)) or not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", float(self.confidence))
        created_at = coerce_datetime(self.created_at, field="created_at")
        updated_at = coerce_datetime(self.updated_at, field="updated_at")
        valid_from = coerce_datetime(self.valid_from, field="valid_from")
        valid_until = coerce_datetime(self.valid_until, field="valid_until")
        retracted_at = coerce_datetime(self.retracted_at, field="retracted_at")
        expired_at = coerce_datetime(self.expired_at, field="expired_at")
        assert created_at is not None and updated_at is not None
        if updated_at < created_at:
            raise ValueError("updated_at must not be before created_at")
        if valid_from is not None and valid_until is not None and valid_until < valid_from:
            raise ValueError("valid_until must not be before valid_from")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(self, "retracted_at", retracted_at)
        object.__setattr__(self, "expired_at", expired_at)
        if not isinstance(self.retention_policy, str) or not self.retention_policy.strip():
            raise ValueError("retention_policy must be a non-empty string")
        if len(self.retention_policy) > 128 or any(
            char.isspace() or ord(char) < 32 for char in self.retention_policy
        ):
            raise ValueError("retention_policy must be a short opaque policy identifier")
        if self.revision < 1:
            raise ValueError("revision must be at least 1")
        if self.last_event_sequence < 1:
            raise ValueError("last_event_sequence must be at least 1")
        if self.schema_version != LEDGER_SCHEMA_VERSION:
            raise ValueError("unsupported memory record schema_version")
        relations = tuple(
            relation if isinstance(relation, MemoryRelation) else MemoryRelation(**relation)
            for relation in self.relations
        )
        if len({(relation.relation, relation.record_id) for relation in relations}) != len(relations):
            raise ValueError("relations must not contain duplicates")
        object.__setattr__(self, "relations", relations)
        if self.superseded_by is not None:
            object.__setattr__(
                self,
                "superseded_by",
                validate_identifier(self.superseded_by, field="superseded_by"),
            )

    @property
    def content_available(self) -> bool:
        """Whether plaintext remains available after lifecycle operations."""
        return self.content is not None

    def is_recallable(self, *, now: datetime | None = None) -> bool:
        """Return whether the default safe reader may surface this record."""
        instant = coerce_datetime(now, field="now") if now is not None else utc_now()
        assert instant is not None
        if (
            self.state is not MemoryState.ACTIVE
            or self.trust is not MemoryTrust.HOST_CONFIRMED
            or self.content is None
        ):
            return False
        if self.valid_from is not None and instant < self.valid_from:
            return False
        return self.valid_until is None or instant < self.valid_until

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        """Return an explicit-export representation, content opt-in by default."""
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "record_id": self.record_id,
            "scope": scope_dict(self.scope),
            "kind": self.kind.value,
            "state": self.state.value,
            "trust": self.trust.value,
            "origin": self.origin.value,
            "content_hash": self.content_hash,
            "content_available": self.content_available,
            "source_refs": list(self.source_refs),
            "evidence_refs": list(self.evidence_refs),
            "confidence": self.confidence,
            "created_at": format_timestamp(self.created_at),
            "updated_at": format_timestamp(self.updated_at),
            "valid_from": format_timestamp(self.valid_from) if self.valid_from else None,
            "valid_until": format_timestamp(self.valid_until) if self.valid_until else None,
            "retention_policy": self.retention_policy,
            "revision": self.revision,
            "last_event_sequence": self.last_event_sequence,
            "relations": [relation.to_dict() for relation in self.relations],
            "superseded_by": self.superseded_by,
            "retracted_at": format_timestamp(self.retracted_at) if self.retracted_at else None,
            "expired_at": format_timestamp(self.expired_at) if self.expired_at else None,
        }
        if include_content:
            data["content"] = self.content
        return data


@dataclass(frozen=True, slots=True)
class MemoryEvent:
    """Content-free lifecycle event in the operational history."""

    sequence: int
    event_id: str
    record_id: str
    scope: MemoryScope
    event_type: MemoryEventType
    occurred_at: datetime
    revision: int
    actor: str
    related_record_id: str | None = None
    reason_code: str | None = None
    schema_version: int = LEDGER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("sequence must be at least 1")
        object.__setattr__(self, "event_id", validate_identifier(self.event_id, field="event_id"))
        object.__setattr__(self, "record_id", validate_identifier(self.record_id, field="record_id"))
        scope_dict(self.scope)
        object.__setattr__(self, "event_type", MemoryEventType(self.event_type))
        occurred_at = coerce_datetime(self.occurred_at, field="occurred_at")
        assert occurred_at is not None
        object.__setattr__(self, "occurred_at", occurred_at)
        if self.revision < 1:
            raise ValueError("revision must be at least 1")
        object.__setattr__(self, "actor", validate_identifier(self.actor, field="actor"))
        if self.related_record_id is not None:
            object.__setattr__(
                self,
                "related_record_id",
                validate_identifier(self.related_record_id, field="related_record_id"),
            )
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                validate_identifier(self.reason_code, field="reason_code"),
            )
        if self.schema_version != LEDGER_SCHEMA_VERSION:
            raise ValueError("unsupported memory event schema_version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "record_id": self.record_id,
            "scope": scope_dict(self.scope),
            "event_type": self.event_type.value,
            "occurred_at": format_timestamp(self.occurred_at),
            "revision": self.revision,
            "actor": self.actor,
            "related_record_id": self.related_record_id,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class MemoryAdmissionAudit:
    """Content-free durable receipt for one applied admission decision.

    The companion audit deliberately does not duplicate candidate text,
    source/evidence references, content hashes, or a free-form policy
    explanation.  Its event ID is the same opaque host-created idempotency
    key used by the lifecycle transition it accompanies.
    """

    event_id: str
    record_id: str
    scope: MemoryScope
    candidate_revision: int
    origin: MemoryOrigin | str
    policy_id: str
    policy_version: str
    policy_fingerprint: str
    action: MemoryAdmissionAction | str
    reason_code: str
    occurred_at: datetime
    reviewer_actor: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", validate_identifier(self.event_id, field="event_id"))
        object.__setattr__(self, "record_id", validate_identifier(self.record_id, field="record_id"))
        scope_dict(self.scope)
        if isinstance(self.candidate_revision, bool) or not isinstance(
            self.candidate_revision, int
        ) or self.candidate_revision < 1:
            raise ValueError("candidate_revision must be a positive integer")
        object.__setattr__(self, "origin", MemoryOrigin(self.origin))
        object.__setattr__(self, "policy_id", validate_identifier(self.policy_id, field="policy_id"))
        object.__setattr__(
            self,
            "policy_version",
            validate_identifier(self.policy_version, field="policy_version"),
        )
        if not isinstance(self.policy_fingerprint, str) or len(self.policy_fingerprint) != 64:
            raise ValueError("policy_fingerprint must be a 64-character digest")
        object.__setattr__(self, "action", MemoryAdmissionAction(self.action))
        object.__setattr__(
            self,
            "reason_code",
            validate_identifier(self.reason_code, field="reason_code"),
        )
        occurred_at = coerce_datetime(self.occurred_at, field="occurred_at")
        assert occurred_at is not None
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(
            self,
            "reviewer_actor",
            validate_identifier(self.reviewer_actor, field="reviewer_actor"),
        )
        if self.schema_version != 1:
            raise ValueError("unsupported memory admission audit schema_version")

    def to_dict(self) -> dict[str, Any]:
        """Return the content-free audit representation."""

        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "record_id": self.record_id,
            "scope": scope_dict(self.scope),
            "candidate_revision": self.candidate_revision,
            "origin": self.origin.value,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_fingerprint": self.policy_fingerprint,
            "action": self.action.value,
            "reason_code": self.reason_code,
            "occurred_at": format_timestamp(self.occurred_at),
            "reviewer_actor": self.reviewer_actor,
        }


@dataclass(frozen=True, slots=True)
class ErasureReceipt:
    """Content-free result of a local scoped ``forget`` or ``erase`` action."""

    record_id: str
    state: MemoryState | None
    payload_deleted: bool
    source_refs_deleted: int
    relations_deleted: int
    events_deleted: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", validate_identifier(self.record_id, field="record_id"))
        if self.state is not None:
            object.__setattr__(self, "state", MemoryState(self.state))
        for field_name in ("source_refs_deleted", "relations_deleted", "events_deleted"):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "state": self.state.value if self.state is not None else None,
            "payload_deleted": self.payload_deleted,
            "source_refs_deleted": self.source_refs_deleted,
            "relations_deleted": self.relations_deleted,
            "events_deleted": self.events_deleted,
        }
