"""Experimental, host-confirmed durable memory ledger.

The ledger is intentionally opt-in and does not alter the legacy vector store,
profiles, sessions, or :class:`protoprompt.MemoryService`. Import it directly
from ``protoprompt.ledger``. The experimental ``protoprompt.ledger.recall``
package remains separate by default; its explicit host-owned composer can form
one admitted Ledger data lane in a bounded request without auto-wiring legacy
memory or reference applications.
"""

from protoprompt.ledger.sqlite import SqliteMemoryLedger
from protoprompt.ledger.postgres import PostgresMemoryLedger
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
from protoprompt.ledger.task_resume import (
    TASK_RESUME_SCHEMA_VERSION,
    TaskEpisode,
    TaskOutcome,
    TaskProcedure,
    TaskResumePayload,
    TaskResumePayloadError,
    decode_task_resume_payload,
    encode_task_resume_payload,
)
from protoprompt.ledger.task_resume_planner import (
    TASK_RESUME_ADAPTER_SCHEMA_VERSION,
    TASK_RESUME_SCOPE_KIND,
    TaskResumeBindingError,
    TaskResumeConfigurationError,
    TaskResumeError,
    TaskResumePayloadBindingError,
    TaskResumePlanner,
    TaskResumeSelectionError,
    task_resume_scope,
)

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
    "PostgresMemoryLedger",
    "SqliteMemoryLedger",
    "StaleMemoryReviewError",
    "TASK_RESUME_ADAPTER_SCHEMA_VERSION",
    "TASK_RESUME_SCHEMA_VERSION",
    "TASK_RESUME_SCOPE_KIND",
    "TaskEpisode",
    "TaskOutcome",
    "TaskProcedure",
    "TaskResumeBindingError",
    "TaskResumeConfigurationError",
    "TaskResumeError",
    "TaskResumePayload",
    "TaskResumePayloadBindingError",
    "TaskResumePayloadError",
    "TaskResumePlanner",
    "TaskResumeSelectionError",
    "decode_task_resume_payload",
    "encode_task_resume_payload",
    "task_resume_scope",
]
