"""Experimental, host-confirmed durable memory ledger.

The ledger is intentionally opt-in and does not alter the legacy vector store,
profiles, sessions, or :class:`protoprompt.MemoryService`. Import it directly
from ``protoprompt.ledger``. The experimental ``protoprompt.ledger.recall``
package remains separate by default; its explicit host-owned composer can form
one admitted Ledger data lane in a bounded request without auto-wiring legacy
memory or reference applications.
"""

from protoprompt.ledger.sqlite import SqliteMemoryLedger
from protoprompt.ledger.admission import (
    MemoryAdmissionDecision,
    MemoryAdmissionError,
    MemoryAdmissionPolicy,
    MemoryAdmissionPolicyError,
    MemoryReview,
    MemoryReviewGate,
    StaleMemoryReviewError,
)
from protoprompt.ledger.types import (
    ErasureReceipt,
    LedgerConflictError,
    LedgerError,
    LedgerNotReadyError,
    LedgerStateError,
    MemoryAdmissionAction,
    MemoryAdmissionAudit,
    MemoryEvent,
    MemoryEventType,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryRelation,
    MemoryRelationType,
    MemoryState,
    MemoryTrust,
)
from protoprompt.ledger.writer import MemoryWriter

__all__ = [
    "ErasureReceipt",
    "LedgerConflictError",
    "LedgerError",
    "LedgerNotReadyError",
    "LedgerStateError",
    "MemoryAdmissionAction",
    "MemoryAdmissionAudit",
    "MemoryAdmissionDecision",
    "MemoryAdmissionError",
    "MemoryAdmissionPolicy",
    "MemoryAdmissionPolicyError",
    "MemoryEvent",
    "MemoryEventType",
    "MemoryKind",
    "MemoryOrigin",
    "MemoryRecord",
    "MemoryRelation",
    "MemoryRelationType",
    "MemoryState",
    "MemoryTrust",
    "MemoryReview",
    "MemoryReviewGate",
    "MemoryWriter",
    "SqliteMemoryLedger",
    "StaleMemoryReviewError",
]
