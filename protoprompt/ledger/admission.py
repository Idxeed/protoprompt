"""Host-owned admission policy and review capability for Ledger candidates.

The module deliberately does not expose a model tool.  A host gives an
integration a scope-pinned :class:`MemoryReviewGate` with a fixed ingress
origin and policy; untrusted text can become only a candidate.  ``review()``
is pure and does not mutate storage.  One of the action-specific methods then
revalidates a sealed in-process review under the Ledger write boundary before
performing the lifecycle transition and recording its content-free audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import math
import secrets
from typing import Iterable

from protoprompt.ledger.types import (
    ErasureReceipt,
    MemoryAdmissionAction,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    MemoryTrust,
    canonical_json,
    coerce_datetime,
    command_hash,
    utc_now,
    validate_identifier,
    validate_reference,
    validate_references,
)
from protoprompt.ledger.writer import MemoryWriter
from protoprompt.scope import MemoryScope


_POLICY_SCHEMA_VERSION = 1
_REVIEW_SCHEMA_VERSION = 1
_SAFE_KINDS = (
    MemoryKind.FACT,
    MemoryKind.DECISION,
    MemoryKind.PREFERENCE,
)


class MemoryAdmissionError(RuntimeError):
    """Base error for admission-policy and review capability failures."""


class MemoryAdmissionPolicyError(MemoryAdmissionError):
    """Raised when a host policy cannot produce a valid safe decision."""


class StaleMemoryReviewError(MemoryAdmissionError):
    """Raised when a reviewed candidate no longer matches its sealed snapshot."""


def _finite_confidence(value: float, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise TypeError(f"{field} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise ValueError(f"{field} must be between 0 and 1")
    return normalized


@dataclass(frozen=True, slots=True)
class MemoryAdmissionDecision:
    """One deterministic, content-free policy outcome for a candidate."""

    action: MemoryAdmissionAction | str
    reason_code: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", MemoryAdmissionAction(self.action))
        object.__setattr__(
            self,
            "reason_code",
            validate_identifier(self.reason_code, field="reason_code"),
        )

    def explain(self) -> dict[str, str]:
        """Return the safe, non-payload policy decision."""

        return {"action": self.action.value, "reason_code": self.reason_code}


@dataclass(frozen=True, slots=True)
class MemoryAdmissionPolicy:
    """A local, deterministic rule set for one admission gate.

    The policy has no network, retrieval, model, time, or write dependency.
    ``MemoryReviewGate`` still requires an explicit later action; an ``allow``
    decision never writes or upgrades trust by itself.  The constructor's
    conservative defaults quarantine every origin.  ``safe_default()`` is an
    explicit opt-in for host assertions only.
    """

    schema_version: int = _POLICY_SCHEMA_VERSION
    policy_id: str = "ledger-admission-quarantine-all-v1"
    policy_version: str = "1"
    allowed_origins: tuple[MemoryOrigin | str, ...] = ()
    rejected_origins: tuple[MemoryOrigin | str, ...] = ()
    allowed_kinds: tuple[MemoryKind | str, ...] = _SAFE_KINDS
    minimum_confidence: float = 1.0

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != _POLICY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported memory admission policy schema version")
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
        allowed_origins = tuple(MemoryOrigin(origin) for origin in self.allowed_origins)
        rejected_origins = tuple(MemoryOrigin(origin) for origin in self.rejected_origins)
        if len(set(allowed_origins)) != len(allowed_origins):
            raise ValueError("allowed_origins must not contain duplicates")
        if len(set(rejected_origins)) != len(rejected_origins):
            raise ValueError("rejected_origins must not contain duplicates")
        if set(allowed_origins).intersection(rejected_origins):
            raise ValueError("allowed_origins and rejected_origins must not overlap")
        object.__setattr__(self, "allowed_origins", allowed_origins)
        object.__setattr__(self, "rejected_origins", rejected_origins)
        kinds = tuple(MemoryKind(kind) for kind in self.allowed_kinds)
        if not kinds:
            raise ValueError("allowed_kinds must not be empty")
        if len(set(kinds)) != len(kinds):
            raise ValueError("allowed_kinds must not contain duplicates")
        object.__setattr__(self, "allowed_kinds", kinds)
        object.__setattr__(
            self,
            "minimum_confidence",
            _finite_confidence(self.minimum_confidence, field="minimum_confidence"),
        )

    @classmethod
    def safe_default(cls) -> "MemoryAdmissionPolicy":
        """Return the explicit conservative policy for host assertions.

        User input, documents, tool output, model extraction, raw writer
        candidates, and migrated records remain quarantined until a host opts
        into a more specific policy.
        """

        return cls(
            policy_id="ledger-admission-safe-v1",
            policy_version="1",
            allowed_origins=(MemoryOrigin.HOST_ASSERTION,),
            minimum_confidence=0.75,
        )

    def evaluate(self, candidate: MemoryRecord) -> MemoryAdmissionDecision:
        """Evaluate a candidate without mutating it or reading external state."""

        if not isinstance(candidate, MemoryRecord):
            raise TypeError("candidate must be a MemoryRecord")
        if candidate.origin in {MemoryOrigin.UNKNOWN, MemoryOrigin.LEGACY_UNKNOWN}:
            return MemoryAdmissionDecision(
                MemoryAdmissionAction.QUARANTINE,
                "origin_unverified",
            )
        if candidate.origin in self.rejected_origins:
            return MemoryAdmissionDecision(
                MemoryAdmissionAction.REJECT,
                "origin_rejected",
            )
        if candidate.origin not in self.allowed_origins:
            return MemoryAdmissionDecision(
                MemoryAdmissionAction.QUARANTINE,
                "origin_requires_review",
            )
        if candidate.kind not in self.allowed_kinds:
            return MemoryAdmissionDecision(
                MemoryAdmissionAction.QUARANTINE,
                "kind_requires_review",
            )
        if candidate.confidence < self.minimum_confidence:
            return MemoryAdmissionDecision(
                MemoryAdmissionAction.QUARANTINE,
                "below_confidence",
            )
        return MemoryAdmissionDecision(MemoryAdmissionAction.ALLOW, "policy_allowed")

    def explain(self) -> dict[str, object]:
        """Return the versioned, content-free policy contract."""

        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "allowed_origins": [origin.value for origin in self.allowed_origins],
            "rejected_origins": [origin.value for origin in self.rejected_origins],
            "allowed_kinds": [kind.value for kind in self.allowed_kinds],
            "minimum_confidence": self.minimum_confidence,
        }


def _policy_fingerprint(policy: MemoryAdmissionPolicy) -> str:
    return command_hash(policy.explain())


def _review_integrity_tag(
    secret: bytes,
    *,
    scope: MemoryScope,
    record_id: str,
    candidate_revision: int,
    content_hash: str,
    origin: MemoryOrigin,
    policy_id: str,
    policy_version: str,
    policy_fingerprint: str,
    action: MemoryAdmissionAction,
    reason_code: str,
    reviewer_actor: str,
) -> str:
    """Authenticate private review metadata to its creating gate instance."""

    payload = canonical_json({
        "scope_id": scope.correlation_id(),
        "record_id": record_id,
        "candidate_revision": candidate_revision,
        "content_hash": content_hash,
        "origin": origin.value,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "policy_fingerprint": policy_fingerprint,
        "action": action.value,
        "reason_code": reason_code,
        "reviewer_actor": reviewer_actor,
    })
    return hashlib.blake2b(
        payload.encode("utf-8"),
        key=secret,
        digest_size=32,
        person=b"pp-ledger-admit",
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class MemoryReview:
    """A sealed, in-process review snapshot with no plaintext payload.

    Like a recall plan, a review is not a portable checkpoint.  It is bound to
    one gate instance and must be recreated after process restart, policy
    change, or candidate lifecycle change.
    """

    schema_version: int
    policy_id: str
    policy_version: str
    policy_fingerprint: str
    action: MemoryAdmissionAction | str
    reason_code: str
    reviewed_at: datetime
    _record_id: str = field(repr=False, compare=False)
    _candidate_revision: int = field(repr=False, compare=False)
    _content_hash: str = field(repr=False, compare=False)
    _origin: MemoryOrigin = field(repr=False, compare=False)
    _scope: MemoryScope = field(repr=False, compare=False)
    _reviewer_actor: str = field(repr=False, compare=False)
    _owner_token: object | None = field(repr=False, compare=False, default=None)
    _integrity_tag: str = field(repr=False, compare=False, default="")

    def __post_init__(self) -> None:
        if self.schema_version != _REVIEW_SCHEMA_VERSION:
            raise ValueError("unsupported memory review schema version")
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
        reviewed_at = coerce_datetime(self.reviewed_at, field="reviewed_at")
        assert reviewed_at is not None
        object.__setattr__(self, "reviewed_at", reviewed_at)
        object.__setattr__(
            self,
            "_record_id",
            validate_identifier(self._record_id, field="record_id"),
        )
        if isinstance(self._candidate_revision, bool) or not isinstance(
            self._candidate_revision, int
        ) or self._candidate_revision < 1:
            raise ValueError("review requires a positive candidate revision")
        if not isinstance(self._content_hash, str) or len(self._content_hash) != 64:
            raise ValueError("review requires a 64-character content hash")
        object.__setattr__(self, "_origin", MemoryOrigin(self._origin))
        if not isinstance(self._scope, MemoryScope) or self._scope.is_empty:
            raise ValueError("review requires a non-empty MemoryScope")
        object.__setattr__(
            self,
            "_reviewer_actor",
            validate_identifier(self._reviewer_actor, field="reviewer_actor"),
        )
        if self._owner_token is None:
            raise ValueError("review requires a private owner token")
        if not isinstance(self._integrity_tag, str) or len(self._integrity_tag) != 64:
            raise ValueError("review requires a private integrity tag")

    def explain(self) -> dict[str, object]:
        """Return a content-free policy receipt, without scope or record IDs."""

        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_fingerprint": self.policy_fingerprint,
            "action": self.action.value,
            "reason_code": self.reason_code,
            "reviewed_at": self.reviewed_at.isoformat().replace("+00:00", "Z"),
        }


class _MemoryAdmissionIngress:
    """A narrow, host-configured submission endpoint for untrusted text.

    All authority-bearing fields are fixed when the host creates the ingress:
    origin, kind, source/evidence references, confidence, retention, validity,
    and opaque record/event identities never come from ``submit()``.  This is
    the only admission API a model-facing adapter should receive.  The host
    must never route model text through an ingress marked ``asserted``.
    """

    def __init__(
        self,
        writer: MemoryWriter,
        *,
        origin: MemoryOrigin,
        kind: MemoryKind | str,
        source_ref: str,
        evidence_refs: Iterable[str],
        confidence: float,
        retention_policy: str,
        valid_from: datetime | str | None,
        valid_until: datetime | str | None,
        asserted: bool,
    ) -> None:
        if not isinstance(writer, MemoryWriter):
            raise TypeError("writer must be a MemoryWriter")
        self._writer = writer
        self._origin = MemoryOrigin(origin)
        if self._origin in {MemoryOrigin.UNKNOWN, MemoryOrigin.LEGACY_UNKNOWN}:
            raise ValueError("admission ingress requires a concrete trusted origin")
        if not isinstance(asserted, bool):
            raise TypeError("asserted must be a bool")
        if asserted is not (self._origin is MemoryOrigin.HOST_ASSERTION):
            raise ValueError("asserted ingress must use exactly the host_assertion origin")
        self._kind = MemoryKind(kind)
        self._source_ref = validate_reference(source_ref, field="source_ref")
        self._evidence_refs = validate_references(evidence_refs, field="evidence_refs")
        self._confidence = _finite_confidence(confidence, field="confidence")
        self._retention_policy = retention_policy
        self._valid_from = valid_from
        self._valid_until = valid_until
        self._asserted = asserted

    def submit(self, content: str) -> MemoryRecord:
        """Submit untrusted text with no caller-controlled authority fields."""

        if self._asserted:
            return self._writer._assert_candidate_with_origin(
                origin=self._origin,
                kind=self._kind,
                content=content,
                source_ref=self._source_ref,
                evidence_refs=self._evidence_refs,
                confidence=self._confidence,
                retention_policy=self._retention_policy,
                valid_from=self._valid_from,
                valid_until=self._valid_until,
            )
        return self._writer._propose_with_origin(
            origin=self._origin,
            kind=self._kind,
            content=content,
            source_ref=self._source_ref,
            evidence_refs=self._evidence_refs,
            confidence=self._confidence,
            retention_policy=self._retention_policy,
            valid_from=self._valid_from,
            valid_until=self._valid_until,
        )


class MemoryReviewGate:
    """One scope-, origin-, policy-, and actor-pinned admission boundary.

    The gate is a host capability, not a Python authorization sandbox.  Do
    not hand a gate, writer, Ledger, or any lifecycle method to a model tool.
    A model-facing adapter may call only a host-owned ingress function that
    chooses which gate receives an untrusted submission.
    """

    def __init__(
        self,
        writer: MemoryWriter,
        *,
        origin: MemoryOrigin | str,
        policy: MemoryAdmissionPolicy,
    ) -> None:
        if not isinstance(writer, MemoryWriter):
            raise TypeError("writer must be a MemoryWriter")
        if not isinstance(policy, MemoryAdmissionPolicy):
            raise TypeError("policy must be a MemoryAdmissionPolicy")
        normalized_origin = MemoryOrigin(origin)
        if normalized_origin in {MemoryOrigin.UNKNOWN, MemoryOrigin.LEGACY_UNKNOWN}:
            raise ValueError("review gate origin must be a concrete trusted ingress category")
        self._writer = writer
        self._origin = normalized_origin
        self._policy = policy
        self._actor = writer._actor
        self._owner_token = object()
        self._review_secret = secrets.token_bytes(32)

    @property
    def origin(self) -> MemoryOrigin:
        """Return the immutable ingress category assigned by this gate."""

        return self._origin

    @property
    def policy(self) -> MemoryAdmissionPolicy:
        """Return the immutable policy used for future reviews."""

        return self._policy

    def ingress(
        self,
        *,
        kind: MemoryKind | str,
        source_ref: str,
        evidence_refs: Iterable[str] = (),
        confidence: float = 0.5,
        retention_policy: str = "default",
        valid_from: datetime | str | None = None,
        valid_until: datetime | str | None = None,
        asserted: bool = False,
    ) -> _MemoryAdmissionIngress:
        """Create one host-configured narrow ingress for later text submissions.

        ``asserted=True`` is intentionally available only for a
        ``host_assertion`` gate.  Such an ingress is for trusted host code;
        model, document, user, and tool adapters must use the default
        unasserted path.
        """

        if self._origin is MemoryOrigin.HOST_ASSERTION and not asserted:
            raise ValueError("host_assertion ingress must set asserted=True")
        if self._origin is not MemoryOrigin.HOST_ASSERTION and asserted:
            raise ValueError("only a host_assertion gate can create an asserted ingress")
        return _MemoryAdmissionIngress(
            self._writer,
            origin=self._origin,
            kind=kind,
            source_ref=source_ref,
            evidence_refs=evidence_refs,
            confidence=confidence,
            retention_policy=retention_policy,
            valid_from=valid_from,
            valid_until=valid_until,
            asserted=asserted,
        )

    def review(self, record_id: str) -> MemoryReview:
        """Evaluate exactly one current candidate without mutating the Ledger."""

        identity = validate_identifier(record_id, field="record_id")
        candidate = self._writer.get(identity)
        if candidate is None:
            raise KeyError(identity)
        if (
            candidate.state is not MemoryState.CANDIDATE
            or candidate.trust is not MemoryTrust.UNTRUSTED
            or candidate.content is None
            or candidate.origin is not self._origin
        ):
            raise StaleMemoryReviewError(
                "candidate is no longer eligible for this admission gate"
            )
        try:
            decision = self._policy.evaluate(candidate)
        except Exception as exc:
            raise MemoryAdmissionPolicyError("admission policy evaluation failed") from exc
        if not isinstance(decision, MemoryAdmissionDecision):
            raise MemoryAdmissionPolicyError(
                "admission policy must return a MemoryAdmissionDecision"
            )
        reviewed_at = utc_now()
        fingerprint = _policy_fingerprint(self._policy)
        return MemoryReview(
            schema_version=_REVIEW_SCHEMA_VERSION,
            policy_id=self._policy.policy_id,
            policy_version=self._policy.policy_version,
            policy_fingerprint=fingerprint,
            action=decision.action,
            reason_code=decision.reason_code,
            reviewed_at=reviewed_at,
            _record_id=candidate.record_id,
            _candidate_revision=candidate.revision,
            _content_hash=candidate.content_hash,
            _origin=candidate.origin,
            _scope=self._writer.scope,
            _reviewer_actor=self._actor,
            _owner_token=self._owner_token,
            _integrity_tag=_review_integrity_tag(
                self._review_secret,
                scope=self._writer.scope,
                record_id=candidate.record_id,
                candidate_revision=candidate.revision,
                content_hash=candidate.content_hash,
                origin=candidate.origin,
                policy_id=self._policy.policy_id,
                policy_version=self._policy.policy_version,
                policy_fingerprint=fingerprint,
                action=decision.action,
                reason_code=decision.reason_code,
                reviewer_actor=self._actor,
            ),
        )

    def confirm(self, review: MemoryReview, *, event_id: str) -> MemoryRecord:
        """Apply a sealed ``allow`` review under a fresh write boundary."""

        verified = self._verified_review(review, action=MemoryAdmissionAction.ALLOW)
        occurred_at = self._writer._sample_admission_timestamp()
        outcome = self._writer._apply_admission_review(
            review=verified,
            event_id=validate_identifier(event_id, field="event_id"),
            occurred_at=occurred_at,
        )
        if not isinstance(outcome, MemoryRecord):
            raise RuntimeError("allow admission returned an unexpected result")
        return outcome

    def quarantine(self, review: MemoryReview, *, event_id: str) -> MemoryRecord:
        """Apply a sealed ``quarantine`` review under a fresh write boundary."""

        verified = self._verified_review(review, action=MemoryAdmissionAction.QUARANTINE)
        occurred_at = self._writer._sample_admission_timestamp()
        outcome = self._writer._apply_admission_review(
            review=verified,
            event_id=validate_identifier(event_id, field="event_id"),
            occurred_at=occurred_at,
        )
        if not isinstance(outcome, MemoryRecord):
            raise RuntimeError("quarantine admission returned an unexpected result")
        return outcome

    def reject(self, review: MemoryReview, *, event_id: str) -> ErasureReceipt:
        """Apply a sealed ``reject`` review and erase its local payload."""

        verified = self._verified_review(review, action=MemoryAdmissionAction.REJECT)
        occurred_at = self._writer._sample_admission_timestamp()
        outcome = self._writer._apply_admission_review(
            review=verified,
            event_id=validate_identifier(event_id, field="event_id"),
            occurred_at=occurred_at,
        )
        if not isinstance(outcome, ErasureReceipt):
            raise RuntimeError("reject admission returned an unexpected result")
        return outcome

    def _verified_review(
        self,
        review: MemoryReview,
        *,
        action: MemoryAdmissionAction,
    ) -> MemoryReview:
        if not isinstance(review, MemoryReview):
            raise TypeError("review must be a MemoryReview")
        expected_fingerprint = _policy_fingerprint(self._policy)
        if (
            review.action is not action
            or review.policy_id != self._policy.policy_id
            or review.policy_version != self._policy.policy_version
            or review.policy_fingerprint != expected_fingerprint
            or review._origin is not self._origin
            or review._scope != self._writer.scope
            or review._reviewer_actor != self._actor
            or review._owner_token is not self._owner_token
            or not hmac.compare_digest(
                review._integrity_tag,
                _review_integrity_tag(
                    self._review_secret,
                    scope=review._scope,
                    record_id=review._record_id,
                    candidate_revision=review._candidate_revision,
                    content_hash=review._content_hash,
                    origin=review._origin,
                    policy_id=review.policy_id,
                    policy_version=review.policy_version,
                    policy_fingerprint=review.policy_fingerprint,
                    action=review.action,
                    reason_code=review.reason_code,
                    reviewer_actor=review._reviewer_actor,
                ),
            )
        ):
            raise StaleMemoryReviewError(
                "review was created for a different admission gate boundary"
            )
        return review
