"""Tests for the sealed v1 Ledger storage capability contract."""

from __future__ import annotations

import pytest

from protoprompt.ledger.storage_conformance import (
    LEDGER_STORAGE_CAPABILITIES_SCHEMA_VERSION,
    LEDGER_STORAGE_CONTRACT_ID,
    LEDGER_STORAGE_CONTRACT_VERSION,
    LEDGER_TARGET_STORAGE_SCHEMA_VERSION,
    STRICT_HOST_LEDGER_SEMANTIC_PROFILE,
    LedgerBackendId,
    LedgerBackupMode,
    LedgerSetupMode,
    LedgerStorageCapabilities,
    LedgerStorageConformanceError,
    postgres_v7_storage_capabilities,
    sqlite_v7_storage_capabilities,
)
from ledger_conformance.v1 import (
    LEDGER_STORAGE_CONFORMANCE_V1_CHECK_IDS,
    run_ledger_storage_conformance_v1,
)
from protoprompt.ledger import PostgresMemoryLedger, SqliteMemoryLedger
from protoprompt.ledger import postgres as postgres_module


def test_built_in_capabilities_are_content_free_and_operationally_distinct():
    sqlite = sqlite_v7_storage_capabilities()
    postgres = postgres_v7_storage_capabilities()

    assert sqlite.explain() == {
        "descriptor_schema_version": LEDGER_STORAGE_CAPABILITIES_SCHEMA_VERSION,
        "contract_id": LEDGER_STORAGE_CONTRACT_ID,
        "contract_version": LEDGER_STORAGE_CONTRACT_VERSION,
        "backend_id": "sqlite_v7",
        "semantic_profile": STRICT_HOST_LEDGER_SEMANTIC_PROFILE,
        "record_schema_version": 1,
        "target_storage_schema_version": LEDGER_TARGET_STORAGE_SCHEMA_VERSION,
        "setup_mode": "in_place_migration",
        "backup_mode": "file_copy",
    }
    assert postgres.explain() == {
        **sqlite.explain(),
        "backend_id": "postgres_v7",
        "setup_mode": "fresh_v7_only",
        "backup_mode": "operator_managed",
    }
    assert "path" not in str(sqlite.explain()).lower()
    assert "path" not in str(postgres.explain()).lower()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"descriptor_schema_version": 2}, "capabilities schema"),
        ({"descriptor_schema_version": True}, "capabilities schema"),
        ({"descriptor_schema_version": 1.0}, "capabilities schema"),
        ({"contract_id": "another.contract"}, "contract id"),
        ({"contract_version": 2}, "contract version"),
        ({"contract_version": True}, "contract version"),
        ({"contract_version": 1.0}, "contract version"),
        ({"record_schema_version": 2}, "record_schema_version"),
        ({"record_schema_version": True}, "record_schema_version"),
        ({"record_schema_version": 1.0}, "record_schema_version"),
        ({"target_storage_schema_version": 5}, "target_storage_schema_version"),
        ({"target_storage_schema_version": True}, "target_storage_schema_version"),
        ({"target_storage_schema_version": 6.0}, "target_storage_schema_version"),
        ({"semantic_profile": "other_profile_v1"}, "semantic profile"),
        ({"semantic_profile": "invalid profile"}, "semantic_profile"),
        ({"backend_id": "not-a-built-in"}, "backend_id"),
        ({"setup_mode": "not-a-setup-mode"}, "setup_mode"),
        ({"backup_mode": "not-a-backup-mode"}, "backup_mode"),
        (
            {
                "backend_id": LedgerBackendId.SQLITE_V7,
                "setup_mode": LedgerSetupMode.FRESH_V7_ONLY,
            },
            "sqlite_v7 requires",
        ),
        (
            {
                "backend_id": LedgerBackendId.POSTGRES_V7,
                "backup_mode": LedgerBackupMode.FILE_COPY,
            },
            "postgres_v7 requires",
        ),
    ],
)
def test_capability_contract_rejects_schema_and_backend_mode_drift(kwargs, message):
    with pytest.raises(LedgerStorageConformanceError, match=message):
        LedgerStorageCapabilities(**kwargs)


def test_descriptor_normalizes_public_enum_inputs_without_a_storage_connection():
    postgres = LedgerStorageCapabilities(
        backend_id="postgres_v7",
        setup_mode="fresh_v7_only",
        backup_mode="operator_managed",
    )

    assert postgres.backend_id is LedgerBackendId.POSTGRES_V7
    assert postgres.setup_mode is LedgerSetupMode.FRESH_V7_ONLY
    assert postgres.backup_mode is LedgerBackupMode.OPERATOR_MANAGED


def test_v1_semantic_profile_has_a_frozen_named_check_list():
    assert LEDGER_STORAGE_CONFORMANCE_V1_CHECK_IDS == (
        "candidate_confirmation_content_free_events",
        "audited_admission_strict_recall",
        "exact_scope_isolation_scoped_forget",
        "idempotent_retries_event_reuse",
        "lifecycle_source_revoke_hard_erase",
        "restart_setup_persistence",
        "sealed_checkpoint_restart_invalidation",
    )


def test_builtin_classes_expose_receipts_without_constructing_storage():
    assert SqliteMemoryLedger.storage_capabilities() == sqlite_v7_storage_capabilities()
    assert PostgresMemoryLedger.storage_capabilities() == postgres_v7_storage_capabilities()


def test_receipts_are_pinned_to_the_actual_built_in_storage_schema_targets():
    assert SqliteMemoryLedger.MIGRATION_VERSION == LEDGER_TARGET_STORAGE_SCHEMA_VERSION
    assert PostgresMemoryLedger.MIGRATION_VERSION == LEDGER_TARGET_STORAGE_SCHEMA_VERSION


def test_postgres_fresh_v7_catalog_contract_covers_scope_payload_purge_receipts():
    """The fresh PostgreSQL DDL and strict catalog target include v7 receipts."""

    table = "memory_scope_payload_purge_receipts"
    assert any(
        table in statement
        for statement in postgres_module._postgres_schema_statements()
    )
    assert postgres_module._PG_EXPECTED_CONSTRAINTS[table] == {
        ("p", "primary key (scope_id, scope_json, operation_key)"),
        ("c", "check (records_forgotten >= 0)"),
        ("c", "check (payload_rows_deleted >= 0)"),
        ("c", "check (source_refs_deleted >= 0)"),
        ("c", "check (relations_deleted >= 0)"),
        ("c", "check (schema_version = 1)"),
        ("c", "check (payload_rows_deleted = records_forgotten)"),
    }
    assert {
        "records_forgotten",
        "payload_rows_deleted",
        "source_refs_deleted",
        "relations_deleted",
        "schema_version",
    } <= postgres_module._PG_INTEGER_COLUMNS


def test_runner_rejects_a_descriptor_that_does_not_match_its_backend():
    with pytest.raises(ValueError, match="do not match"):
        run_ledger_storage_conformance_v1(
            SqliteMemoryLedger,
            capabilities=postgres_v7_storage_capabilities(),
        )


def test_sqlite_runner_executes_the_complete_named_v1_profile(tmp_path):
    path = tmp_path / "ledger-storage-conformance-v1.db"

    def reopenable_sqlite() -> SqliteMemoryLedger:
        return SqliteMemoryLedger(str(path))

    capabilities = SqliteMemoryLedger.storage_capabilities()
    report = run_ledger_storage_conformance_v1(
        reopenable_sqlite,
        capabilities=capabilities,
    )

    assert report == {
        "report_schema_version": 1,
        "contract_id": LEDGER_STORAGE_CONTRACT_ID,
        "contract_version": LEDGER_STORAGE_CONTRACT_VERSION,
        "semantic_profile": STRICT_HOST_LEDGER_SEMANTIC_PROFILE,
        "backend": capabilities.explain(),
        "check_ids": list(LEDGER_STORAGE_CONFORMANCE_V1_CHECK_IDS),
        "passed_check_count": len(LEDGER_STORAGE_CONFORMANCE_V1_CHECK_IDS),
        "status": "passed",
    }
