"""Host-controlled memory isolation for shared stores.

``MemoryScope`` has two jobs: expose a backend-independent metadata contract
and keep identical logical document ids from colliding across tenants, users,
or threads.  An empty scope deliberately preserves the pre-0.4 storage layout.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

SCOPE_TENANT_KEY = "scope_tenant"
SCOPE_USER_KEY = "scope_user"
SCOPE_THREAD_KEY = "scope_thread"
SCOPE_KIND_KEY = "scope_kind"
LOGICAL_DOC_ID_KEY = "logical_doc_id"

_SCOPE_FIELDS = (
    ("tenant", SCOPE_TENANT_KEY),
    ("user", SCOPE_USER_KEY),
    ("thread", SCOPE_THREAD_KEY),
    ("kind", SCOPE_KIND_KEY),
)


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """A host-owned namespace for memory reads and writes.

    Empty fields are omitted from filters. Every non-empty field participates
    in physical document identity as well as metadata filtering, so deletion
    in one tenant/user/thread/kind cannot affect another one.
    """

    tenant: str = ""
    user: str = ""
    thread: str = ""
    kind: str = ""

    def __post_init__(self) -> None:
        for name, _ in _SCOPE_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"MemoryScope.{name} must be a string")

    @property
    def is_empty(self) -> bool:
        """Whether the scope has no metadata or identity fields."""
        return not any(getattr(self, name) for name, _ in _SCOPE_FIELDS)

    @property
    def has_identity(self) -> bool:
        """Whether the scope changes physical document identity."""
        return not self.is_empty

    def to_metadata(self) -> dict[str, str]:
        """Return the canonical non-empty scope metadata fields."""
        return {
            key: getattr(self, name)
            for name, key in _SCOPE_FIELDS
            if getattr(self, name)
        }

    def to_where(self) -> dict[str, str]:
        """Return a StoreProtocol-compatible equality filter."""
        return self.to_metadata()

    def merge_metadata(
        self, metadata: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Merge scope into metadata, rejecting attempts to widen it."""
        merged = dict(metadata or {})
        for key, value in self.to_metadata().items():
            existing = merged.get(key)
            if existing is not None and existing != value:
                raise ValueError(
                    f"metadata field {key!r} conflicts with the host MemoryScope"
                )
            merged[key] = value
        return merged

    def merge_where(self, where: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Merge scope into a query filter, rejecting conflicting filters."""
        merged = dict(where or {})
        for key, value in self.to_where().items():
            existing = merged.get(key)
            if existing is not None and existing != value:
                raise ValueError(
                    f"where field {key!r} conflicts with the host MemoryScope"
                )
            merged[key] = value
        return merged

    def storage_doc_id(self, doc_id: str | int) -> str:
        """Map a logical id to a deterministic, collision-safe storage id."""
        logical = str(doc_id)
        if not self.has_identity:
            return logical
        return f"ppscope_{self.correlation_id()}__{logical}"

    def correlation_id(self) -> str:
        """Return a stable opaque id suitable for traces and storage keys."""
        if self.is_empty:
            return ""
        canonical = json.dumps(
            [self.tenant, self.user, self.thread, self.kind],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.blake2b(
            canonical,
            digest_size=12,
            person=b"protoprompt",
        ).hexdigest()
        return digest

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any]) -> "MemoryScope":
        """Rebuild a scope from canonical metadata fields."""
        values: dict[str, str] = {}
        for name, key in _SCOPE_FIELDS:
            value = metadata.get(key, "")
            if not isinstance(value, str):
                raise TypeError(f"metadata field {key!r} must be a string")
            values[name] = value
        return cls(**values)


def scoped_metadata(
    scope: MemoryScope | None,
    metadata: Mapping[str, Any] | None = None,
    *,
    logical_doc_id: str | int | None = None,
) -> dict[str, Any]:
    """Apply an optional scope and logical-id marker to metadata."""
    merged = scope.merge_metadata(metadata) if scope is not None else dict(metadata or {})
    if logical_doc_id is not None and scope is not None and scope.has_identity:
        logical = str(logical_doc_id)
        existing = merged.get(LOGICAL_DOC_ID_KEY)
        if existing is not None and existing != logical:
            raise ValueError(
                f"metadata field {LOGICAL_DOC_ID_KEY!r} conflicts with doc_id"
            )
        merged[LOGICAL_DOC_ID_KEY] = logical
    return merged


def scoped_doc_id(doc_id: str | int, scope: MemoryScope | None) -> str:
    """Map a logical id through ``scope`` while preserving legacy behavior."""
    return scope.storage_doc_id(doc_id) if scope is not None else str(doc_id)
