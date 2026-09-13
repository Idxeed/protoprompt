"""Narrow, versioned public API candidate for ProtoPrompt 1.x.

The historical package and subpackage exports remain available for backward
compatibility.  This module is the deliberately smaller import seam whose
documented contracts are being frozen for 1.0.  In pre-1.0 releases every
symbol here is a *v1 candidate*, not yet a final SemVer stability promise.

Use :func:`v1_api_manifest` to inspect the machine-readable boundary without
opening storage or importing optional provider SDKs.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any, Final

from protoprompt.context_plan import (
    ContextBlockDecision,
    ContextDataLaneReceipt,
    ContextDecision,
    ContextPlan,
    ContextRequestReceipt,
)
from protoprompt.ledger.policy import (
    MEMORY_POLICY_SCHEMA_VERSION,
    MemoryPolicy,
    MemoryPolicyError,
)
from protoprompt.ledger.postgres import PostgresMemoryLedger
from protoprompt.ledger.sqlite import SqliteMemoryLedger
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
from protoprompt.ledger.types import (
    LEDGER_SCHEMA_VERSION,
    SCOPE_PAYLOAD_PURGE_SCHEMA_VERSION,
    ErasureReceipt,
    LedgerConflictError,
    LedgerError,
    LedgerNotReadyError,
    LedgerStateError,
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
    ScopePayloadPurgeReceipt,
    ScopePayloadReadback,
)
from protoprompt.ledger.writer import MemoryWriter
from protoprompt.scope import MemoryScope


API_CONTRACT_SCHEMA_VERSION: Final = 1
API_CONTRACT_ID: Final = "protoprompt.public-api"
API_TARGET_MAJOR: Final = 1
V1_API_STATUS: Final = "v1_candidate"
V1_API_MANIFEST_SHA256: Final = (
    "a0f5c9ef1cf51301a9011393475f0627f0011f02408c7cbf4271765f32d814dc"
)


def v1_api_manifest() -> dict[str, Any]:
    """Return a detached JSON-safe copy of the packaged v1 API manifest.

    Reading the contract has no storage, network, credential, or provider-SDK
    side effects.  A new object is returned on every call so callers cannot
    mutate process-global contract state.
    """

    resource = files("protoprompt").joinpath("api_contract_v1.json")
    manifest = json.loads(resource.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != API_CONTRACT_SCHEMA_VERSION:
        raise RuntimeError("unsupported packaged ProtoPrompt API contract schema")
    if manifest.get("contract_id") != API_CONTRACT_ID:
        raise RuntimeError("packaged ProtoPrompt API contract id mismatch")
    if manifest.get("target_major") != API_TARGET_MAJOR:
        raise RuntimeError("packaged ProtoPrompt API target major mismatch")
    if manifest.get("status") != V1_API_STATUS:
        raise RuntimeError("packaged ProtoPrompt API status mismatch")
    return manifest


__all__ = [
    "API_CONTRACT_ID",
    "API_CONTRACT_SCHEMA_VERSION",
    "API_TARGET_MAJOR",
    "ContextBlockDecision",
    "ContextDataLaneReceipt",
    "ContextDecision",
    "ContextPlan",
    "ContextRequestReceipt",
    "ErasureReceipt",
    "LEDGER_SCHEMA_VERSION",
    "LEDGER_STORAGE_CAPABILITIES_SCHEMA_VERSION",
    "LEDGER_STORAGE_CONTRACT_ID",
    "LEDGER_STORAGE_CONTRACT_VERSION",
    "LEDGER_TARGET_STORAGE_SCHEMA_VERSION",
    "LedgerBackendId",
    "LedgerBackupMode",
    "LedgerConflictError",
    "LedgerError",
    "LedgerNotReadyError",
    "LedgerSetupMode",
    "LedgerStateError",
    "LedgerStorageCapabilities",
    "LedgerStorageConformanceError",
    "MEMORY_POLICY_SCHEMA_VERSION",
    "MemoryAdmissionAudit",
    "MemoryEvent",
    "MemoryEventType",
    "MemoryKind",
    "MemoryOrigin",
    "MemoryPolicy",
    "MemoryPolicyError",
    "MemoryRecord",
    "MemoryRelation",
    "MemoryRelationType",
    "MemoryScope",
    "MemoryState",
    "MemoryTrust",
    "MemoryWriter",
    "PostgresMemoryLedger",
    "SCOPE_PAYLOAD_PURGE_SCHEMA_VERSION",
    "STRICT_HOST_LEDGER_SEMANTIC_PROFILE",
    "ScopePayloadPurgeReceipt",
    "ScopePayloadReadback",
    "SqliteMemoryLedger",
    "V1_API_MANIFEST_SHA256",
    "V1_API_STATUS",
    "postgres_v7_storage_capabilities",
    "sqlite_v7_storage_capabilities",
    "v1_api_manifest",
]
