"""Public, sealed capability descriptor for built-in Ledger storage.

The durable Ledger deliberately has no public storage-plugin registry. Its
private command backend remains a trusted implementation detail because it
contains lifecycle, admission, erasure, and checkpoint operations that must
not become a casual third-party extension surface.

This module instead exposes a small, versioned *description* of the two
built-in backends. It makes the common semantic profile and their irreducible
operational differences reviewable before the v1 API freeze.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from protoprompt.ledger.types import LEDGER_SCHEMA_VERSION, validate_identifier


LEDGER_STORAGE_CAPABILITIES_SCHEMA_VERSION = 1
LEDGER_STORAGE_CONTRACT_ID = "protoprompt.ledger.storage"
LEDGER_STORAGE_CONTRACT_VERSION = 1
STRICT_HOST_LEDGER_SEMANTIC_PROFILE = "strict_host_ledger_v1"
LEDGER_TARGET_STORAGE_SCHEMA_VERSION = 7


class LedgerBackendId(StrEnum):
    """The built-in durable Ledger backend identities in contract v1."""

    SQLITE_V7 = "sqlite_v7"
    POSTGRES_V7 = "postgres_v7"


class LedgerSetupMode(StrEnum):
    """The only supported setup/cutover model for one backend."""

    IN_PLACE_MIGRATION = "in_place_migration"
    FRESH_V7_ONLY = "fresh_v7_only"


class LedgerBackupMode(StrEnum):
    """The operator-visible backup responsibility for one backend."""

    FILE_COPY = "file_copy"
    OPERATOR_MANAGED = "operator_managed"


class LedgerStorageConformanceError(ValueError):
    """Raised when a storage capability receipt violates contract v1."""


def _require_exact_version(
    value: object,
    *,
    expected: int,
    field: str,
) -> None:
    """Reject bool/float lookalikes for a versioned public receipt."""

    if type(value) is not int or value != expected:
        raise LedgerStorageConformanceError(
            f"{field} does not match contract v1"
        )


@dataclass(frozen=True, slots=True)
class LedgerStorageCapabilities:
    """Content-free v1 capability receipt for a built-in Ledger backend.

    ``semantic_profile`` states the shared host-facing contract, while setup
    and backup modes deliberately preserve real operational differences.
    It does not advertise a generic extension hook or promise that a passing
    descriptor proves restore/PITR, filesystem security, secret custody, or
    physical deletion from database backups.
    """

    descriptor_schema_version: int = LEDGER_STORAGE_CAPABILITIES_SCHEMA_VERSION
    contract_id: str = LEDGER_STORAGE_CONTRACT_ID
    contract_version: int = LEDGER_STORAGE_CONTRACT_VERSION
    backend_id: LedgerBackendId | str = LedgerBackendId.SQLITE_V7
    semantic_profile: str = STRICT_HOST_LEDGER_SEMANTIC_PROFILE
    record_schema_version: int = LEDGER_SCHEMA_VERSION
    target_storage_schema_version: int = LEDGER_TARGET_STORAGE_SCHEMA_VERSION
    setup_mode: LedgerSetupMode | str = LedgerSetupMode.IN_PLACE_MIGRATION
    backup_mode: LedgerBackupMode | str = LedgerBackupMode.FILE_COPY

    def __post_init__(self) -> None:
        _require_exact_version(
            self.descriptor_schema_version,
            expected=LEDGER_STORAGE_CAPABILITIES_SCHEMA_VERSION,
            field="unsupported ledger storage capabilities schema version",
        )
        if self.contract_id != LEDGER_STORAGE_CONTRACT_ID:
            raise LedgerStorageConformanceError("unsupported ledger storage contract id")
        _require_exact_version(
            self.contract_version,
            expected=LEDGER_STORAGE_CONTRACT_VERSION,
            field="unsupported ledger storage contract version",
        )
        _require_exact_version(
            self.record_schema_version,
            expected=LEDGER_SCHEMA_VERSION,
            field="record_schema_version",
        )
        _require_exact_version(
            self.target_storage_schema_version,
            expected=LEDGER_TARGET_STORAGE_SCHEMA_VERSION,
            field="target_storage_schema_version",
        )
        try:
            backend_id = LedgerBackendId(self.backend_id)
        except (TypeError, ValueError) as exc:
            raise LedgerStorageConformanceError("unsupported Ledger backend_id") from exc
        object.__setattr__(self, "backend_id", backend_id)
        try:
            semantic_profile = validate_identifier(
                self.semantic_profile,
                field="semantic_profile",
            )
        except (TypeError, ValueError) as exc:
            raise LedgerStorageConformanceError("invalid Ledger semantic_profile") from exc
        object.__setattr__(self, "semantic_profile", semantic_profile)
        if self.semantic_profile != STRICT_HOST_LEDGER_SEMANTIC_PROFILE:
            raise LedgerStorageConformanceError(
                "unsupported Ledger semantic profile for storage contract v1"
            )
        try:
            setup_mode = LedgerSetupMode(self.setup_mode)
        except (TypeError, ValueError) as exc:
            raise LedgerStorageConformanceError("unsupported Ledger setup_mode") from exc
        try:
            backup_mode = LedgerBackupMode(self.backup_mode)
        except (TypeError, ValueError) as exc:
            raise LedgerStorageConformanceError("unsupported Ledger backup_mode") from exc
        object.__setattr__(self, "setup_mode", setup_mode)
        object.__setattr__(self, "backup_mode", backup_mode)
        self._validate_builtin_mode_pair()

    def _validate_builtin_mode_pair(self) -> None:
        """Reject descriptors that erase built-in operational differences."""

        if self.backend_id is LedgerBackendId.SQLITE_V7:
            if self.setup_mode is not LedgerSetupMode.IN_PLACE_MIGRATION:
                raise LedgerStorageConformanceError(
                    "sqlite_v7 requires in_place_migration setup mode"
                )
            if self.backup_mode is not LedgerBackupMode.FILE_COPY:
                raise LedgerStorageConformanceError(
                    "sqlite_v7 requires file_copy backup mode"
                )
        elif self.backend_id is LedgerBackendId.POSTGRES_V7:
            if self.setup_mode is not LedgerSetupMode.FRESH_V7_ONLY:
                raise LedgerStorageConformanceError(
                    "postgres_v7 requires fresh_v7_only setup mode"
                )
            if self.backup_mode is not LedgerBackupMode.OPERATOR_MANAGED:
                raise LedgerStorageConformanceError(
                    "postgres_v7 requires operator_managed backup mode"
                )

    def explain(self) -> dict[str, object]:
        """Return a fresh JSON-safe receipt with no storage path or payload."""

        return {
            "descriptor_schema_version": self.descriptor_schema_version,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "backend_id": self.backend_id.value,
            "semantic_profile": self.semantic_profile,
            "record_schema_version": self.record_schema_version,
            "target_storage_schema_version": self.target_storage_schema_version,
            "setup_mode": self.setup_mode.value,
            "backup_mode": self.backup_mode.value,
        }


def sqlite_v7_storage_capabilities() -> LedgerStorageCapabilities:
    """Return the fixed SQLite capability descriptor without opening storage."""

    return LedgerStorageCapabilities()


def postgres_v7_storage_capabilities() -> LedgerStorageCapabilities:
    """Return the fixed PostgreSQL descriptor without importing psycopg."""

    return LedgerStorageCapabilities(
        backend_id=LedgerBackendId.POSTGRES_V7,
        setup_mode=LedgerSetupMode.FRESH_V7_ONLY,
        backup_mode=LedgerBackupMode.OPERATOR_MANAGED,
    )


__all__ = [
    "LEDGER_STORAGE_CAPABILITIES_SCHEMA_VERSION",
    "LEDGER_STORAGE_CONTRACT_ID",
    "LEDGER_STORAGE_CONTRACT_VERSION",
    "LEDGER_TARGET_STORAGE_SCHEMA_VERSION",
    "STRICT_HOST_LEDGER_SEMANTIC_PROFILE",
    "LedgerBackendId",
    "LedgerBackupMode",
    "LedgerSetupMode",
    "LedgerStorageCapabilities",
    "LedgerStorageConformanceError",
    "postgres_v7_storage_capabilities",
    "sqlite_v7_storage_capabilities",
]
