"""Read-only, bounded recall planning over host-confirmed ledger records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import math
import re
import secrets
from typing import Callable

from protoprompt.ledger.recall.policy import LedgerRecallPolicy
from protoprompt.ledger.recall.types import (
    _RecallSelection,
    LedgerRecallBudgetError,
    LedgerRecallContext,
    LedgerRecallDecision,
    LedgerRecallPlan,
    StaleMemoryPlanError,
)
from protoprompt.ledger.types import (
    MemoryKind,
    MemoryRecord,
    canonical_json,
    command_hash,
    coerce_datetime,
    utc_now,
    validate_identifier,
)
from protoprompt.ledger.writer import MemoryWriter
from protoprompt.scope import MemoryScope
from protoprompt.tokens.protocol import TokenCounter
from protoprompt.tokens.regex_counter import RegexTokenCounter


_TERM_RE = re.compile(r"[^\W_]+", re.UNICODE)
_MAX_TASK_CHARS = 16_000
_DATA_SCHEMA_VERSION = 1
_DATA_TYPE = "protoprompt.ledger-recall"


def _validate_budget(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must be non-negative")
    return value


def _validate_task(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("task must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("task must not be empty")
    if len(normalized) > _MAX_TASK_CHARS:
        raise ValueError(f"task must be at most {_MAX_TASK_CHARS} characters")
    return normalized


def _terms(value: str) -> frozenset[str]:
    return frozenset(match.group(0).casefold() for match in _TERM_RE.finditer(value))


def _render_data(records: list[MemoryRecord]) -> str:
    """Return one canonical JSON data envelope without ledger identifiers."""

    payload = {
        "records": [
            {"content": record.content, "kind": record.kind.value}
            for record in records
        ],
        "schema_version": _DATA_SCHEMA_VERSION,
        "type": _DATA_TYPE,
    }
    rendered = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    # The envelope is data, not an XML/HTML prompt wrapper.  Escape delimiter-
    # shaped characters anyway so a record cannot visibly close a downstream
    # wrapper that embeds this serialized data verbatim.
    return (
        rendered.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8", errors="strict"))


def _counter_count(counter: TokenCounter, value: str) -> int:
    result = counter.count(value)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise TypeError("TokenCounter.count() must return a non-negative integer")
    return result


def _policy_signature(policy: LedgerRecallPolicy) -> str:
    """Return an opaque compatibility marker without remembering task data."""

    return command_hash(policy.explain())


def _counter_identity(counter: TokenCounter, explicit_id: str | None) -> str:
    """Return a stable receipt label for the token counter contract."""

    if explicit_id is not None:
        return validate_identifier(explicit_id, field="counter_id")
    if isinstance(counter, RegexTokenCounter):
        return "regex-token-counter-v1"
    counter_type = type(counter)
    return validate_identifier(
        f"{counter_type.__module__}.{counter_type.__qualname__}",
        field="counter_id",
    )


def _plan_integrity_tag(
    secret: bytes,
    *,
    policy_fingerprint: str,
    counter_id: str,
    scope: MemoryScope,
    token_budget: int,
    byte_budget: int,
    selections: tuple[_RecallSelection, ...],
) -> str:
    """Authenticate opaque plan metadata to its creating planner instance."""

    payload = canonical_json({
        "policy_fingerprint": policy_fingerprint,
        "counter_id": counter_id,
        "scope_id": scope.correlation_id(),
        "token_budget": token_budget,
        "byte_budget": byte_budget,
        "selections": [
            {
                "record_id": selection.record_id,
                "revision": selection.revision,
                "content_hash": selection.content_hash,
                "kind": selection.kind.value,
            }
            for selection in selections
        ],
    })
    return hashlib.blake2b(
        payload.encode("utf-8"),
        key=secret,
        digest_size=32,
        person=b"pp-ledger-recall",
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class _ScoredCandidate:
    record: MemoryRecord
    score: float
    relevance: float
    confidence_signal: float
    recency_signal: float
    scan_bytes: int


class LedgerRecallPlanner:
    """Plan a bounded, explainable data lane from one pinned ledger writer.

    The planner is intentionally local and read-only. It uses only the
    writer's active-memory reader path; it never calls an LLM, embedding/vector
    service, legacy memory API, or lifecycle mutator. Its budget is for this
    JSON data lane only. The final provider request still needs its own
    :class:`protoprompt.ContextPlan` / request receipt composition.
    """

    def __init__(
        self,
        writer: MemoryWriter,
        *,
        policy: LedgerRecallPolicy | None = None,
        counter: TokenCounter | None = None,
        counter_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(writer, MemoryWriter):
            raise TypeError("writer must be a MemoryWriter")
        if policy is not None and not isinstance(policy, LedgerRecallPolicy):
            raise TypeError("policy must be a LedgerRecallPolicy or None")
        if counter is not None and not callable(getattr(counter, "count", None)):
            raise TypeError("counter must implement TokenCounter.count()")
        self._writer = writer
        self._policy = policy or LedgerRecallPolicy.safe_default()
        self._counter = counter or RegexTokenCounter()
        self._counter_id = _counter_identity(self._counter, counter_id)
        self._clock = clock or utc_now
        self._owner_token = object()
        self._plan_secret = secrets.token_bytes(32)

    @property
    def policy(self) -> LedgerRecallPolicy:
        """Return the immutable selection policy used by this planner."""

        return self._policy

    @property
    def counter_id(self) -> str:
        """Return the versioned token-counter label carried into each receipt."""

        return self._counter_id

    def plan(
        self,
        *,
        task: str,
        token_budget: int,
        byte_budget: int = 32_768,
    ) -> LedgerRecallPlan:
        """Plan whole active records without retaining their plaintext.

        ``task`` is used only for local lexical ranking and is never stored in
        the plan or returned by :meth:`LedgerRecallPlan.explain`.  The host
        controls time through the planner's injected clock; model/tool callers
        cannot backdate a request to bypass record validity.
        """

        normalized_task = _validate_task(task)
        max_tokens = _validate_budget(token_budget, field="token_budget")
        max_bytes = _validate_budget(byte_budget, field="byte_budget")
        instant = self._clock()
        assert instant is not None
        instant = coerce_datetime(instant, field="clock")
        assert instant is not None

        empty_render = _render_data([])
        empty_tokens = _counter_count(self._counter, empty_render)
        empty_bytes = _utf8_size(empty_render)
        if empty_tokens > max_tokens:
            raise LedgerRecallBudgetError(
                "token_budget does not fit the mandatory ledger data envelope"
            )
        if empty_bytes > max_bytes:
            raise LedgerRecallBudgetError(
                "byte_budget does not fit the mandatory ledger data envelope"
            )

        records = self._writer.list_active(
            now=instant,
            limit=self._policy.active_read_limit,
        )
        task_terms = _terms(normalized_task)
        decisions: list[LedgerRecallDecision] = []
        eligible_records: list[MemoryRecord] = []
        for record in records:
            if record.content is None:
                decisions.append(
                    LedgerRecallDecision(
                        kind=record.kind,
                        decision="excluded",
                        reason="missing_content",
                    )
                )
            elif record.kind not in self._policy.allowed_kinds:
                decisions.append(
                    LedgerRecallDecision(
                        kind=record.kind,
                        decision="excluded",
                        reason="policy_excluded",
                    )
                )
            elif record.confidence < self._policy.minimum_confidence:
                decisions.append(
                    LedgerRecallDecision(
                        kind=record.kind,
                        decision="excluded",
                        reason="below_confidence",
                    )
                )
            else:
                eligible_records.append(record)

        considered_records = eligible_records[: self._policy.candidate_limit]
        for record in eligible_records[self._policy.candidate_limit :]:
            decisions.append(
                LedgerRecallDecision(
                    kind=record.kind,
                    decision="excluded",
                    reason="candidate_limit",
                )
            )
        scanned: list[_ScoredCandidate] = []
        scanned_bytes = 0
        unscanned_count = 0
        scanned_count = 0

        for record in considered_records:
            assert record.content is not None
            try:
                content_bytes = _utf8_size(record.content)
            except UnicodeEncodeError:
                scanned_count += 1
                decisions.append(
                    LedgerRecallDecision(
                        kind=record.kind,
                        decision="excluded",
                        reason="non_utf8_content",
                    )
                )
                continue
            if scanned_bytes + content_bytes > self._policy.candidate_scan_byte_budget:
                unscanned_count += 1
                decisions.append(
                    LedgerRecallDecision(
                        kind=record.kind,
                        decision="excluded",
                        reason="scan_byte_budget",
                    )
                )
                continue
            scanned_count += 1
            scanned_bytes += content_bytes
            score, relevance, confidence_signal, recency_signal = self._score(
                record,
                task_terms=task_terms,
                now=instant,
            )
            scanned.append(
                _ScoredCandidate(
                    record=record,
                    score=score,
                    relevance=relevance,
                    confidence_signal=confidence_signal,
                    recency_signal=recency_signal,
                    scan_bytes=content_bytes,
                )
            )

        # Relevance is deliberately lexicographic: an actually relevant record
        # cannot lose merely because an unrelated one has a higher confidence
        # or is newer. Confidence and recency rank only inside the same lexical
        # relevance tier; the ledger order settles exact ties.
        scored = sorted(
            scanned,
            key=lambda candidate: (
                -candidate.relevance,
                -candidate.confidence_signal,
                -candidate.recency_signal,
                -candidate.record.updated_at.timestamp(),
                candidate.record.record_id,
            ),
        )
        selected_records: list[MemoryRecord] = []
        selections: list[_RecallSelection] = []
        used_tokens = empty_tokens
        used_bytes = empty_bytes

        for candidate in scored:
            prospective = [*selected_records, candidate.record]
            try:
                rendered = _render_data(prospective)
                prospective_bytes = _utf8_size(rendered)
            except UnicodeEncodeError:
                decisions.append(
                    LedgerRecallDecision(
                        kind=candidate.record.kind,
                        decision="excluded",
                        reason="non_utf8_content",
                        score=candidate.score,
                    )
                )
                continue
            prospective_tokens = _counter_count(self._counter, rendered)
            # A deterministic third-party tokenizer need not be monotonic
            # across two distinct JSON strings (for example due to boundary
            # merges). The prospective full-envelope count governs budgets;
            # per-record receipt costs remain non-negative.
            marginal_tokens = max(0, prospective_tokens - used_tokens)
            marginal_bytes = prospective_bytes - used_bytes
            if prospective_tokens > max_tokens:
                decisions.append(
                    LedgerRecallDecision(
                        kind=candidate.record.kind,
                        decision="excluded",
                        reason="over_token_budget",
                        candidate_tokens=marginal_tokens,
                        candidate_bytes=marginal_bytes,
                        score=candidate.score,
                    )
                )
                continue
            if prospective_bytes > max_bytes:
                decisions.append(
                    LedgerRecallDecision(
                        kind=candidate.record.kind,
                        decision="excluded",
                        reason="over_byte_budget",
                        candidate_tokens=marginal_tokens,
                        candidate_bytes=marginal_bytes,
                        score=candidate.score,
                    )
                )
                continue
            selected_records.append(candidate.record)
            selections.append(
                _RecallSelection(
                    record_id=candidate.record.record_id,
                    revision=candidate.record.revision,
                    content_hash=candidate.record.content_hash,
                    kind=candidate.record.kind,
                )
            )
            decisions.append(
                LedgerRecallDecision(
                    kind=candidate.record.kind,
                    decision="selected",
                    reason="selected",
                    token_cost=marginal_tokens,
                    byte_cost=marginal_bytes,
                    candidate_tokens=marginal_tokens,
                    candidate_bytes=marginal_bytes,
                    score=candidate.score,
                )
            )
            used_tokens = prospective_tokens
            used_bytes = prospective_bytes

        selection_snapshot = tuple(selections)
        policy_fingerprint = _policy_signature(self._policy)
        policy_explain_json = json.dumps(
            self._policy.explain(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return LedgerRecallPlan(
            schema_version=1,
            policy_id=self._policy.policy_id,
            policy_fingerprint=policy_fingerprint,
            counter_id=self._counter_id,
            planned_at=instant,
            token_budget=max_tokens,
            byte_budget=max_bytes,
            used_tokens=used_tokens,
            used_bytes=used_bytes,
            active_record_count=len(records),
            active_read_limit_reached=len(records) == self._policy.active_read_limit,
            eligible_record_count=len(eligible_records),
            candidate_count=len(considered_records),
            scanned_count=scanned_count,
            unscanned_count=unscanned_count,
            candidate_limit_reached=len(eligible_records) > len(considered_records),
            decisions=tuple(decisions),
            _selections=selection_snapshot,
            _policy_explain_json=policy_explain_json,
            _owner_token=self._owner_token,
            _integrity_tag=_plan_integrity_tag(
                self._plan_secret,
                policy_fingerprint=policy_fingerprint,
                counter_id=self._counter_id,
                scope=self._writer.scope,
                token_budget=max_tokens,
                byte_budget=max_bytes,
                selections=selection_snapshot,
            ),
            _scope=self._writer.scope,
        )

    def resolve(
        self,
        plan: LedgerRecallPlan,
    ) -> LedgerRecallContext:
        """Resolve a plan with final lifecycle validation or fail closed.

        Rendering and token accounting run before the short exclusive Ledger
        validation boundary, so an injected :class:`TokenCounter` can neither
        hold a SQLite writer lock nor re-enter a connection inside a
        transaction.  The final validation is the linearization point: a
        concurrent ``forget``/``retract`` that wins before it makes this
        method fail closed; one that starts later observes a context that was
        current at that point.  Callers still must not retain/send a previously
        returned data string after a later deletion request.
        """

        if not isinstance(plan, LedgerRecallPlan):
            raise TypeError("plan must be a LedgerRecallPlan")
        if (
            plan.policy_id != self._policy.policy_id
            or plan.policy_fingerprint != _policy_signature(self._policy)
            or plan.counter_id != self._counter_id
            or plan._scope != self._writer.scope
            or plan._owner_token is not self._owner_token
            or not hmac.compare_digest(
                plan._integrity_tag,
                _plan_integrity_tag(
                    self._plan_secret,
                    policy_fingerprint=plan.policy_fingerprint,
                    counter_id=plan.counter_id,
                    scope=plan._scope,
                    token_budget=plan.token_budget,
                    byte_budget=plan.byte_budget,
                    selections=plan._selections,
                ),
            )
        ):
            raise ValueError("plan was created for a different recall planner boundary")
        instant = self._clock()
        assert instant is not None
        instant = coerce_datetime(instant, field="clock")
        assert instant is not None

        resolved_records = self._resolve_records(
            plan,
            self._writer.list_active(
                now=instant,
                limit=self._policy.active_read_limit,
            ),
            instant=instant,
        )
        context = self._render_context(plan, resolved_records)
        if not plan._selections:
            return context
        pre_validation_instant = self._clock()
        assert pre_validation_instant is not None
        pre_validation_instant = coerce_datetime(pre_validation_instant, field="clock")
        assert pre_validation_instant is not None
        valid_snapshot = self._writer._validate_active_snapshot(
            now=pre_validation_instant,
            limit=self._policy.active_read_limit,
            selections=tuple(
                (
                    selection.record_id,
                    selection.revision,
                    selection.content_hash,
                    selection.kind,
                )
                for selection in plan._selections
            ),
        )
        if not valid_snapshot:
            raise StaleMemoryPlanError(
                "selected ledger memory changed or is no longer recallable; replan"
            )
        post_validation_instant = self._clock()
        assert post_validation_instant is not None
        post_validation_instant = coerce_datetime(post_validation_instant, field="clock")
        assert post_validation_instant is not None
        if any(
            record.kind not in self._policy.allowed_kinds
            or record.confidence < self._policy.minimum_confidence
            or not record.is_recallable(now=post_validation_instant)
            for record in resolved_records
        ):
            raise StaleMemoryPlanError(
                "selected ledger memory is no longer recallable at final host time; replan"
            )
        return context

    def _resolve_records(
        self,
        plan: LedgerRecallPlan,
        current_records: list[MemoryRecord],
        *,
        instant: datetime,
    ) -> list[MemoryRecord]:
        """Return records that still match an authenticated plan snapshot."""

        current_by_id = {record.record_id: record for record in current_records}
        resolved_records: list[MemoryRecord] = []
        for selection in plan._selections:
            record = current_by_id.get(selection.record_id)
            if (
                record is None
                or record.revision != selection.revision
                or record.content_hash != selection.content_hash
                or record.kind is not selection.kind
                or record.kind not in self._policy.allowed_kinds
                or record.confidence < self._policy.minimum_confidence
                or not record.is_recallable(now=instant)
            ):
                raise StaleMemoryPlanError(
                    "selected ledger memory changed or is no longer recallable; replan"
                )
            resolved_records.append(record)
        return resolved_records

    def _render_context(
        self,
        plan: LedgerRecallPlan,
        resolved_records: list[MemoryRecord],
    ) -> LedgerRecallContext:
        """Render and account outside the SQLite lifecycle-write boundary."""

        try:
            rendered = _render_data(resolved_records)
            used_bytes = _utf8_size(rendered)
        except UnicodeEncodeError as exc:
            raise StaleMemoryPlanError(
                "selected ledger memory can no longer be safely UTF-8 rendered; replan"
            ) from exc
        used_tokens = _counter_count(self._counter, rendered)
        if (
            used_tokens != plan.used_tokens
            or used_bytes != plan.used_bytes
            or used_tokens > plan.token_budget
            or used_bytes > plan.byte_budget
        ):
            raise StaleMemoryPlanError(
                "resolved ledger memory no longer matches the planned bounded receipt; replan"
            )
        return LedgerRecallContext(
            schema_version=1,
            policy_id=plan.policy_id,
            used_tokens=used_tokens,
            used_bytes=used_bytes,
            token_budget=plan.token_budget,
            byte_budget=plan.byte_budget,
            record_count=len(resolved_records),
            _rendered_data=rendered,
        )

    def _score(
        self,
        record: MemoryRecord,
        *,
        task_terms: frozenset[str],
        now: datetime,
    ) -> tuple[float, float, float, float]:
        """Rank local content without embeddings, calls, or hidden state."""

        assert record.content is not None
        content_terms = _terms(record.content)
        relevance = (
            len(task_terms.intersection(content_terms)) / len(task_terms)
            if task_terms
            else 0.0
        )
        age_seconds = max(0.0, (now - record.updated_at).total_seconds())
        recency = 1.0 / (1.0 + age_seconds / 3600.0)
        relevance_signal = self._policy.relevance_weight * relevance
        confidence_signal = self._policy.confidence_weight * record.confidence
        recency_signal = self._policy.recency_weight * recency
        score = relevance_signal + confidence_signal + recency_signal
        if not math.isfinite(score):
            raise RuntimeError("ledger recall score must be finite")
        return (
            round(score, 9),
            round(relevance_signal, 9),
            round(confidence_signal, 9),
            round(recency_signal, 9),
        )
