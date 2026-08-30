"""Property-based public conformance checks for Memory Ledger backends.

The deterministic examples in :mod:`ledger_conformance.core` pin named
regressions.  This module complements them with a small state machine that
uses only the host-facing ``MemoryWriter`` and Ledger setup/close APIs.  It
does not know a backend's tables, transactions, or private helpers, so the
same checks can later run against another conformant implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import string
from typing import Any, Callable

from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from protoprompt.ledger import (
    LedgerConflictError,
    LedgerStateError,
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryState,
    MemoryTrust,
    MemoryWriter,
)
from protoprompt.ledger.recall import LedgerRecallPlanner, LedgerRecallPolicy
from protoprompt.scope import MemoryScope
from protoprompt.tokens import RegexTokenCounter


LedgerFactory = Callable[[], Any]

# ASCII-only values are deliberate: the property checks lifecycle semantics,
# not identifier validation or Unicode normalization.  Keeping them compact
# also makes a minimized Hypothesis failure readable in CI output.
_OPAQUE_ALPHABET = string.ascii_lowercase + string.digits
OPAQUE_TEXT = st.text(alphabet=_OPAQUE_ALPHABET, min_size=1, max_size=8)
SCOPE_FIELD = st.sampled_from(("tenant", "user", "thread", "kind"))
MEMORY_KIND = st.sampled_from(tuple(MemoryKind))

# Recall payloads deliberately carry an unambiguous marker once admitted below.
# The varied characters exercise UTF-8 accounting and JSON escaping while the
# short bounded values keep this a regular deterministic release-gate test.
_RECALL_PAYLOAD_ALPHABET = string.ascii_lowercase + string.digits + "\u0451\u6f22\U0001f680<>&/\""
RECALL_PAYLOADS = st.lists(
    st.text(alphabet=_RECALL_PAYLOAD_ALPHABET, min_size=1, max_size=16),
    min_size=1,
    max_size=5,
)
RECALL_TOKEN_SLACK = st.integers(min_value=0, max_value=96)
RECALL_BYTE_SLACK = st.integers(min_value=0, max_value=768)

# Statefulness is deterministic and intentionally bounded so this remains a
# normal release-gate test rather than an open-ended fuzz job.  The database is
# disabled because a failing minimized sequence is printed by Hypothesis and
# should not make the next local run depend on hidden on-disk state.
PROPERTY_SETTINGS = settings(
    max_examples=20,
    stateful_step_count=12,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=(HealthCheck.too_slow,),
)

_FIXED_NOW = datetime(2042, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@dataclass
class _TrackedRecord:
    """The small public-contract model retained by the state machine."""

    writer_name: str
    record_id: str
    source_ref: str
    content: str
    payload_present: bool = True
    erased: bool = False


def _scopes(seed: str, differing_field: str) -> tuple[MemoryScope, MemoryScope]:
    """Return two non-empty scopes that differ in exactly one field."""

    left_values = {
        "tenant": f"tenant-{seed}",
        "user": f"user-{seed}",
        "thread": f"thread-{seed}",
        "kind": f"kind-{seed}",
    }
    right_values = dict(left_values)
    right_values[differing_field] = f"other-{differing_field}-{seed}"
    return MemoryScope(**left_values), MemoryScope(**right_values)


def _writer(ledger: Any, scope: MemoryScope, name: str) -> MemoryWriter:
    """Build a deterministic host-owned writer through its public API."""

    return MemoryWriter(
        ledger,
        scope=scope,
        actor=f"property-host-{name}",
        clock=lambda: _FIXED_NOW,
    )


def _strict_document_policy() -> MemoryAdmissionPolicy:
    """Return the document-only host policy required by strict recall."""

    return MemoryAdmissionPolicy(
        policy_id="ledger-property-document-policy-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.DOCUMENT,),
        minimum_confidence=0.5,
    )


def assert_recall_budget_packing_property(
    factory: LedgerFactory,
    *,
    payloads: list[str],
    token_slack: int,
    byte_slack: int,
) -> None:
    """Check strict recall's bounded whole-record packing for one input.

    The caller supplies bounded generated document payloads and budget slack.
    Every record enters through a document ``MemoryReviewGate`` and is then
    selected by an admission-safe planner, so this tests the public host path
    rather than an adapter's storage details.  A roomy plan establishes the
    safely measurable envelope; generated budgets are then clipped between
    the mandatory empty envelope and that complete result.
    """

    ledger = factory()
    ledger.setup()
    try:
        scope = MemoryScope(
            tenant="property",
            user="recall",
            thread="budget-packing",
        )
        writer = _writer(ledger, scope, "recall")
        counter = RegexTokenCounter()
        planner = LedgerRecallPlanner(
            writer,
            policy=LedgerRecallPolicy.admission_safe_default(),
            counter=counter,
            clock=lambda: _FIXED_NOW,
        )
        task = "strict document recall budget packing"

        # Obtain the public mandatory envelope before documents are admitted;
        # no private renderer or storage inspection is needed to find its cost.
        empty_plan = planner.plan(task=task, token_budget=4_096, byte_budget=32_768)
        empty_context = planner.resolve(empty_plan)
        assert empty_context.record_count == 0

        gate = MemoryReviewGate(
            writer,
            origin=MemoryOrigin.DOCUMENT,
            policy=_strict_document_policy(),
        )
        admitted_payloads: list[str] = []
        for index, payload in enumerate(payloads):
            content = f"generated-recall-payload-{index}-{payload}"
            candidate = gate.ingress(
                kind=MemoryKind.FACT,
                source_ref=f"property:recall-source:{index}",
                evidence_refs=(f"property:recall-evidence:{index}",),
                confidence=0.9,
            ).submit(content)
            active = gate.confirm(
                gate.review(candidate.record_id),
                event_id=f"property-recall-confirm-{index}",
            )
            assert active.content == content
            admitted_payloads.append(content)

        # The bounded input size makes these intentionally roomy limits a
        # public reference envelope containing every strictly admitted record.
        roomy_plan = planner.plan(task=task, token_budget=4_096, byte_budget=32_768)
        roomy_context = planner.resolve(roomy_plan)
        assert roomy_context.record_count == len(admitted_payloads)

        token_budget = min(
            roomy_plan.used_tokens,
            empty_context.used_tokens + token_slack,
        )
        byte_budget = min(
            roomy_plan.used_bytes,
            empty_context.used_bytes + byte_slack,
        )
        plan = planner.plan(
            task=task,
            token_budget=token_budget,
            byte_budget=byte_budget,
        )
        context = planner.resolve(plan)
        rendered = context.render_data()
        plan_receipt = plan.explain()
        context_receipt = context.explain()

        # The plan and fresh resolution independently retain the same exact
        # whole-envelope accounting and neither may cross its allocated lane.
        assert plan.token_budget == token_budget
        assert plan.byte_budget == byte_budget
        assert plan.used_tokens <= plan.token_budget
        assert plan.used_bytes <= plan.byte_budget
        assert context.used_tokens <= context.token_budget
        assert context.used_bytes <= context.byte_budget
        assert context.token_budget == plan.token_budget
        assert context.byte_budget == plan.byte_budget
        assert context.used_tokens == plan.used_tokens == counter.count(rendered)
        assert context.used_bytes == plan.used_bytes == len(
            rendered.encode("utf-8", errors="strict")
        )

        # Reconcile public receipts with each other, the resolved data, and
        # the planner's selected decisions.  The parsed JSON proves that the
        # packed records are whole admitted payloads, not backend projections.
        for field in (
            "schema_version",
            "policy_id",
            "token_budget",
            "byte_budget",
            "used_tokens",
            "used_bytes",
            "remaining_tokens",
            "remaining_bytes",
        ):
            assert plan_receipt[field] == context_receipt[field]
        assert plan_receipt["remaining_tokens"] == token_budget - plan.used_tokens
        assert plan_receipt["remaining_bytes"] == byte_budget - plan.used_bytes
        assert plan_receipt["selected_count"] == context.record_count
        assert sum(
            decision["decision"] == "selected"
            for decision in plan_receipt["decisions"]
        ) == context.record_count
        assert plan.scanned_count + plan.unscanned_count == plan.candidate_count

        records = json.loads(rendered)["records"]
        selected_payloads = [record["content"] for record in records]
        assert len(selected_payloads) == context.record_count
        assert set(selected_payloads).issubset(set(admitted_payloads))
        assert all(record["kind"] == MemoryKind.FACT.value for record in records)

        # Explain receipts remain operational metadata: every generated
        # plaintext document payload is absent even if it was selected.
        receipt_texts = (
            json.dumps(plan_receipt, ensure_ascii=False, sort_keys=True),
            json.dumps(context_receipt, ensure_ascii=False, sort_keys=True),
        )
        for payload in admitted_payloads:
            assert all(payload not in receipt for receipt in receipt_texts)
    finally:
        ledger.close()


def assert_scoped_deletion_property(
    factory: LedgerFactory,
    *,
    scope_seed: str,
    differing_field: str,
    content: str,
) -> None:
    """Prove scope-local delete/erase behavior for one generated input.

    Both writers intentionally use the same logical record and source IDs.
    A normal forget, its exact retry, and a subsequent hard erase must affect
    only the writer's host-owned scope.  The test observes every result through
    ``MemoryWriter`` rather than inspecting backend-specific rows.
    """

    ledger = factory()
    ledger.setup()
    try:
        left_scope, right_scope = _scopes(scope_seed, differing_field)
        left = _writer(ledger, left_scope, "left")
        right = _writer(ledger, right_scope, "right")
        record_id = "shared-record"
        source_ref = f"source-{scope_seed}"
        left_content = f"left-{content}"
        right_content = f"right-{content}"

        left_candidate = left.propose(
            kind=MemoryKind.FACT,
            content=left_content,
            source_ref=source_ref,
            record_id=record_id,
            event_id="left-observed",
        )
        right_candidate = right.propose(
            kind=MemoryKind.FACT,
            content=right_content,
            source_ref=source_ref,
            record_id=record_id,
            event_id="right-observed",
        )
        left_active = left.confirm(
            record_id,
            expected_revision=left_candidate.revision,
            event_id="left-confirmed",
        )
        right_active = right.confirm(
            record_id,
            expected_revision=right_candidate.revision,
            event_id="right-confirmed",
        )
        right_events_before = right.events(record_id)

        forgotten = left.forget(
            record_id,
            expected_revision=left_active.revision,
            event_id="left-forgotten",
        )
        assert (
            left.forget(
                record_id,
                expected_revision=left_active.revision,
                event_id="left-forgotten",
            )
            == forgotten
        )
        left_after_forget = left.get(record_id)
        assert left_after_forget is not None
        assert left_after_forget.content is None
        assert left_after_forget.source_refs == ()
        assert left.list_active() == []

        # The same logical id and source remain independent in the neighboring
        # host scope, including their append-only event history.
        right_after_forget = right.get(record_id)
        assert right_after_forget is not None
        assert right_after_forget.scope == right_scope
        assert right_after_forget.content == right_content
        assert right_after_forget.revision == right_active.revision
        assert right.events(record_id) == right_events_before

        erased = left.erase(
            record_id,
            expected_revision=left_after_forget.revision,
            event_id="left-erased",
        )
        assert (
            left.erase(
                record_id,
                expected_revision=left_after_forget.revision,
                event_id="left-erased",
            )
            == erased
        )
        assert left.get(record_id) is None
        assert left.events(record_id) == []
        assert right.get(record_id) is not None
        assert right.get(record_id).content == right_content

        # Source-wide deletion is a separate public command.  It must revoke
        # only the left scope and prevent a later ingest there, while the same
        # opaque source remains usable by the neighboring host scope.
        source_record_id = "source-revocation-record"
        left_source_content = f"left-source-{content}"
        right_source_content = f"right-source-{content}"
        left_source_candidate = left.propose(
            kind=MemoryKind.FACT,
            content=left_source_content,
            source_ref=source_ref,
            record_id=source_record_id,
            event_id="left-source-observed",
        )
        right_source_candidate = right.propose(
            kind=MemoryKind.FACT,
            content=right_source_content,
            source_ref=source_ref,
            record_id=source_record_id,
            event_id="right-source-observed",
        )
        left.confirm(
            source_record_id,
            expected_revision=left_source_candidate.revision,
            event_id="left-source-confirmed",
        )
        right.confirm(
            source_record_id,
            expected_revision=right_source_candidate.revision,
            event_id="right-source-confirmed",
        )
        receipts = left.forget_by_source(source_ref)
        assert [receipt.record_id for receipt in receipts] == [source_record_id]
        assert left.forget_by_source(source_ref) == []
        left_source_after = left.get(source_record_id)
        assert left_source_after is not None
        assert left_source_after.content is None
        assert left_source_after.source_refs == ()
        try:
            left.propose(
                kind=MemoryKind.FACT,
                content="blocked-source-reingest",
                source_ref=source_ref,
                record_id="left-source-reingest",
                event_id="left-source-reingest",
            )
        except LedgerStateError:
            pass
        else:
            raise AssertionError("a source revoked in one scope was re-ingested")
        right_source_after = right.get(source_record_id)
        assert right_source_after is not None
        assert right_source_after.content == right_source_content
        right_extra = right.propose(
            kind=MemoryKind.FACT,
            content="allowed-source-reingest",
            source_ref=source_ref,
            record_id="right-source-reingest",
            event_id="right-source-reingest",
        )
        assert right_extra.content == "allowed-source-reingest"
    finally:
        ledger.close()


def run_lifecycle_state_machine(
    factory: LedgerFactory,
    *,
    state_machine_settings: Any = PROPERTY_SETTINGS,
) -> None:
    """Run bounded lifecycle, scope, idempotency, and deletion properties.

    ``factory`` must return a fresh Ledger; this helper performs its explicit
    setup and close cycle. All commands go through scope-pinned writers,
    enabling the helper to serve as a shared conformance test for supported
    backends later.
    """

    class LedgerLifecycleMachine(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            self._ledger = factory()
            self._ledger.setup()
            self._writers: dict[str, MemoryWriter] = {}
            self._tracks: dict[tuple[str, str], _TrackedRecord] = {}
            self._record_counter = 0
            self._event_counter = 0
            self._initialized = False

        def teardown(self) -> None:
            self._ledger.close()

        @initialize(scope_seed=OPAQUE_TEXT, differing_field=SCOPE_FIELD)
        def initialize_scopes(self, scope_seed: str, differing_field: str) -> None:
            left_scope, right_scope = _scopes(scope_seed, differing_field)
            self._writers = {
                "left": _writer(self._ledger, left_scope, "left"),
                "right": _writer(self._ledger, right_scope, "right"),
            }
            self._initialized = True
            # A shared logical id is present in every generated example, so
            # every rule/invariant has a real scope-isolation target.
            self._propose_pair("initial", "initial", MemoryKind.FACT)

        def _event_id(self, operation: str) -> str:
            self._event_counter += 1
            return f"event-{operation}-{self._event_counter}"

        def _propose_pair(self, content: str, source: str, kind: MemoryKind) -> None:
            record_id = f"record-{self._record_counter}"
            self._record_counter += 1
            source_ref = f"source-{source}-{self._record_counter}"
            for writer_name, prefix in (("left", "left"), ("right", "right")):
                writer = self._writers[writer_name]
                record_content = f"{prefix}-{self._record_counter}-{content}"
                event_id = self._event_id(f"{writer_name}-observed")
                first = writer.propose(
                    kind=kind,
                    content=record_content,
                    source_ref=source_ref,
                    record_id=record_id,
                    event_id=event_id,
                )
                retried = writer.propose(
                    kind=kind,
                    content=record_content,
                    source_ref=source_ref,
                    record_id=record_id,
                    event_id=event_id,
                )
                assert retried == first
                assert len(writer.events(record_id)) == 1
                self._tracks[(writer_name, record_id)] = _TrackedRecord(
                    writer_name=writer_name,
                    record_id=record_id,
                    source_ref=source_ref,
                    content=record_content,
                )

        def _records_matching(self, predicate: Callable[[Any], bool]) -> list[_TrackedRecord]:
            matches: list[_TrackedRecord] = []
            for track in self._tracks.values():
                record = self._writers[track.writer_name].get(track.record_id)
                if record is not None and predicate(record):
                    matches.append(track)
            return matches

        @staticmethod
        def _choose(records: list[_TrackedRecord], index: int) -> _TrackedRecord:
            assert records
            return records[index % len(records)]

        def _record(self, track: _TrackedRecord):
            record = self._writers[track.writer_name].get(track.record_id)
            assert record is not None
            return record

        def _has_active_pair(self) -> bool:
            for writer_name in self._writers:
                active_count = sum(
                    1
                    for track in self._tracks.values()
                    if track.writer_name == writer_name
                    and self._writers[writer_name].get(track.record_id) is not None
                    and self._writers[writer_name].get(track.record_id).state
                    is MemoryState.ACTIVE
                )
                if active_count >= 2:
                    return True
            return False

        def _transition_with_exact_retry(
            self,
            track: _TrackedRecord,
            operation: str,
            apply: Callable[[MemoryWriter, Any, str], Any],
        ) -> Any:
            writer = self._writers[track.writer_name]
            before = self._record(track)
            events_before = writer.events(track.record_id)
            event_id = self._event_id(operation)
            first = apply(writer, before, event_id)
            retried = apply(writer, before, event_id)
            assert retried == first
            assert len(writer.events(track.record_id)) == len(events_before) + 1
            return first

        @rule(content=OPAQUE_TEXT, source=OPAQUE_TEXT, kind=MEMORY_KIND)
        def propose_pair(self, content: str, source: str, kind: MemoryKind) -> None:
            self._propose_pair(content, source, kind)

        @precondition(
            lambda self: bool(
                self._records_matching(lambda record: record.state is MemoryState.CANDIDATE)
            )
        )
        @rule(index=st.integers(min_value=0, max_value=100))
        def confirm_candidate(self, index: int) -> None:
            track = self._choose(
                self._records_matching(lambda record: record.state is MemoryState.CANDIDATE),
                index,
            )
            confirmed = self._transition_with_exact_retry(
                track,
                "confirmed",
                lambda writer, record, event_id: writer.confirm(
                    record.record_id,
                    expected_revision=record.revision,
                    event_id=event_id,
                ),
            )
            assert confirmed.state is MemoryState.ACTIVE
            assert confirmed.trust is MemoryTrust.HOST_CONFIRMED

        @precondition(
            lambda self: bool(
                self._records_matching(
                    lambda record: record.state
                    in {MemoryState.CANDIDATE, MemoryState.ACTIVE}
                )
            )
        )
        @rule(index=st.integers(min_value=0, max_value=100))
        def quarantine_record(self, index: int) -> None:
            track = self._choose(
                self._records_matching(
                    lambda record: record.state
                    in {MemoryState.CANDIDATE, MemoryState.ACTIVE}
                ),
                index,
            )
            quarantined = self._transition_with_exact_retry(
                track,
                "quarantined",
                lambda writer, record, event_id: writer.quarantine(
                    record.record_id,
                    expected_revision=record.revision,
                    reason_code="property_review",
                    event_id=event_id,
                ),
            )
            assert quarantined.state is MemoryState.QUARANTINED

        @precondition(
            lambda self: bool(
                self._records_matching(
                    lambda record: record.state
                    in {MemoryState.CANDIDATE, MemoryState.ACTIVE}
                )
            )
        )
        @rule(index=st.integers(min_value=0, max_value=100))
        def expire_record(self, index: int) -> None:
            track = self._choose(
                self._records_matching(
                    lambda record: record.state
                    in {MemoryState.CANDIDATE, MemoryState.ACTIVE}
                ),
                index,
            )
            expired = self._transition_with_exact_retry(
                track,
                "expired",
                lambda writer, record, event_id: writer.expire(
                    record.record_id,
                    expected_revision=record.revision,
                    event_id=event_id,
                ),
            )
            assert expired.state is MemoryState.EXPIRED

        @precondition(
            lambda self: bool(
                self._records_matching(
                    lambda record: record.state
                    in {
                        MemoryState.CANDIDATE,
                        MemoryState.ACTIVE,
                        MemoryState.SUPERSEDED,
                        MemoryState.EXPIRED,
                        MemoryState.QUARANTINED,
                    }
                )
            )
        )
        @rule(index=st.integers(min_value=0, max_value=100))
        def retract_record(self, index: int) -> None:
            track = self._choose(
                self._records_matching(
                    lambda record: record.state
                    in {
                        MemoryState.CANDIDATE,
                        MemoryState.ACTIVE,
                        MemoryState.SUPERSEDED,
                        MemoryState.EXPIRED,
                        MemoryState.QUARANTINED,
                    }
                ),
                index,
            )
            retracted = self._transition_with_exact_retry(
                track,
                "retracted",
                lambda writer, record, event_id: writer.retract(
                    record.record_id,
                    expected_revision=record.revision,
                    reason_code="property_retraction",
                    event_id=event_id,
                ),
            )
            assert retracted.state is MemoryState.RETRACTED

        @precondition(
            lambda self: bool(
                self._records_matching(lambda record: record.content is not None)
            )
        )
        @rule(index=st.integers(min_value=0, max_value=100))
        def forget_record(self, index: int) -> None:
            track = self._choose(
                self._records_matching(lambda record: record.content is not None),
                index,
            )
            forgotten = self._transition_with_exact_retry(
                track,
                "forgotten",
                lambda writer, record, event_id: writer.forget(
                    record.record_id,
                    expected_revision=record.revision,
                    reason_code="property_forget",
                    event_id=event_id,
                ),
            )
            assert forgotten.payload_deleted is True
            track.payload_present = False
            erased_payload = self._record(track)
            assert erased_payload.state is MemoryState.RETRACTED
            assert erased_payload.content is None
            assert erased_payload.source_refs == ()

        @precondition(lambda self: bool(self._records_matching(lambda record: True)))
        @rule(index=st.integers(min_value=0, max_value=100))
        def hard_erase_record(self, index: int) -> None:
            track = self._choose(self._records_matching(lambda record: True), index)
            writer = self._writers[track.writer_name]
            before = self._record(track)
            event_id = self._event_id("erased")
            first = writer.erase(
                before.record_id,
                expected_revision=before.revision,
                event_id=event_id,
            )
            retried = writer.erase(
                before.record_id,
                expected_revision=before.revision,
                event_id=event_id,
            )
            assert retried == first
            track.erased = True
            track.payload_present = False
            assert writer.get(track.record_id) is None
            assert writer.events(track.record_id) == []

        @precondition(lambda self: self._has_active_pair())
        @rule(index=st.integers(min_value=0, max_value=100))
        def supersede_active_record(self, index: int) -> None:
            pairs: list[tuple[_TrackedRecord, _TrackedRecord]] = []
            for writer_name in self._writers:
                active = [
                    track
                    for track in self._records_matching(
                        lambda record: record.state is MemoryState.ACTIVE
                    )
                    if track.writer_name == writer_name
                ]
                for offset, old in enumerate(active):
                    for replacement in active[offset + 1 :]:
                        pairs.append((old, replacement))
            if not pairs:
                return
            old, replacement = pairs[index % len(pairs)]
            writer = self._writers[old.writer_name]
            old_before = self._record(old)
            replacement_before = self._record(replacement)
            event_id = self._event_id("superseded")
            first = writer.supersede(
                old_before.record_id,
                replacement_record_id=replacement_before.record_id,
                expected_revision=old_before.revision,
                expected_replacement_revision=replacement_before.revision,
                event_id=event_id,
            )
            retried = writer.supersede(
                old_before.record_id,
                replacement_record_id=replacement_before.record_id,
                expected_revision=old_before.revision,
                expected_replacement_revision=replacement_before.revision,
                event_id=event_id,
            )
            assert retried == first
            assert first.state is MemoryState.SUPERSEDED
            assert first.superseded_by == replacement_before.record_id

        @precondition(
            lambda self: bool(
                self._records_matching(
                    lambda record: record.state is not MemoryState.CANDIDATE
                )
            )
        )
        @rule(index=st.integers(min_value=0, max_value=100))
        def invalid_transition_is_atomic(self, index: int) -> None:
            track = self._choose(
                self._records_matching(
                    lambda record: record.state is not MemoryState.CANDIDATE
                ),
                index,
            )
            writer = self._writers[track.writer_name]
            before = self._record(track)
            events_before = writer.events(track.record_id)
            try:
                writer.confirm(
                    before.record_id,
                    expected_revision=before.revision,
                    event_id=self._event_id("invalid-confirm"),
                )
            except LedgerStateError:
                pass
            else:
                raise AssertionError("an invalid confirmation unexpectedly succeeded")
            assert writer.get(track.record_id) == before
            assert writer.events(track.record_id) == events_before

        @precondition(
            lambda self: bool(
                self._records_matching(
                    lambda record: record.state
                    in {MemoryState.CANDIDATE, MemoryState.ACTIVE}
                )
            )
        )
        @rule(index=st.integers(min_value=0, max_value=100))
        def stale_revision_is_atomic(self, index: int) -> None:
            track = self._choose(
                self._records_matching(
                    lambda record: record.state
                    in {MemoryState.CANDIDATE, MemoryState.ACTIVE}
                ),
                index,
            )
            writer = self._writers[track.writer_name]
            before = self._record(track)
            events_before = writer.events(track.record_id)
            try:
                writer.quarantine(
                    before.record_id,
                    expected_revision=before.revision + 1,
                    reason_code="property_stale",
                    event_id=self._event_id("stale"),
                )
            except LedgerConflictError:
                pass
            else:
                raise AssertionError("a stale revision unexpectedly succeeded")
            assert writer.get(track.record_id) == before
            assert writer.events(track.record_id) == events_before

        @invariant()
        def public_projection_and_scope_invariants(self) -> None:
            if not self._initialized:
                return
            for writer_name, writer in self._writers.items():
                active_records = writer.list_active(limit=100)
                active_ids = {record.record_id for record in active_records}
                assert len(active_ids) == len(active_records)
                for record in active_records:
                    assert record.scope == writer.scope
                    assert record.state is MemoryState.ACTIVE
                    assert record.trust is MemoryTrust.HOST_CONFIRMED
                    assert record.content is not None

                for (tracked_writer, record_id), track in self._tracks.items():
                    if tracked_writer != writer_name:
                        continue
                    record = writer.get(record_id)
                    if track.erased:
                        assert record is None
                        assert writer.events(record_id) == []
                        assert record_id not in active_ids
                        continue
                    assert record is not None
                    assert record.scope == writer.scope
                    events = writer.events(record_id)
                    assert events
                    assert all(event.scope == writer.scope for event in events)
                    assert [event.sequence for event in events] == sorted(
                        event.sequence for event in events
                    )
                    if track.payload_present:
                        assert record.content == track.content
                    else:
                        assert record.content is None
                        assert record.source_refs == ()
                        assert record_id not in active_ids

            # A peer may independently be forgotten or erased, but operations
            # in one scope must never rewrite its scope or its still-live text.
            for track in self._tracks.values():
                other_writer_name = "right" if track.writer_name == "left" else "left"
                peer = self._tracks.get((other_writer_name, track.record_id))
                if peer is None:
                    continue
                record = self._writers[track.writer_name].get(track.record_id)
                peer_record = self._writers[other_writer_name].get(peer.record_id)
                if record is not None and peer_record is not None:
                    assert record.scope != peer_record.scope
                    if track.payload_present and peer.payload_present:
                        assert record.content == track.content
                        assert peer_record.content == peer.content
                        assert record.content != peer_record.content

    run_state_machine_as_test(LedgerLifecycleMachine, settings=state_machine_settings)
