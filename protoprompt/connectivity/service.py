"""A scope-pinned memory facade shared by MCP and framework adapters."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any
import uuid

from protoprompt.agent.working import WorkingMemory
from protoprompt.events import EventDispatcher, EventSink, RetrieveEvent, dispatch, new_trace_id, scope_id
from protoprompt.llm import EmbeddingClientProtocol
from protoprompt.profile.manager import ProfileManager
from protoprompt.profile.store import profile_to_dict
from protoprompt.profile.types import Signal
from protoprompt.scope import (
    LOGICAL_DOC_ID_KEY,
    MemoryScope,
    scoped_doc_id,
    scoped_metadata,
)
from protoprompt.store.protocol import StoreProtocol, await_if_needed

_MEMORY_KIND = "memory"
_MEMORY_PREFIX = "memory"


class MemoryService:
    """High-level memory operations pinned to one host-provided scope.

    The scope never appears in method arguments, so a model-facing adapter
    cannot switch tenant, user, or thread. The service can be shared by MCP,
    agent SDKs, graph nodes, and channel demos without duplicating policy.
    """

    def __init__(
        self,
        store: StoreProtocol,
        embeddings: EmbeddingClientProtocol,
        scope: MemoryScope,
        *,
        profile_manager: ProfileManager | None = None,
        working_memory: WorkingMemory | None = None,
        context_builder: Any | None = None,
        embedding_model: str = "nomic-embed-text",
        event_sink: EventSink | EventDispatcher | None = None,
    ) -> None:
        if scope.is_empty:
            raise ValueError("MemoryService requires a non-empty host MemoryScope")
        if (
            profile_manager is not None
            and getattr(profile_manager, "scope", None) != scope
        ):
            raise ValueError(
                "profile_manager scope must exactly match the MemoryService scope"
            )
        self._store = store
        self._embeddings = embeddings
        self._scope = scope
        self._profile_manager = profile_manager
        self._working_memory = working_memory
        self._context_builder = context_builder
        self._embedding_model = embedding_model
        self._event_sink = event_sink
        self._remembered_ids: set[str] = set()
        self._last_search: dict[str, Any] = {
            "query_performed": False,
            "result_count": 0,
            "results": [],
        }

    @property
    def scope(self) -> MemoryScope:
        return self._scope

    async def remember(
        self,
        text: str,
        *,
        memory_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Embed and persist one confirmed memory in the pinned scope."""
        normalized = text.strip()
        if not normalized:
            raise ValueError("memory text must not be empty")
        identity = (memory_id or uuid.uuid4().hex).strip()
        if not identity:
            raise ValueError("memory_id must not be empty")
        logical_doc_id = self._logical_doc_id(identity)
        storage_id = scoped_doc_id(logical_doc_id, self._scope)
        vector = (
            await self._embeddings.embed([normalized], model=self._embedding_model)
        )[0]
        meta = scoped_metadata(
            self._scope,
            metadata,
            logical_doc_id=logical_doc_id,
        )
        meta.update({"kind": _MEMORY_KIND, "memory_id": identity})
        await await_if_needed(self._store.delete(storage_id))
        await await_if_needed(self._store.add(storage_id, [normalized], [vector], meta))
        self._remembered_ids.add(identity)
        return {"memory_id": identity, "stored": True}

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        score_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Search confirmed memories and return text with provenance."""
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        vector = (
            await self._embeddings.embed([query], model=self._embedding_model)
        )[0]
        where = self._scope.merge_where({"kind": _MEMORY_KIND})
        hits = await await_if_needed(self._store.query(
            vector,
            top_k=top_k,
            where=where,
            score_threshold=score_threshold,
        ))
        output: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        for hit in hits:
            metadata = dict(hit.get("metadata") or {})
            memory_id = str(metadata.get("memory_id", ""))
            score = hit.get("score")
            if score is None and hit.get("distance") is not None:
                score = 1.0 - float(hit["distance"])
            item = {
                "memory_id": memory_id,
                "text": str(hit.get("document", "")),
                "score": float(score) if score is not None else 0.0,
                "chunk_index": int(metadata.get("chunk_index", 0)),
            }
            output.append(item)
            receipts.append({key: value for key, value in item.items() if key != "text"})
        self._last_search = {
            "query_performed": True,
            "result_count": len(output),
            "results": receipts,
            "threshold_applied": score_threshold is not None,
        }
        dispatch(self._event_sink, RetrieveEvent(
            action="completed",
            trace_id=new_trace_id(),
            scope_id=scope_id(self._scope),
            attributes={
                "channel": "memory_service",
                "top_k": top_k,
                "hit_count": len(output),
                "threshold_applied": score_threshold is not None,
            },
        ))
        return output

    async def forget(self, memory_id: str) -> dict[str, Any]:
        """Delete a logical memory id only inside the pinned scope."""
        identity = memory_id.strip()
        if not identity:
            raise ValueError("memory_id must not be empty")
        await await_if_needed(
            self._store.delete(scoped_doc_id(self._logical_doc_id(identity), self._scope))
        )
        self._remembered_ids.discard(identity)
        return {"memory_id": identity, "forgotten": True}

    async def profile_update(self, text: str) -> dict[str, Any]:
        """Fold one model-visible signal into the current pinned user profile."""
        if self._profile_manager is None:
            raise RuntimeError("profile operations are not configured by the host")
        if not self._scope.user:
            raise RuntimeError("profile operations require MemoryScope.user")
        profile = await self._profile_manager.update(
            self._scope.user,
            [Signal(
                user_id=self._scope.user,
                kind="message",
                role="user",
                text=text,
            )],
        )
        return profile_to_dict(profile)

    async def current_profile(self) -> dict[str, Any] | None:
        if self._profile_manager is None or not self._scope.user:
            return None
        profile = await self._profile_manager.get(self._scope.user)
        return profile_to_dict(profile) if profile is not None else None

    def explain(self) -> dict[str, Any]:
        """Return content-free provenance for the most recent search."""
        return dict(self._last_search)

    def manifest(self) -> dict[str, Any]:
        """Return a read-only manifest for confirmed and cold memories."""
        cold: list[dict[str, Any]] = []
        if self._working_memory is not None:
            cold = [
                {
                    "item_id": entry.item_id,
                    "kind": entry.kind,
                    "summary": entry.summary,
                    "tokens": entry.tokens,
                    "evicted_at": entry.evicted_at,
                    "important": entry.important,
                }
                for entry in self._working_memory.manifest.entries
            ]
        return {
            "confirmed_memory_ids": sorted(self._remembered_ids),
            "cold": cold,
        }

    def budget_report(self) -> dict[str, Any] | None:
        """Return the latest builder report without prompt content."""
        if self._context_builder is None:
            return None
        report = getattr(self._context_builder, "last_report", None)
        if report is None:
            return None
        if is_dataclass(report):
            return asdict(report)
        if isinstance(report, dict):
            return dict(report)
        return {"available": True, "type": type(report).__name__}

    @staticmethod
    def _logical_doc_id(memory_id: str) -> str:
        return f"{_MEMORY_PREFIX}_{memory_id}"
