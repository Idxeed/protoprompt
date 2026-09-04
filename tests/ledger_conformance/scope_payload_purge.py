"""Named backend-neutral conformance runner for exact-scope payload purge.

This is intentionally separate from ``ledger_conformance.v1``.  That frozen
semantic-storage profile remains unchanged, while this runner adds focused
coverage for the newer public ``MemoryWriter.payload_readback`` and
``MemoryWriter.purge_payloads`` contract.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ledger_conformance.core import (
    assert_scope_payload_purge_durable_retry_and_scope_isolation,
)


SCOPE_PAYLOAD_PURGE_CONFORMANCE_REPORT_SCHEMA_VERSION = 1
SCOPE_PAYLOAD_PURGE_CONFORMANCE_V1_CHECK_IDS = (
    "exact_scope_durable_retry_and_command_drift",
)

LedgerFactory = Callable[[], Any]


def run_scope_payload_purge_conformance_v1(
    factory: LedgerFactory,
) -> dict[str, object]:
    """Run the public exact-scope purge contract against a durable backend.

    The factory must reopen the same logical store: persistence across that
    restart is a material part of the retry contract.  The return value is a
    content-free test receipt, not a production storage-capability descriptor.
    """

    if not callable(factory):
        raise TypeError("factory must be callable")

    assert_scope_payload_purge_durable_retry_and_scope_isolation(factory)
    return {
        "report_schema_version": SCOPE_PAYLOAD_PURGE_CONFORMANCE_REPORT_SCHEMA_VERSION,
        "contract_id": "memory_writer_scope_payload_purge",
        "contract_version": 1,
        "check_ids": list(SCOPE_PAYLOAD_PURGE_CONFORMANCE_V1_CHECK_IDS),
        "passed_check_count": len(SCOPE_PAYLOAD_PURGE_CONFORMANCE_V1_CHECK_IDS),
        "status": "passed",
    }


__all__ = [
    "SCOPE_PAYLOAD_PURGE_CONFORMANCE_REPORT_SCHEMA_VERSION",
    "SCOPE_PAYLOAD_PURGE_CONFORMANCE_V1_CHECK_IDS",
    "run_scope_payload_purge_conformance_v1",
]
