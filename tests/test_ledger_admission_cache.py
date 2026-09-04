"""Fail-closed invalidation coverage for SQLite's immutable-admission cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

import protoprompt.ledger.sqlite as ledger_sqlite
from protoprompt.ledger import (
    LedgerStateError,
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.scope import MemoryScope


def test_external_audit_corruption_invalidates_a_warmed_active_read_cache(tmp_path):
    """A new SQLite data version must never leave a stale audit marker trusted."""

    path = tmp_path / "admission-cache.db"
    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()
    try:
        writer = MemoryWriter(
            ledger,
            scope=MemoryScope(tenant="cache", user="operator", thread="strict"),
            actor="cache-host",
        )
        gate = MemoryReviewGate(
            writer,
            origin=MemoryOrigin.HOST_ASSERTION,
            policy=MemoryAdmissionPolicy.safe_default(),
        )
        candidate = gate.ingress(
            kind=MemoryKind.FACT,
            source_ref="cache-source",
            evidence_refs=("cache-evidence",),
            confidence=0.95,
            asserted=True,
        ).submit("strict admitted record used to warm the cache")
        active = gate.confirm(gate.review(candidate.record_id), event_id="cache-confirm")

        # This first read validates the immutable sidecar and seeds only its
        # content-free marker in memory.
        assert [record.record_id for record in writer.list_active()] == [active.record_id]

        # Model an out-of-band file mutation.  It changes SQLite's
        # connection-visible data_version, so the next read must discard its
        # marker and re-run the full fail-closed audit validation.
        external = sqlite3.connect(path)
        try:
            external.execute(
                "DROP TRIGGER IF EXISTS "
                "protoprompt_memory_ledger_review_audits_reject_delete_v1"
            )
            external.execute(
                "DELETE FROM memory_review_audits WHERE record_id = ?",
                (active.record_id,),
            )
            external.execute(ledger_sqlite._REVIEW_AUDIT_DELETE_TRIGGER_SQL)
            external.commit()
        finally:
            external.close()

        with pytest.raises(LedgerStateError, match="missing a valid allow admission audit"):
            writer.list_active()
    finally:
        ledger.close()


def test_active_admission_cache_is_bounded_across_stable_read_only_scopes(
    monkeypatch: pytest.MonkeyPatch,
):
    """A long-lived reader cannot retain one marker per historic record forever."""

    monkeypatch.setattr(ledger_sqlite, "_ACTIVE_ADMISSION_CACHE_MAX_ENTRIES", 2)
    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        admitted: list[tuple[MemoryWriter, str]] = []
        for index in range(3):
            writer = MemoryWriter(
                ledger,
                scope=MemoryScope(
                    tenant="cache",
                    user="operator",
                    thread=f"stable-{index}",
                ),
                actor="cache-host",
            )
            gate = MemoryReviewGate(
                writer,
                origin=MemoryOrigin.HOST_ASSERTION,
                policy=MemoryAdmissionPolicy.safe_default(),
            )
            candidate = gate.ingress(
                kind=MemoryKind.FACT,
                source_ref=f"cache-source-{index}",
                confidence=0.95,
                asserted=True,
            ).submit(f"strict record {index}")
            gate.confirm(gate.review(candidate.record_id), event_id=f"cache-confirm-{index}")
            admitted.append((writer, candidate.record_id))

        # No further writes occur while distinct scope reads seed the cache.
        for writer, record_id in admitted:
            assert [record.record_id for record in writer.list_active()] == [record_id]

        assert len(ledger._active_admission_cache) <= 2
        assert all(len(marker) == 4 for marker in ledger._active_admission_cache)
    finally:
        ledger.close()


def test_cache_capacity_eviction_keeps_rows_that_were_hits_at_snapshot_start_validated(
    monkeypatch: pytest.MonkeyPatch,
):
    """Evict before batched audit fetches so a former hit cannot lose its sidecar."""

    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        scope = MemoryScope(tenant="cache", user="operator", thread="mixed-window")
        first_time = datetime(2042, 1, 1, tzinfo=timezone.utc)
        old_writer = MemoryWriter(
            ledger,
            scope=scope,
            actor="cache-old-host",
            clock=lambda: first_time,
        )
        old_gate = MemoryReviewGate(
            old_writer,
            origin=MemoryOrigin.HOST_ASSERTION,
            policy=MemoryAdmissionPolicy.safe_default(),
        )
        old_candidate = old_gate.ingress(
            kind=MemoryKind.FACT,
            source_ref="cache-old-source",
            confidence=0.95,
            asserted=True,
        ).submit("older strict record")
        old_gate.confirm(old_gate.review(old_candidate.record_id), event_id="cache-old-confirm")

        newer_writer = MemoryWriter(
            ledger,
            scope=scope,
            actor="cache-new-host",
            clock=lambda: first_time + timedelta(seconds=1),
        )
        newer_gate = MemoryReviewGate(
            newer_writer,
            origin=MemoryOrigin.HOST_ASSERTION,
            policy=MemoryAdmissionPolicy.safe_default(),
        )
        newer_candidate = newer_gate.ingress(
            kind=MemoryKind.FACT,
            source_ref="cache-new-source",
            confidence=0.95,
            asserted=True,
        ).submit("newer strict record")
        newer_gate.confirm(
            newer_gate.review(newer_candidate.record_id),
            event_id="cache-new-confirm",
        )

        # Seed a stable snapshot cache, then model the exact mixed state that
        # exposes a new record before a formerly cached older one.
        monkeypatch.setattr(ledger_sqlite, "_ACTIVE_ADMISSION_CACHE_MAX_ENTRIES", 2)
        assert len(old_writer.list_active()) == 2
        scope_id, scope_json = ledger_sqlite._scope_storage(scope)
        ledger._active_admission_cache.clear()
        ledger._active_admission_cache.add(
            (scope_id, scope_json, old_candidate.record_id, MemoryOrigin.HOST_ASSERTION.value)
        )
        monkeypatch.setattr(ledger_sqlite, "_ACTIVE_ADMISSION_CACHE_MAX_ENTRIES", 1)

        active = old_writer.list_active()
        assert [record.record_id for record in active] == [
            newer_candidate.record_id,
            old_candidate.record_id,
        ]
        # A snapshot larger than the bound is fully revalidated but not
        # retained, preventing both false failures and unbounded growth.
        assert ledger._active_admission_cache == set()
    finally:
        ledger.close()
