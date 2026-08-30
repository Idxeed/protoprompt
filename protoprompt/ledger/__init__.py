"""Experimental, host-confirmed durable memory ledger.

The ledger is intentionally opt-in and does not alter the legacy vector store,
profiles, sessions, or :class:`protoprompt.MemoryService`.  Import it directly
from ``protoprompt.ledger`` while the public adapter and migration work is
completed in the v0.8 line.
"""

from protoprompt.ledger.sqlite import SqliteMemoryLedger
from protoprompt.ledger.types import (
    ErasureReceipt,
    LedgerConflictError,
    LedgerError,
    LedgerNotReadyError,
    LedgerStateError,
    MemoryEvent,
    MemoryEventType,
    MemoryKind,
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
    "MemoryEvent",
    "MemoryEventType",
    "MemoryKind",
    "MemoryRecord",
    "MemoryRelation",
    "MemoryRelationType",
    "MemoryState",
    "MemoryTrust",
    "MemoryWriter",
    "SqliteMemoryLedger",
]
