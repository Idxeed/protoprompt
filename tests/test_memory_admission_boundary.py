"""Regression tests for the v0.10 host-owned memory admission boundary.

These tests intentionally cover the boundary rather than policy ergonomics:
concrete ingress provenance cannot be promoted through the legacy writer,
reviews are single-gate capabilities, sidecars are immutable, and a v4
database receives only the safe legacy provenance that can be proven during
migration.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import json
import sqlite3
import threading

import pytest

import protoprompt.ledger.sqlite as ledger_sqlite
from protoprompt.ledger import (
    LedgerStateError,
    MemoryAdmissionAction,
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryState,
    MemoryTrust,
    MemoryWriter,
    SqliteMemoryLedger,
    StaleMemoryReviewError,
)
from protoprompt.ledger.recall import LedgerRecallPlanner
from protoprompt.ledger.types import canonical_json, scope_dict
from protoprompt.scope import MemoryScope


@pytest.fixture
def scope() -> MemoryScope:
    return MemoryScope(tenant="admission-acme", user="alice", thread="review-1")


@pytest.fixture
def ledger() -> SqliteMemoryLedger:
    store = SqliteMemoryLedger()
    store.setup()
    try:
        yield store
    finally:
        store.close()


def _writer(ledger: SqliteMemoryLedger, scope: MemoryScope) -> MemoryWriter:
    # Admission samples the writer clock before it enters its SQLite write
    # boundary; these ordinary boundary tests use the production clock.
    return MemoryWriter(ledger, scope=scope, actor="admission-host")


def _document_policy(*, reject: bool = False) -> MemoryAdmissionPolicy:
    return MemoryAdmissionPolicy(
        policy_id="reject-document-v1" if reject else "allow-document-v1",
        policy_version="1",
        allowed_origins=() if reject else (MemoryOrigin.DOCUMENT,),
        rejected_origins=(MemoryOrigin.DOCUMENT,) if reject else (),
        minimum_confidence=0.5,
    )


def _document_candidate(
    gate: MemoryReviewGate,
    *,
    content: str = "private admission payload sentinel",
    source_ref: str = "pdf:admission-boundary",
):
    return gate.ingress(
        kind=MemoryKind.FACT,
        source_ref=source_ref,
        evidence_refs=("pdf:admission-boundary:page:1",),
        confidence=0.9,
    ).submit(content)


def _stage_v4_schema(connection: sqlite3.Connection) -> None:
    """Remove every v5-only object to model an exact pre-admission Ledger."""

    for trigger_name in (
        "protoprompt_memory_ledger_admission_metadata_reject_update_v1",
        "protoprompt_memory_ledger_admission_metadata_reject_delete_v1",
        "protoprompt_memory_ledger_admission_metadata_reject_replace_v1",
        "protoprompt_memory_ledger_review_audits_reject_update_v1",
        "protoprompt_memory_ledger_review_audits_reject_delete_v1",
        "protoprompt_memory_ledger_review_audits_reject_replace_v1",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    connection.execute("DROP INDEX IF EXISTS idx_memory_review_audits_record")
    connection.execute("DROP TABLE memory_review_audits")
    connection.execute("DROP TABLE memory_record_admission_metadata")
    connection.execute(
        "UPDATE ledger_schema SET version = 4 WHERE component = 'memory_ledger'"
    )


def test_legacy_writer_confirm_cannot_bypass_concrete_origin_admission_audit(ledger, scope):
    """A gate-owned candidate must be promoted only with its sealed review.

    ``MemoryWriter.confirm`` remains the explicit compatibility escape hatch
    for raw ``unknown`` candidates.  It must not turn a v5 concrete-origin
    candidate into active memory, because that would skip the immutable review
    audit and make a policy gate merely advisory.
    """

    writer = _writer(ledger, scope)
    gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.DOCUMENT,
        policy=_document_policy(),
    )
    candidate = _document_candidate(gate)

    assert candidate.origin is MemoryOrigin.DOCUMENT
    assert candidate.state is MemoryState.CANDIDATE
    with pytest.raises(LedgerStateError):
        writer.confirm(
            candidate.record_id,
            expected_revision=candidate.revision,
            event_id="evt-direct-confirm-must-not-bypass",
        )

    untouched = writer.get(candidate.record_id)
    assert untouched is not None
    assert untouched.state is MemoryState.CANDIDATE
    assert untouched.trust is MemoryTrust.UNTRUSTED
    assert writer.admission_audits(candidate.record_id) == []
    assert [event.event_type.value for event in writer.events(candidate.record_id)] == [
        "observed"
    ]

    # The older raw writer path remains intentionally available to a trusted
    # host for records whose provenance is explicitly ``unknown``.
    raw = writer.propose(
        kind=MemoryKind.FACT,
        content="legacy trusted host payload",
        source_ref="host:legacy-escape",
        record_id="raw-compatible-candidate",
        event_id="evt-raw-compatible-observed",
    )
    active = writer.confirm(
        raw.record_id,
        expected_revision=raw.revision,
        event_id="evt-raw-compatible-confirmed",
    )
    assert active.origin is MemoryOrigin.UNKNOWN
    assert active.state is MemoryState.ACTIVE


def test_review_is_sealed_to_its_gate_and_explain_is_content_free(ledger, scope):
    writer = _writer(ledger, scope)
    policy = _document_policy()
    gate = MemoryReviewGate(writer, origin=MemoryOrigin.DOCUMENT, policy=policy)
    other_gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.DOCUMENT,
        policy=policy,
    )
    content = "sealed review payload sentinel"
    source_ref = "pdf:sealed-review"
    candidate = _document_candidate(gate, content=content, source_ref=source_ref)
    review = gate.review(candidate.record_id)

    assert review.action is MemoryAdmissionAction.ALLOW
    explained = json.dumps(review.explain(), ensure_ascii=False)
    assert content not in explained
    assert source_ref not in explained
    assert candidate.record_id not in explained
    assert scope.correlation_id() not in explained

    # A second gate with the same public policy and actor is still a different
    # capability.  Copying public review fields also invalidates its tag.
    with pytest.raises(StaleMemoryReviewError):
        other_gate.confirm(review, event_id="evt-other-gate-cannot-apply")
    with pytest.raises(StaleMemoryReviewError):
        gate.confirm(
            replace(review, reason_code="tampered_review"),
            event_id="evt-tampered-review-cannot-apply",
        )

    untouched = writer.get(candidate.record_id)
    assert untouched is not None
    assert untouched.state is MemoryState.CANDIDATE
    assert writer.admission_audits(candidate.record_id) == []

    active = gate.confirm(review, event_id="evt-sealed-review-confirm")
    assert active.state is MemoryState.ACTIVE
    assert active.trust is MemoryTrust.HOST_CONFIRMED
    audits = writer.admission_audits(candidate.record_id)
    assert len(audits) == 1
    assert audits[0].action is MemoryAdmissionAction.ALLOW


def test_ingress_cannot_be_reconfigured_by_the_submitter(ledger, scope):
    writer = _writer(ledger, scope)
    document_gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.DOCUMENT,
        policy=_document_policy(),
    )
    with pytest.raises(ValueError, match="host_assertion"):
        document_gate.ingress(
            kind=MemoryKind.FACT,
            source_ref="pdf:invalid-assertion",
            asserted=True,
        )

    host_gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.HOST_ASSERTION,
        policy=MemoryAdmissionPolicy.safe_default(),
    )
    with pytest.raises(ValueError, match="asserted"):
        host_gate.ingress(kind=MemoryKind.FACT, source_ref="host:missing-assertion")

    ingress = document_gate.ingress(
        kind=MemoryKind.FACT,
        source_ref="pdf:fixed-boundary",
        confidence=0.7,
    )
    with pytest.raises(TypeError):
        ingress.submit("attempted authority override", origin=MemoryOrigin.HOST_ASSERTION)
    with pytest.raises(TypeError):
        ingress.submit("attempted identity override", record_id="caller-chosen-id")

    candidate = ingress.submit("only content crosses the ingress")
    assert candidate.origin is MemoryOrigin.DOCUMENT
    assert candidate.confidence == 0.7
    assert candidate.source_refs == ("pdf:fixed-boundary",)


def test_reject_audit_is_immutable_content_free_and_hard_erase_cascades(ledger, scope):
    writer = _writer(ledger, scope)
    gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.DOCUMENT,
        policy=_document_policy(reject=True),
    )
    content = "do not retain this rejection payload"
    source_ref = "pdf:reject-me"
    candidate = _document_candidate(gate, content=content, source_ref=source_ref)
    review = gate.review(candidate.record_id)
    assert review.action is MemoryAdmissionAction.REJECT

    receipt = gate.reject(review, event_id="evt-rejected-admission")
    assert receipt.record_id == candidate.record_id
    rejected = writer.get(candidate.record_id)
    assert rejected is not None
    assert rejected.state is MemoryState.RETRACTED
    assert rejected.content is None

    audits = writer.admission_audits(candidate.record_id)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.action is MemoryAdmissionAction.REJECT
    audit_json = json.dumps(audit.to_dict(), ensure_ascii=False)
    assert content not in audit_json
    assert source_ref not in audit_json

    with pytest.raises(sqlite3.DatabaseError, match="memory admission metadata are immutable"):
        ledger._conn.execute(
            "UPDATE memory_record_admission_metadata SET origin = ? WHERE record_id = ?",
            (MemoryOrigin.HOST_ASSERTION.value, candidate.record_id),
        )
    ledger._conn.rollback()
    with pytest.raises(sqlite3.DatabaseError, match="memory review audits are immutable"):
        ledger._conn.execute(
            "UPDATE memory_review_audits SET policy_id = ? WHERE event_id = ?",
            ("mutated-policy", audit.event_id),
        )
    ledger._conn.rollback()
    with pytest.raises(sqlite3.DatabaseError, match="memory admission metadata are append-only"):
        ledger._conn.execute(
            "INSERT OR REPLACE INTO memory_record_admission_metadata "
            "(scope_id, scope_json, record_id, origin) SELECT scope_id, scope_json, "
            "record_id, ? FROM memory_record_admission_metadata WHERE record_id = ?",
            (MemoryOrigin.HOST_ASSERTION.value, candidate.record_id),
        )
    ledger._conn.rollback()
    with pytest.raises(sqlite3.DatabaseError, match="memory review audits are append-only"):
        ledger._conn.execute(
            "INSERT OR REPLACE INTO memory_review_audits "
            "(scope_id, scope_json, event_id, record_id, candidate_revision, origin, "
            "policy_id, policy_version, policy_fingerprint, action, reason_code) "
            "SELECT scope_id, scope_json, event_id, record_id, candidate_revision, origin, "
            "?, policy_version, policy_fingerprint, action, reason_code "
            "FROM memory_review_audits WHERE event_id = ?",
            ("mutated-policy", audit.event_id),
        )
    ledger._conn.rollback()

    hard_receipt = writer.erase(
        candidate.record_id,
        expected_revision=rejected.revision,
        event_id="evt-hard-erase-rejected-admission",
    )
    assert hard_receipt.events_deleted == 2
    for table_name in ("memory_record_admission_metadata", "memory_review_audits"):
        count = ledger._conn.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE record_id = ?",
            (candidate.record_id,),
        ).fetchone()[0]
        assert count == 0


def test_gate_samples_a_deterministic_writer_clock_before_its_sqlite_write_boundary(scope):
    """Admission uses the writer's clock without invoking it under ``BEGIN``.

    The clock deliberately performs one unrelated ledger write when the gate
    samples it.  That is safe only before the admission transaction starts;
    executing the callback under the write boundary would re-enter SQLite and
    fail instead of admitting the reviewed candidate.
    """

    timestamp = datetime(2034, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    store = SqliteMemoryLedger()
    store.setup()
    injecting = False
    injected = False

    def clock() -> datetime:
        nonlocal injecting, injected
        if not injecting and not injected:
            injecting = True
            try:
                writer.propose(
                    kind=MemoryKind.FACT,
                    content="clock callback wrote this independently",
                    source_ref="host:clock-callback",
                    record_id="clock-callback-record",
                    event_id="evt-clock-callback-observed",
                )
            finally:
                injecting = False
            injected = True
        return timestamp

    try:
        writer = MemoryWriter(store, scope=scope, actor="clock-host", clock=clock)
        # The first clock call creates the candidate, so keep the callback
        # dormant until the admission action itself.
        injected = True
        gate = MemoryReviewGate(
            writer,
            origin=MemoryOrigin.HOST_ASSERTION,
            policy=MemoryAdmissionPolicy.safe_default(),
        )
        candidate = gate.ingress(
            kind=MemoryKind.FACT,
            source_ref="host:deterministic-admission",
            confidence=0.9,
            asserted=True,
        ).submit("deterministic host assertion")
        review = gate.review(candidate.record_id)

        injected = False
        active = gate.confirm(review, event_id="evt-deterministic-admission-confirm")

        assert injected is True
        assert active.updated_at == timestamp
        assert active.created_at == timestamp
        callback_record = writer.get("clock-callback-record")
        assert callback_record is not None
        assert callback_record.origin is MemoryOrigin.UNKNOWN
    finally:
        store.close()


def test_hard_erase_reinstalls_append_only_sidecar_guards_across_restart(tmp_path, scope):
    """Guard suspension for FK cascades cannot leave a reopened DB writable."""

    path = tmp_path / "admission-hard-erase-restart.db"
    store = SqliteMemoryLedger(str(path))
    store.setup()
    writer = _writer(store, scope)
    gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.DOCUMENT,
        policy=_document_policy(),
    )
    candidate = _document_candidate(gate, content="erase before restart")
    active = gate.confirm(gate.review(candidate.record_id), event_id="evt-before-restart")
    writer.erase(
        candidate.record_id,
        expected_revision=active.revision,
        event_id="evt-hard-erase-before-restart",
    )
    trigger_names = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND name LIKE 'protoprompt_memory_ledger_%'"
        ).fetchall()
    }
    assert {
        "protoprompt_memory_ledger_admission_metadata_reject_update_v1",
        "protoprompt_memory_ledger_admission_metadata_reject_delete_v1",
        "protoprompt_memory_ledger_admission_metadata_reject_replace_v1",
        "protoprompt_memory_ledger_review_audits_reject_update_v1",
        "protoprompt_memory_ledger_review_audits_reject_delete_v1",
        "protoprompt_memory_ledger_review_audits_reject_replace_v1",
    } <= trigger_names
    store.close()

    restored = SqliteMemoryLedger(str(path))
    try:
        # Explicit setup both validates and behaviorally re-proves guard
        # ownership after a process restart.
        restored.setup()
        restored_writer = _writer(restored, scope)
        restored_gate = MemoryReviewGate(
            restored_writer,
            origin=MemoryOrigin.DOCUMENT,
            policy=_document_policy(),
        )
        restored_candidate = _document_candidate(
            restored_gate,
            content="persisted guard target",
            source_ref="pdf:restart-guard",
        )
        restored_gate.confirm(
            restored_gate.review(restored_candidate.record_id),
            event_id="evt-restart-guard-confirm",
        )

        with pytest.raises(sqlite3.DatabaseError, match="memory admission metadata are immutable"):
            restored._conn.execute(
                "UPDATE memory_record_admission_metadata SET origin = ? WHERE record_id = ?",
                (MemoryOrigin.HOST_ASSERTION.value, restored_candidate.record_id),
            )
        restored._conn.rollback()
        with pytest.raises(sqlite3.DatabaseError, match="memory admission metadata are append-only"):
            restored._conn.execute(
                "DELETE FROM memory_record_admission_metadata WHERE record_id = ?",
                (restored_candidate.record_id,),
            )
        restored._conn.rollback()
        with pytest.raises(sqlite3.DatabaseError, match="memory review audits are immutable"):
            restored._conn.execute(
                "UPDATE memory_review_audits SET policy_id = ? WHERE record_id = ?",
                ("mutated-policy", restored_candidate.record_id),
            )
        restored._conn.rollback()
        with pytest.raises(sqlite3.DatabaseError, match="memory review audits are append-only"):
            restored._conn.execute(
                "DELETE FROM memory_review_audits WHERE record_id = ?",
                (restored_candidate.record_id,),
            )
        restored._conn.rollback()
    finally:
        restored.close()


def test_v4_to_v5_migration_backfills_only_live_payloads_as_legacy_unknown(tmp_path, scope):
    """v5 must not invent modern provenance or a review audit for old rows."""

    path = tmp_path / "v4-admission-ledger.db"
    original = SqliteMemoryLedger(str(path))
    original.setup()
    writer = _writer(original, scope)
    live = writer.propose(
        kind=MemoryKind.FACT,
        content="v4 live payload",
        source_ref="v4:live",
        record_id="v4-live",
        event_id="evt-v4-live-observed",
    )
    forgotten = writer.propose(
        kind=MemoryKind.FACT,
        content="v4 removed payload",
        source_ref="v4:removed",
        record_id="v4-forgotten",
        event_id="evt-v4-forgotten-observed",
    )
    writer.forget(
        forgotten.record_id,
        expected_revision=forgotten.revision,
        event_id="evt-v4-forgotten",
    )
    original.close()

    # Stage a database that has the exact v4 tables and schema marker.  This
    # models a real pre-v5 ledger, rather than relying on an artificial row.
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "DROP TRIGGER IF EXISTS protoprompt_memory_ledger_admission_metadata_reject_update_v1"
        )
        connection.execute(
            "DROP TRIGGER IF EXISTS protoprompt_memory_ledger_admission_metadata_reject_delete_v1"
        )
        connection.execute(
            "DROP TRIGGER IF EXISTS protoprompt_memory_ledger_review_audits_reject_update_v1"
        )
        connection.execute(
            "DROP TRIGGER IF EXISTS protoprompt_memory_ledger_review_audits_reject_delete_v1"
        )
        connection.execute("DROP INDEX IF EXISTS idx_memory_review_audits_record")
        connection.execute("DROP TABLE memory_review_audits")
        connection.execute("DROP TABLE memory_record_admission_metadata")
        connection.execute(
            "UPDATE ledger_schema SET version = 4 WHERE component = 'memory_ledger'"
        )
        connection.commit()
    finally:
        connection.close()

    # A dry run is genuinely read-only for an already valid v4 database: no
    # event bytes, catalog objects, or schema marker can be changed before an
    # operator deliberately invokes setup.
    connection = sqlite3.connect(path)
    try:
        v4_events = connection.execute(
            "SELECT * FROM memory_events ORDER BY sequence"
        ).fetchall()
        v4_catalog = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        assert connection.execute(
            "SELECT version FROM ledger_schema WHERE component = 'memory_ledger'"
        ).fetchone()[0] == 4
    finally:
        connection.close()

    upgraded = SqliteMemoryLedger(str(path))
    try:
        expected_dry_run = {
            "component": "memory_ledger",
            "from_version": 4,
            "to_version": 5,
            "changes_required": True,
            "actions": ["add v5 admission provenance and review audit tables"],
        }
        backup_path = tmp_path / "v4-before-admission-upgrade.backup.db"
        upgraded.backup(str(backup_path))
        backup = SqliteMemoryLedger(str(backup_path))
        try:
            assert backup.schema_version() == 4
            assert backup.dry_run_setup() == expected_dry_run
        finally:
            backup.close()

        assert upgraded.dry_run_setup() == expected_dry_run
        connection = sqlite3.connect(path)
        try:
            assert connection.execute(
                "SELECT * FROM memory_events ORDER BY sequence"
            ).fetchall() == v4_events
            assert connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall() == v4_catalog
            assert connection.execute(
                "SELECT version FROM ledger_schema WHERE component = 'memory_ledger'"
            ).fetchone()[0] == 4
        finally:
            connection.close()

        upgraded.setup()
        assert upgraded.schema_version() == 5
        connection = sqlite3.connect(path)
        try:
            assert connection.execute(
                "SELECT * FROM memory_events ORDER BY sequence"
            ).fetchall() == v4_events
        finally:
            connection.close()

        migrated_live = upgraded.get(scope, live.record_id)
        assert migrated_live is not None
        assert migrated_live.origin is MemoryOrigin.LEGACY_UNKNOWN
        assert migrated_live.content == "v4 live payload"
        assert upgraded.admission_audits(scope, live.record_id) == []

        migrated_forgotten = upgraded.get(scope, forgotten.record_id)
        assert migrated_forgotten is not None
        assert migrated_forgotten.origin is MemoryOrigin.UNKNOWN
        assert migrated_forgotten.content is None
        assert upgraded.admission_audits(scope, forgotten.record_id) == []
    finally:
        upgraded.close()


@pytest.mark.parametrize(
    "forged_origin",
    (MemoryOrigin.UNKNOWN, MemoryOrigin.DOCUMENT),
    ids=("raw-origin", "mismatched-record-origin"),
)
def test_forged_raw_quarantine_audit_fails_admission_reads_and_setup(
    ledger,
    scope,
    forged_origin,
):
    """A sidecar INSERT cannot manufacture a review for a raw lifecycle event.

    The first case uses a forbidden raw/unknown origin; the second uses a
    concrete audit origin that disagrees with the raw record's persisted
    origin.  Both also have the raw quarantine command fingerprint rather
    than an admission-review fingerprint.
    """

    writer = _writer(ledger, scope)
    candidate = writer.propose(
        kind=MemoryKind.FACT,
        content="raw lifecycle must not acquire a forged review",
        source_ref="host:raw-quarantine",
        record_id="raw-quarantine-record",
        event_id="evt-raw-quarantine-observed",
    )
    writer.quarantine(
        candidate.record_id,
        expected_revision=candidate.revision,
        reason_code="raw-quarantine",
        event_id="evt-raw-quarantine",
    )

    ledger._conn.execute(
        "INSERT INTO memory_review_audits "
        "(scope_id, scope_json, event_id, record_id, candidate_revision, origin, "
        "policy_id, policy_version, policy_fingerprint, action, reason_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.correlation_id(),
            canonical_json(scope_dict(scope)),
            "evt-raw-quarantine",
            candidate.record_id,
            candidate.revision,
            forged_origin.value,
            "forged-policy",
            "1",
            "f" * 64,
            MemoryAdmissionAction.QUARANTINE.value,
            "raw-quarantine",
        ),
    )
    ledger._conn.commit()

    with pytest.raises(LedgerStateError, match="admission audit"):
        writer.admission_audits(candidate.record_id)
    with pytest.raises(LedgerStateError, match="admission audit"):
        ledger.dry_run_setup()


def test_restart_recovery_reads_durable_outcome_without_replaying_sealed_review(
    tmp_path,
    scope,
):
    """A process-local review cannot be reused after restart, but receipts can."""

    path = tmp_path / "admission-recovery.db"
    policy = _document_policy()
    original = SqliteMemoryLedger(str(path))
    try:
        original.setup()
        writer = _writer(original, scope)
        gate = MemoryReviewGate(
            writer,
            origin=MemoryOrigin.DOCUMENT,
            policy=policy,
        )
        candidate = _document_candidate(
            gate,
            content="restart recovery admission sentinel",
            source_ref="pdf:restart-recovery",
        )
        review = gate.review(candidate.record_id)
        applied = gate.confirm(review, event_id="evt-restart-recovery-review")
    finally:
        original.close()

    restored = SqliteMemoryLedger(str(path))
    try:
        restored_writer = _writer(restored, scope)
        recreated_gate = MemoryReviewGate(
            restored_writer,
            origin=MemoryOrigin.DOCUMENT,
            policy=policy,
        )

        with pytest.raises(StaleMemoryReviewError, match="different admission gate"):
            recreated_gate.confirm(review, event_id="evt-restart-recovery-review")

        events = restored_writer.events(candidate.record_id)
        audits = restored_writer.admission_audits(candidate.record_id)
        assert events[-1].event_id == "evt-restart-recovery-review"
        assert events[-1].event_type.value == "confirmed"
        assert len(audits) == 1
        assert audits[0].event_id == "evt-restart-recovery-review"
        assert audits[0].record_id == candidate.record_id
        assert audits[0].action is MemoryAdmissionAction.ALLOW

        erased = restored_writer.erase(
            candidate.record_id,
            expected_revision=applied.revision,
            event_id="evt-restart-recovery-hard-erase",
        )
        assert erased.events_deleted == len(events)
        assert restored_writer.get(candidate.record_id) is None
        assert restored_writer.events(candidate.record_id) == []
        assert restored_writer.admission_audits(candidate.record_id) == []
    finally:
        restored.close()


def test_v4_migrated_active_legacy_unknown_stays_recallable(tmp_path, scope):
    """The add-only v5 admission migration does not withdraw existing recall."""

    path = tmp_path / "v4-active-legacy-recall.db"
    original = SqliteMemoryLedger(str(path))
    try:
        original.setup()
        writer = _writer(original, scope)
        candidate = writer.propose(
            kind=MemoryKind.FACT,
            content="legacy active recall sentinel",
            source_ref="v4:active-recall",
            confidence=0.9,
            record_id="v4-active-recall",
            event_id="evt-v4-active-observed",
        )
        active = writer.confirm(
            candidate.record_id,
            expected_revision=candidate.revision,
            event_id="evt-v4-active-confirmed",
        )
        assert active.origin is MemoryOrigin.UNKNOWN
    finally:
        original.close()

    connection = sqlite3.connect(path)
    try:
        _stage_v4_schema(connection)
        connection.commit()
    finally:
        connection.close()

    upgraded = SqliteMemoryLedger(str(path))
    try:
        upgraded.setup()
        assert upgraded.dry_run_setup()["changes_required"] is False
        migrated_writer = _writer(upgraded, scope)
        migrated = migrated_writer.get(candidate.record_id)
        assert migrated is not None
        assert migrated.state is MemoryState.ACTIVE
        assert migrated.origin is MemoryOrigin.LEGACY_UNKNOWN
        assert migrated_writer.admission_audits(candidate.record_id) == []
        assert [record.record_id for record in migrated_writer.list_active()] == [
            candidate.record_id
        ]

        planner = LedgerRecallPlanner(migrated_writer)
        context = planner.resolve(
            planner.plan(
                task="legacy active recall sentinel",
                token_budget=500,
                byte_budget=10_000,
            )
        )
        assert "legacy active recall sentinel" in context.render_data()
    finally:
        upgraded.close()


def test_model_facing_ingress_wire_shape_accepts_only_content(ledger, scope):
    """A model RPC adapter has exactly one caller-controlled field: text."""

    writer = _writer(ledger, scope)
    ingress = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.DOCUMENT,
        policy=_document_policy(),
    ).ingress(
        kind=MemoryKind.FACT,
        source_ref="pdf:model-rpc-fixed-source",
        evidence_refs=("pdf:model-rpc-fixed-source:page:1",),
        confidence=0.8,
    )

    signature = inspect.signature(ingress.submit)
    assert tuple(signature.parameters) == ("content",)
    assert not hasattr(ingress, "propose")

    # This models decoding a model-tool/RPC request directly into keyword
    # arguments.  Every authority-bearing field must fail at the transport
    # boundary rather than being silently ignored or honoured.
    forbidden_fields = {
        "origin": MemoryOrigin.HOST_ASSERTION,
        "kind": MemoryKind.PROCEDURE,
        "source_ref": "model:chosen-source",
        "evidence_refs": ("model:chosen-evidence",),
        "confidence": 1.0,
        "retention_policy": "model-chosen-policy",
        "valid_from": "2034-01-01T00:00:00Z",
        "valid_until": "2034-01-02T00:00:00Z",
        "asserted": True,
        "record_id": "model-chosen-record",
        "event_id": "model-chosen-event",
        "scope": {"tenant": "another-tenant"},
        "actor": "model",
    }
    for field_name, value in forbidden_fields.items():
        with pytest.raises(TypeError):
            ingress.submit("untrusted RPC payload", **{field_name: value})

    candidate = ingress.submit(content="the single permitted RPC field")
    assert candidate.origin is MemoryOrigin.DOCUMENT
    assert candidate.kind is MemoryKind.FACT
    assert candidate.confidence == 0.8
    assert candidate.source_refs == ("pdf:model-rpc-fixed-source",)
    assert candidate.evidence_refs == ("pdf:model-rpc-fixed-source:page:1",)


def test_allow_rechecks_ledger_utc_after_waiting_for_a_write_lock(
    tmp_path,
    scope,
    monkeypatch,
):
    """A queued admission cannot cross a real-time validity deadline.

    The host writer clock is intentionally frozen before the deadline.  A
    second ledger holds ``BEGIN IMMEDIATE`` while the first waits; only after
    the blocked admission has attempted that boundary does the Ledger's own
    UTC clock advance beyond ``valid_until``.  No confirmation event or audit
    may be written.
    """

    class _BeginObserver:
        def __init__(self, connection: sqlite3.Connection, entered: threading.Event):
            self._connection = connection
            self._entered = entered

        def execute(self, sql: str, parameters=()):
            if sql.strip().upper() == "BEGIN IMMEDIATE":
                self._entered.set()
            return self._connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    path = tmp_path / "admission-expiry-lock-wait.db"
    timestamp = datetime(2034, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    valid_until = timestamp + timedelta(seconds=1)
    ledger_utc = {"now": timestamp}
    first_ledger = SqliteMemoryLedger(str(path))
    first_ledger.setup()
    second_ledger = SqliteMemoryLedger(str(path))
    raw_connection = first_ledger._conn
    confirm_thread: threading.Thread | None = None
    try:
        writer = MemoryWriter(
            first_ledger,
            scope=scope,
            actor="expiry-host",
            clock=lambda: timestamp,
        )
        gate = MemoryReviewGate(
            writer,
            origin=MemoryOrigin.DOCUMENT,
            policy=_document_policy(),
        )
        candidate = gate.ingress(
            kind=MemoryKind.FACT,
            source_ref="pdf:expiry-while-waiting",
            confidence=0.9,
            valid_until=valid_until,
        ).submit("this candidate expires while SQLite is busy")
        review = gate.review(candidate.record_id)

        begin_entered = threading.Event()
        first_ledger._conn = _BeginObserver(raw_connection, begin_entered)
        monkeypatch.setattr(ledger_sqlite, "utc_now", lambda: ledger_utc["now"])
        result: dict[str, object] = {}
        errors: list[BaseException] = []

        def run_confirm() -> None:
            try:
                result["record"] = gate.confirm(
                    review,
                    event_id="evt-expired-while-waiting",
                )
            except BaseException as exc:  # assertion below owns worker failures
                errors.append(exc)

        with second_ledger._lock:
            with second_ledger._write_transaction_locked():
                confirm_thread = threading.Thread(target=run_confirm)
                confirm_thread.start()
                assert begin_entered.wait(timeout=2)
                ledger_utc["now"] = valid_until + timedelta(seconds=1)

        confirm_thread.join(timeout=2)
        assert not confirm_thread.is_alive()
        assert "record" not in result
        assert len(errors) == 1
        assert isinstance(errors[0], LedgerStateError)
        assert "validity already ended" in str(errors[0])

        unchanged = writer.get(candidate.record_id)
        assert unchanged is not None
        assert unchanged.state is MemoryState.CANDIDATE
        assert writer.admission_audits(candidate.record_id) == []
        assert [event.event_type.value for event in writer.events(candidate.record_id)] == [
            "observed"
        ]
    finally:
        if confirm_thread is not None:
            confirm_thread.join(timeout=2)
        first_ledger._conn = raw_connection
        second_ledger.close()
        first_ledger.close()


def test_default_reader_excludes_concrete_active_record_without_allow_audit(tmp_path, scope):
    """A corrupted concrete-origin active row is never eligible for recall."""

    path = tmp_path / "concrete-active-without-audit.db"
    store = SqliteMemoryLedger(str(path))
    store.setup()
    try:
        writer = _writer(store, scope)
        gate = MemoryReviewGate(
            writer,
            origin=MemoryOrigin.DOCUMENT,
            policy=_document_policy(),
        )
        candidate = _document_candidate(
            gate,
            content="unreviewed concrete active sentinel",
            source_ref="pdf:unreviewed-active",
        )

        # Simulate an out-of-band SQLite corruption: the record is marked
        # active and host-confirmed but no paired allow audit/event exists.
        external = sqlite3.connect(path)
        try:
            external.execute(
                "UPDATE memory_records SET state = ?, trust = ? "
                "WHERE scope_id = ? AND scope_json = ? AND record_id = ?",
                (
                    MemoryState.ACTIVE.value,
                    MemoryTrust.HOST_CONFIRMED.value,
                    scope.correlation_id(),
                    canonical_json(scope_dict(scope)),
                    candidate.record_id,
                ),
            )
            external.commit()
        finally:
            external.close()

        malformed = writer.get(candidate.record_id)
        assert malformed is not None
        assert malformed.state is MemoryState.ACTIVE
        assert malformed.trust is MemoryTrust.HOST_CONFIRMED
        assert malformed.origin is MemoryOrigin.DOCUMENT
        # The default reader fails closed rather than returning a corrupted
        # record to a prompt.  A caller can neither observe it as active nor
        # continue with a partial active-memory snapshot.
        with pytest.raises(LedgerStateError, match="missing a valid allow admission audit"):
            writer.list_active()
        with pytest.raises(LedgerStateError, match="missing an allow admission audit"):
            store.dry_run_setup()
    finally:
        store.close()


def test_default_reader_rejects_document_audit_bound_to_another_records_confirmation(
    tmp_path,
    scope,
):
    """A valid confirmed event cannot be reused as another record's audit."""

    path = tmp_path / "cross-record-admission-audit.db"
    store = SqliteMemoryLedger(str(path))
    store.setup()
    try:
        writer = _writer(store, scope)
        raw = writer.propose(
            kind=MemoryKind.FACT,
            content="the legitimate raw record",
            source_ref="host:raw-confirmed",
            record_id="raw-confirmed-record",
            event_id="evt-raw-confirmed-observed",
        )
        writer.confirm(
            raw.record_id,
            expected_revision=raw.revision,
            event_id="evt-raw-confirmed",
        )
        gate = MemoryReviewGate(
            writer,
            origin=MemoryOrigin.DOCUMENT,
            policy=_document_policy(),
        )
        document = _document_candidate(
            gate,
            content="the corrupt document candidate",
            source_ref="pdf:cross-record-audit",
        )

        # Both foreign keys hold: the audit points to a real document record
        # and a real, valid CONFIRMED event.  Its event nevertheless belongs
        # to ``raw``, so it must never qualify the document for recall.
        external = sqlite3.connect(path)
        try:
            external.execute(
                "UPDATE memory_records SET state = ?, trust = ? "
                "WHERE scope_id = ? AND scope_json = ? AND record_id = ?",
                (
                    MemoryState.ACTIVE.value,
                    MemoryTrust.HOST_CONFIRMED.value,
                    scope.correlation_id(),
                    canonical_json(scope_dict(scope)),
                    document.record_id,
                ),
            )
            external.execute(
                "INSERT INTO memory_review_audits "
                "(scope_id, scope_json, event_id, record_id, candidate_revision, origin, "
                "policy_id, policy_version, policy_fingerprint, action, reason_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scope.correlation_id(),
                    canonical_json(scope_dict(scope)),
                    "evt-raw-confirmed",
                    document.record_id,
                    document.revision,
                    MemoryOrigin.DOCUMENT.value,
                    "forged-cross-record-policy",
                    "1",
                    "f" * 64,
                    MemoryAdmissionAction.ALLOW.value,
                    "forged-cross-record-allow",
                ),
            )
            external.commit()
        finally:
            external.close()

        with pytest.raises(LedgerStateError, match="admission audit"):
            writer.list_active()
        with pytest.raises(LedgerStateError, match="admission audit"):
            store.dry_run_setup()
    finally:
        store.close()
