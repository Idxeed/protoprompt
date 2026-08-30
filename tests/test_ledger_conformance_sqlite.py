"""SQLite execution wrapper for backend-neutral Memory Ledger conformance."""

from __future__ import annotations

from ledger_conformance.core import (
    assert_admission_boundary_and_strict_recall,
    assert_candidate_confirmation_and_content_free_events,
    assert_checkpoint_reopen_resume_and_selected_record_invalidation,
    assert_exact_scope_isolation_and_scoped_forget,
    assert_idempotent_retries_and_conflicting_event_reuse,
    assert_lifecycle_forget_source_and_hard_erase,
    assert_restart_and_setup_persistence,
)
from protoprompt.ledger import SqliteMemoryLedger


def test_sqlite_candidate_confirmation_and_content_free_events():
    assert_candidate_confirmation_and_content_free_events(SqliteMemoryLedger)


def test_sqlite_admission_boundary_and_strict_recall():
    assert_admission_boundary_and_strict_recall(SqliteMemoryLedger)


def test_sqlite_exact_scope_isolation_and_scoped_forget():
    assert_exact_scope_isolation_and_scoped_forget(SqliteMemoryLedger)


def test_sqlite_idempotent_retries_and_conflicting_event_reuse():
    assert_idempotent_retries_and_conflicting_event_reuse(SqliteMemoryLedger)


def test_sqlite_lifecycle_forget_source_and_hard_erase():
    assert_lifecycle_forget_source_and_hard_erase(SqliteMemoryLedger)


def test_sqlite_restart_and_setup_persistence(tmp_path):
    path = tmp_path / "ledger-conformance.db"

    def reopenable_ledger() -> SqliteMemoryLedger:
        return SqliteMemoryLedger(str(path))

    assert_restart_and_setup_persistence(reopenable_ledger)


def test_sqlite_checkpoint_reopen_resume_and_selected_record_invalidation(tmp_path):
    path = tmp_path / "ledger-checkpoint-conformance.db"

    def reopenable_ledger() -> SqliteMemoryLedger:
        return SqliteMemoryLedger(str(path))

    assert_checkpoint_reopen_resume_and_selected_record_invalidation(reopenable_ledger)
