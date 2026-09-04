"""Host-owned durable state for the Ollama chat task-resume demonstration.

This module deliberately owns only the small mapping that the Ledger
``TaskResumePlanner`` leaves to its host: a conversation is bound to one
opaque task reference, frozen descriptor, and opaque checkpoint identifier.
It is not a browser-facing API, does not admit Ledger records, and never
persists the host checkpoint secret.

The mapping lives in the reference application's ``chat.db`` as additive
tables.  Every row is authenticated with a domain-separated key derived from
the same host-held checkpoint secret that authenticates Ledger checkpoints.
Consequently a copied, edited, or cross-conversation binding fails closed
before an application can reconstruct a task-resume planner.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator, Mapping
import uuid

from protoprompt.ledger.types import canonical_json, scope_dict, validate_identifier
from protoprompt.scope import MemoryScope


TASK_RESUME_STATE_SCHEMA_VERSION = 1
"""Persisted schema understood by this host-side mapping repository."""

TASK_RESUME_BINDING_CONTRACT_ID = "ledger-task-episode-resume-v1"
"""The narrow Ledger task-resume contract a binding is allowed to restore."""

_INSTALLATION_TABLE = "ollama_chat_task_resume_installation"
_BINDING_TABLE = "ollama_chat_task_resume_bindings"
_INSTALLATION_RECORD_TYPE = "ollama_chat_task_resume_installation"
_BINDING_RECORD_TYPE = "ollama_chat_task_resume_binding"
_STATE_HMAC_KDF_LABEL = b"protoprompt/ollama-chat/task-resume-state-hmac/v1"
_MIN_CHECKPOINT_SECRET_BYTES = 32
_MAX_CHECKPOINT_SECRET_BYTES = 4_096
_MAX_TASK_DESCRIPTOR_CHARS = 16_000


class TaskResumeStateError(RuntimeError):
    """Base error for host-owned task-resume state."""


class TaskResumeStateIntegrityError(TaskResumeStateError):
    """Raised when persisted task-resume state is malformed or unauthenticated."""


class TaskResumeStateConflictError(TaskResumeStateError):
    """Raised when a host attempts to replace or race a durable binding."""


class TaskResumeStateLifecycleError(TaskResumeStateError):
    """Raised when a binding is not in the lifecycle state an operation requires."""


class TaskResumeBindingState(StrEnum):
    """The only durable states for a task-resume mapping."""

    ACTIVE = "active"
    CLOSING = "closing"


@dataclass(frozen=True, slots=True)
class TaskResumeBinding:
    """One verified, host-only conversation-to-task resume mapping.

    ``parent_scope_json`` and ``parent_scope_correlation_id`` are retained as
    explicit fields because they are part of the durable authentication
    contract, rather than re-derived opportunistically from a task reference.
    The repository returns this value only after validating its signature and
    exact local-conversation scope.
    """

    conversation_id: str
    parent_scope: MemoryScope
    parent_scope_json: str
    parent_scope_correlation_id: str
    task_ref: str
    task_descriptor: str
    checkpoint_id: str
    state: TaskResumeBindingState
    generation: int
    contract_id: str
    schema_version: int


def _normalize_checkpoint_secret(value: object) -> bytes:
    """Use the same bounds as durable Ledger checkpoint authentication."""

    if not isinstance(value, bytes):
        raise TypeError("checkpoint_secret must be bytes")
    if not _MIN_CHECKPOINT_SECRET_BYTES <= len(value) <= _MAX_CHECKPOINT_SECRET_BYTES:
        raise ValueError(
            "checkpoint_secret must contain from "
            f"{_MIN_CHECKPOINT_SECRET_BYTES} to {_MAX_CHECKPOINT_SECRET_BYTES} bytes"
        )
    return bytes(value)


def _derive_state_hmac_key(checkpoint_secret: bytes) -> bytes:
    """Derive a key for app state without reusing the Ledger checkpoint key."""

    return hmac.new(
        checkpoint_secret,
        _STATE_HMAC_KDF_LABEL,
        hashlib.sha256,
    ).digest()


def _normalize_descriptor(value: object) -> str:
    """Mirror the bounded host descriptor contract of ``TaskResumePlanner``."""

    if not isinstance(value, str):
        raise TypeError("task_descriptor must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("task_descriptor must not be empty")
    if len(normalized) > _MAX_TASK_DESCRIPTOR_CHARS:
        raise ValueError(
            f"task_descriptor must be at most {_MAX_TASK_DESCRIPTOR_CHARS} characters"
        )
    return normalized


def _positive_generation(value: object, *, field: str = "generation") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _strict_json_object(value: str, *, field: str) -> dict[str, object]:
    """Decode one canonical object while rejecting ambiguous JSON syntax."""

    if not isinstance(value, str):
        raise TaskResumeStateIntegrityError(f"{field} must be a JSON string")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, item in pairs:
            if key in decoded:
                raise TaskResumeStateIntegrityError(
                    f"{field} must not contain duplicate field {key!r}"
                )
            decoded[key] = item
        return decoded

    def reject_constant(item: str) -> object:
        raise TaskResumeStateIntegrityError(
            f"{field} must not contain non-finite JSON constant {item!r}"
        )

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise TaskResumeStateIntegrityError(f"{field} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise TaskResumeStateIntegrityError(f"{field} must be a JSON object")
    return decoded


class TaskResumeStateRepository:
    """SQLite-backed, signed local mapping for host-side task resume.

    The browser must never receive this object.  The caller supplies a stable
    host checkpoint secret on every process start; it is used only to derive
    an in-memory state-HMAC key and is never written to SQLite.
    """

    def __init__(self, path: Path | str, *, checkpoint_secret: bytes) -> None:
        self._path = Path(path)
        self._state_hmac_key = _derive_state_hmac_key(
            _normalize_checkpoint_secret(checkpoint_secret)
        )
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

        try:
            with self._lock:
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._conn.executescript(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_INSTALLATION_TABLE} (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        installation_id TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        integrity_tag TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS {_BINDING_TABLE} (
                        conversation_id TEXT PRIMARY KEY,
                        parent_scope_json TEXT NOT NULL,
                        parent_scope_correlation_id TEXT NOT NULL,
                        task_ref TEXT NOT NULL,
                        task_descriptor TEXT NOT NULL,
                        checkpoint_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('active', 'closing')),
                        generation INTEGER NOT NULL CHECK(generation > 0),
                        contract_id TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        integrity_tag TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_ollama_chat_task_resume_state
                        ON {_BINDING_TABLE}(state, conversation_id);
                    """
                )
                self._assert_required_columns(
                    _INSTALLATION_TABLE,
                    {
                        "singleton",
                        "installation_id",
                        "schema_version",
                        "integrity_tag",
                    },
                )
                self._assert_required_columns(
                    _BINDING_TABLE,
                    {
                        "conversation_id",
                        "parent_scope_json",
                        "parent_scope_correlation_id",
                        "task_ref",
                        "task_descriptor",
                        "checkpoint_id",
                        "state",
                        "generation",
                        "contract_id",
                        "schema_version",
                        "integrity_tag",
                    },
                )
                self._installation_id = self._load_or_create_installation()
                self._conn.commit()
        except BaseException:
            self._conn.rollback()
            self._conn.close()
            raise

    @property
    def installation_id(self) -> str:
        """Return the stable opaque host installation namespace identifier."""

        return self._installation_id

    def close(self) -> None:
        """Close the local SQLite connection when the enclosing runtime stops."""

        with self._lock:
            self._conn.close()

    def parent_scope_for(self, conversation_id: str) -> MemoryScope:
        """Return the only parent scope valid for a local conversation binding."""

        normalized_conversation = validate_identifier(
            conversation_id,
            field="conversation_id",
        )
        return MemoryScope(
            tenant=self._installation_id,
            user="local-owner",
            thread=f"ollama-chat:{normalized_conversation}",
            kind="ollama_chat",
        )

    def create(
        self,
        *,
        conversation_id: str,
        task_ref: str,
        task_descriptor: str,
        checkpoint_id: str,
    ) -> TaskResumeBinding:
        """Create one immutable active binding; replacement is never implicit."""

        normalized_conversation = validate_identifier(
            conversation_id,
            field="conversation_id",
        )
        binding = self._new_binding(
            conversation_id=normalized_conversation,
            task_ref=validate_identifier(task_ref, field="task_ref"),
            task_descriptor=_normalize_descriptor(task_descriptor),
            checkpoint_id=validate_identifier(checkpoint_id, field="checkpoint_id"),
            state=TaskResumeBindingState.ACTIVE,
            generation=1,
        )
        integrity_tag = self._binding_integrity_tag(binding)

        with self._lock, self._write_transaction():
            row = self._conn.execute(
                f"SELECT * FROM {_BINDING_TABLE} WHERE conversation_id = ?",
                (normalized_conversation,),
            ).fetchone()
            if row is not None:
                # Validate before reporting a conflict, so an altered row can
                # never be mistaken for an ordinary already-created binding.
                self._binding_from_row(row)
                raise TaskResumeStateConflictError(
                    "task-resume binding already exists for this conversation"
                )
            self._conn.execute(
                f"""
                INSERT INTO {_BINDING_TABLE}(
                    conversation_id, parent_scope_json, parent_scope_correlation_id,
                    task_ref, task_descriptor, checkpoint_id, state, generation,
                    contract_id, schema_version, integrity_tag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.conversation_id,
                    binding.parent_scope_json,
                    binding.parent_scope_correlation_id,
                    binding.task_ref,
                    binding.task_descriptor,
                    binding.checkpoint_id,
                    binding.state.value,
                    binding.generation,
                    binding.contract_id,
                    binding.schema_version,
                    integrity_tag,
                ),
            )
        return binding

    def load_active(self, conversation_id: str) -> TaskResumeBinding | None:
        """Load only a verified active binding; a closing mapping never resumes."""

        normalized_conversation = validate_identifier(
            conversation_id,
            field="conversation_id",
        )
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM {_BINDING_TABLE} WHERE conversation_id = ?",
                (normalized_conversation,),
            ).fetchone()
        if row is None:
            return None
        binding = self._binding_from_row(row)
        if binding.state is not TaskResumeBindingState.ACTIVE:
            return None
        return binding

    def begin_close(
        self,
        conversation_id: str,
        *,
        expected_generation: int | None = None,
    ) -> TaskResumeBinding | None:
        """Atomically remove a mapping from the resume path before deletion.

        This operation is idempotent for an already-closing binding so a host
        can safely retry a conversation-deletion workflow after a restart.
        Supplying ``expected_generation`` gives callers an optimistic locking
        boundary when they have already loaded a binding.
        """

        normalized_conversation = validate_identifier(
            conversation_id,
            field="conversation_id",
        )
        normalized_expected = (
            _positive_generation(expected_generation, field="expected_generation")
            if expected_generation is not None
            else None
        )
        with self._lock, self._write_transaction():
            row = self._conn.execute(
                f"SELECT * FROM {_BINDING_TABLE} WHERE conversation_id = ?",
                (normalized_conversation,),
            ).fetchone()
            if row is None:
                return None
            binding = self._binding_from_row(row)
            self._assert_expected_generation(binding, normalized_expected)
            if binding.state is TaskResumeBindingState.CLOSING:
                return binding
            closing = self._new_binding(
                conversation_id=binding.conversation_id,
                task_ref=binding.task_ref,
                task_descriptor=binding.task_descriptor,
                checkpoint_id=binding.checkpoint_id,
                state=TaskResumeBindingState.CLOSING,
                generation=binding.generation + 1,
            )
            cursor = self._conn.execute(
                f"""
                UPDATE {_BINDING_TABLE}
                SET parent_scope_json = ?, parent_scope_correlation_id = ?,
                    task_ref = ?, task_descriptor = ?, checkpoint_id = ?, state = ?,
                    generation = ?, contract_id = ?, schema_version = ?, integrity_tag = ?
                WHERE conversation_id = ? AND generation = ?
                """,
                (
                    closing.parent_scope_json,
                    closing.parent_scope_correlation_id,
                    closing.task_ref,
                    closing.task_descriptor,
                    closing.checkpoint_id,
                    closing.state.value,
                    closing.generation,
                    closing.contract_id,
                    closing.schema_version,
                    self._binding_integrity_tag(closing),
                    closing.conversation_id,
                    binding.generation,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - lock serializes here
                raise TaskResumeStateConflictError(
                    "task-resume binding changed while closing"
                )
        return closing

    def finish_close(
        self,
        conversation_id: str,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        """Delete a verified closing mapping after its host cleanup completes.

        ``False`` means there was no mapping, which keeps repeated teardown
        safe.  An active mapping is never deleted by this method: callers
        must first pass through :meth:`begin_close`.
        """

        normalized_conversation = validate_identifier(
            conversation_id,
            field="conversation_id",
        )
        normalized_expected = (
            _positive_generation(expected_generation, field="expected_generation")
            if expected_generation is not None
            else None
        )
        with self._lock, self._write_transaction():
            row = self._conn.execute(
                f"SELECT * FROM {_BINDING_TABLE} WHERE conversation_id = ?",
                (normalized_conversation,),
            ).fetchone()
            if row is None:
                return False
            binding = self._binding_from_row(row)
            self._assert_expected_generation(binding, normalized_expected)
            if binding.state is not TaskResumeBindingState.CLOSING:
                raise TaskResumeStateLifecycleError(
                    "task-resume binding must be closing before it can be removed"
                )
            cursor = self._conn.execute(
                f"DELETE FROM {_BINDING_TABLE} "
                "WHERE conversation_id = ? AND generation = ? AND state = ?",
                (
                    binding.conversation_id,
                    binding.generation,
                    TaskResumeBindingState.CLOSING.value,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - lock serializes here
                raise TaskResumeStateConflictError(
                    "task-resume binding changed while closing"
                )
        return True

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _assert_required_columns(self, table: str, required: set[str]) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {str(row[1]) for row in rows}
        missing = required - columns
        if missing:
            names = ", ".join(sorted(missing))
            raise TaskResumeStateIntegrityError(
                f"unsupported task-resume state schema; missing {names}"
            )

    def _load_or_create_installation(self) -> str:
        rows = self._conn.execute(
            f"SELECT singleton, installation_id, schema_version, integrity_tag "
            f"FROM {_INSTALLATION_TABLE}"
        ).fetchall()
        if not rows:
            installation_id = uuid.uuid4().hex
            integrity_tag = self._integrity_tag({
                "record_type": _INSTALLATION_RECORD_TYPE,
                "schema_version": TASK_RESUME_STATE_SCHEMA_VERSION,
                "installation_id": installation_id,
            })
            self._conn.execute(
                f"""
                INSERT INTO {_INSTALLATION_TABLE}(
                    singleton, installation_id, schema_version, integrity_tag
                ) VALUES (1, ?, ?, ?)
                """,
                (
                    installation_id,
                    TASK_RESUME_STATE_SCHEMA_VERSION,
                    integrity_tag,
                ),
            )
            return installation_id
        if len(rows) != 1:
            raise TaskResumeStateIntegrityError(
                "task-resume installation state must contain exactly one record"
            )
        row = rows[0]
        if row["singleton"] != 1:
            raise TaskResumeStateIntegrityError(
                "task-resume installation state has an invalid singleton key"
            )
        installation_id = self._row_identifier(row, "installation_id")
        schema_version = self._row_positive_int(row, "schema_version")
        if schema_version != TASK_RESUME_STATE_SCHEMA_VERSION:
            raise TaskResumeStateIntegrityError(
                "unsupported task-resume installation schema version"
            )
        integrity_tag = self._row_string(row, "integrity_tag")
        expected_tag = self._integrity_tag({
            "record_type": _INSTALLATION_RECORD_TYPE,
            "schema_version": schema_version,
            "installation_id": installation_id,
        })
        if not hmac.compare_digest(integrity_tag, expected_tag):
            raise TaskResumeStateIntegrityError(
                "task-resume installation integrity check failed"
            )
        return installation_id

    def _new_binding(
        self,
        *,
        conversation_id: str,
        task_ref: str,
        task_descriptor: str,
        checkpoint_id: str,
        state: TaskResumeBindingState,
        generation: int,
    ) -> TaskResumeBinding:
        parent_scope = self.parent_scope_for(conversation_id)
        parent_scope_json = canonical_json(scope_dict(parent_scope))
        return TaskResumeBinding(
            conversation_id=conversation_id,
            parent_scope=parent_scope,
            parent_scope_json=parent_scope_json,
            parent_scope_correlation_id=parent_scope.correlation_id(),
            task_ref=task_ref,
            task_descriptor=task_descriptor,
            checkpoint_id=checkpoint_id,
            state=state,
            generation=_positive_generation(generation),
            contract_id=TASK_RESUME_BINDING_CONTRACT_ID,
            schema_version=TASK_RESUME_STATE_SCHEMA_VERSION,
        )

    def _binding_from_row(self, row: sqlite3.Row) -> TaskResumeBinding:
        """Decode, authenticate, and pin one persisted row to its conversation."""

        try:
            conversation_id = self._row_identifier(row, "conversation_id")
            parent_scope_json = self._row_string(row, "parent_scope_json")
            parent_scope_correlation_id = self._row_string(
                row,
                "parent_scope_correlation_id",
            )
            task_ref = self._row_identifier(row, "task_ref")
            task_descriptor = _normalize_descriptor(
                self._row_string(row, "task_descriptor")
            )
            checkpoint_id = self._row_identifier(row, "checkpoint_id")
            state = TaskResumeBindingState(self._row_string(row, "state"))
            generation = self._row_positive_int(row, "generation")
            contract_id = self._row_string(row, "contract_id")
            schema_version = self._row_positive_int(row, "schema_version")
            integrity_tag = self._row_string(row, "integrity_tag")
        except (TypeError, ValueError) as exc:
            raise TaskResumeStateIntegrityError(
                "task-resume binding contains invalid field values"
            ) from exc

        if task_descriptor != row["task_descriptor"]:
            raise TaskResumeStateIntegrityError(
                "task-resume binding descriptor is not in canonical form"
            )
        if schema_version != TASK_RESUME_STATE_SCHEMA_VERSION:
            raise TaskResumeStateIntegrityError(
                "unsupported task-resume binding schema version"
            )
        if contract_id != TASK_RESUME_BINDING_CONTRACT_ID:
            raise TaskResumeStateIntegrityError(
                "unsupported task-resume binding contract"
            )

        scope_data = _strict_json_object(parent_scope_json, field="parent_scope_json")
        if set(scope_data) != {"tenant", "user", "thread", "kind"}:
            raise TaskResumeStateIntegrityError(
                "task-resume binding parent scope has an invalid shape"
            )
        try:
            parent_scope = MemoryScope(
                tenant=scope_data["tenant"],
                user=scope_data["user"],
                thread=scope_data["thread"],
                kind=scope_data["kind"],
            )
            canonical_parent_scope_json = canonical_json(scope_dict(parent_scope))
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskResumeStateIntegrityError(
                "task-resume binding parent scope is invalid"
            ) from exc
        if parent_scope_json != canonical_parent_scope_json:
            raise TaskResumeStateIntegrityError(
                "task-resume binding parent scope is not canonical"
            )
        if parent_scope_correlation_id != parent_scope.correlation_id():
            raise TaskResumeStateIntegrityError(
                "task-resume binding parent scope correlation does not match"
            )
        if parent_scope != self.parent_scope_for(conversation_id):
            raise TaskResumeStateIntegrityError(
                "task-resume binding parent scope does not match its conversation"
            )

        binding = TaskResumeBinding(
            conversation_id=conversation_id,
            parent_scope=parent_scope,
            parent_scope_json=parent_scope_json,
            parent_scope_correlation_id=parent_scope_correlation_id,
            task_ref=task_ref,
            task_descriptor=task_descriptor,
            checkpoint_id=checkpoint_id,
            state=state,
            generation=generation,
            contract_id=contract_id,
            schema_version=schema_version,
        )
        expected_tag = self._binding_integrity_tag(binding)
        if not hmac.compare_digest(integrity_tag, expected_tag):
            raise TaskResumeStateIntegrityError(
                "task-resume binding integrity check failed"
            )
        return binding

    def _binding_integrity_tag(self, binding: TaskResumeBinding) -> str:
        """Return the state-HMAC tag for one exact persisted binding shape."""

        return self._integrity_tag({
            "record_type": _BINDING_RECORD_TYPE,
            "conversation_id": binding.conversation_id,
            "parent_scope_json": binding.parent_scope_json,
            "parent_scope_correlation_id": binding.parent_scope_correlation_id,
            "task_ref": binding.task_ref,
            "task_descriptor": binding.task_descriptor,
            "checkpoint_id": binding.checkpoint_id,
            "state": binding.state.value,
            "generation": binding.generation,
            "contract_id": binding.contract_id,
            "schema_version": binding.schema_version,
        })

    def _integrity_tag(self, payload: Mapping[str, object]) -> str:
        return hmac.new(
            self._state_hmac_key,
            canonical_json(dict(payload)).encode("utf-8", errors="strict"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _row_string(row: sqlite3.Row, field: str) -> str:
        value = row[field]
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        return value

    @classmethod
    def _row_identifier(cls, row: sqlite3.Row, field: str) -> str:
        return validate_identifier(cls._row_string(row, field), field=field)

    @staticmethod
    def _row_positive_int(row: sqlite3.Row, field: str) -> int:
        return _positive_generation(row[field], field=field)

    @staticmethod
    def _assert_expected_generation(
        binding: TaskResumeBinding,
        expected_generation: int | None,
    ) -> None:
        if expected_generation is not None and binding.generation != expected_generation:
            raise TaskResumeStateConflictError(
                "task-resume binding generation does not match"
            )


__all__ = [
    "TASK_RESUME_BINDING_CONTRACT_ID",
    "TASK_RESUME_STATE_SCHEMA_VERSION",
    "TaskResumeBinding",
    "TaskResumeBindingState",
    "TaskResumeStateConflictError",
    "TaskResumeStateError",
    "TaskResumeStateIntegrityError",
    "TaskResumeStateLifecycleError",
    "TaskResumeStateRepository",
]
