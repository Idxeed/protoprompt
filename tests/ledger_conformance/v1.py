"""Named v1 semantic-storage conformance runner for built-in Ledgers.

This remains test-only. The public production contract is the content-free
``LedgerStorageCapabilities`` descriptor; the runner maps its one shared
semantic profile to the existing host-facing regression checks without
publishing the Ledger's private command surface as a plugin API.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from protoprompt.ledger.storage_conformance import (
    STRICT_HOST_LEDGER_SEMANTIC_PROFILE,
    LedgerStorageCapabilities,
)

from ledger_conformance.core import (
    assert_admission_boundary_and_strict_recall,
    assert_candidate_confirmation_and_content_free_events,
    assert_checkpoint_reopen_resume_and_selected_record_invalidation,
    assert_exact_scope_isolation_and_scoped_forget,
    assert_idempotent_retries_and_conflicting_event_reuse,
    assert_lifecycle_forget_source_and_hard_erase,
    assert_restart_and_setup_persistence,
)


LEDGER_STORAGE_CONFORMANCE_REPORT_SCHEMA_VERSION = 1
LEDGER_STORAGE_CONFORMANCE_V1_CHECK_IDS = (
    "candidate_confirmation_content_free_events",
    "audited_admission_strict_recall",
    "exact_scope_isolation_scoped_forget",
    "idempotent_retries_event_reuse",
    "lifecycle_source_revoke_hard_erase",
    "restart_setup_persistence",
    "sealed_checkpoint_restart_invalidation",
)

LedgerFactory = Callable[[], Any]


def _factory_pinned_to_capabilities(
    factory: LedgerFactory,
    capabilities: LedgerStorageCapabilities,
) -> LedgerFactory:
    """Return a factory that cannot relabel one built-in as the other.

    The runner receives an externally supplied receipt so it can report the
    selected built-in without exposing a storage plugin interface.  Validate
    every constructed backend against that receipt: otherwise a SQLite
    factory paired with a PostgreSQL descriptor could create a misleading
    passing report.
    """

    def checked_factory() -> Any:
        ledger = factory()
        try:
            actual = ledger.storage_capabilities()
        except Exception:
            ledger.close()
            raise
        if not isinstance(actual, LedgerStorageCapabilities) or actual != capabilities:
            ledger.close()
            raise ValueError(
                "factory storage capabilities do not match the requested conformance receipt"
            )
        return ledger

    return checked_factory


def run_ledger_storage_conformance_v1(
    factory: LedgerFactory,
    *,
    capabilities: LedgerStorageCapabilities,
) -> dict[str, object]:
    """Run the fixed shared semantic profile and return a content-free receipt.

    Backend-specific migration/catalog/tamper/recovery tests remain separate:
    a passing report establishes only the named public host semantics shared by
    the two built-ins.
    """

    if not callable(factory):
        raise TypeError("factory must be callable")
    if not isinstance(capabilities, LedgerStorageCapabilities):
        raise TypeError("capabilities must be a LedgerStorageCapabilities")
    if capabilities.semantic_profile != STRICT_HOST_LEDGER_SEMANTIC_PROFILE:
        raise ValueError("storage capabilities do not implement the v1 strict host profile")

    checked_factory = _factory_pinned_to_capabilities(factory, capabilities)

    checks: tuple[tuple[str, Callable[[LedgerFactory], None]], ...] = (
        (
            "candidate_confirmation_content_free_events",
            assert_candidate_confirmation_and_content_free_events,
        ),
        ("audited_admission_strict_recall", assert_admission_boundary_and_strict_recall),
        ("exact_scope_isolation_scoped_forget", assert_exact_scope_isolation_and_scoped_forget),
        ("idempotent_retries_event_reuse", assert_idempotent_retries_and_conflicting_event_reuse),
        ("lifecycle_source_revoke_hard_erase", assert_lifecycle_forget_source_and_hard_erase),
        ("restart_setup_persistence", assert_restart_and_setup_persistence),
        (
            "sealed_checkpoint_restart_invalidation",
            assert_checkpoint_reopen_resume_and_selected_record_invalidation,
        ),
    )
    for _, check in checks:
        check(checked_factory)
    return {
        "report_schema_version": LEDGER_STORAGE_CONFORMANCE_REPORT_SCHEMA_VERSION,
        "contract_id": capabilities.contract_id,
        "contract_version": capabilities.contract_version,
        "semantic_profile": capabilities.semantic_profile,
        "backend": capabilities.explain(),
        "check_ids": [check_id for check_id, _ in checks],
        "passed_check_count": len(checks),
        "status": "passed",
    }


__all__ = [
    "LEDGER_STORAGE_CONFORMANCE_REPORT_SCHEMA_VERSION",
    "LEDGER_STORAGE_CONFORMANCE_V1_CHECK_IDS",
    "run_ledger_storage_conformance_v1",
]
