"""Typed, content-safe observability events.

Events carry counts, decisions, timing, trace correlation, and a hashed scope
identifier. Prompt text, documents, messages, profiles, and secrets are
redacted by default even when a custom attribute mapping accidentally includes
them. Event sink failures are logged and never interrupt the main operation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import logging
import time
from typing import Any, Callable, ClassVar, Mapping, Protocol, TypeAlias
import uuid

from protoprompt.scope import MemoryScope

logger = logging.getLogger(__name__)


def new_trace_id() -> str:
    """Return an opaque per-operation correlation id."""
    return uuid.uuid4().hex


def elapsed_ms(started_at: float) -> float:
    """Convert a ``perf_counter`` start value to non-negative milliseconds."""
    return max(0.0, (time.perf_counter() - started_at) * 1000.0)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProtoPromptEvent:
    """Base fields shared by all typed events."""

    event_name: ClassVar[str] = "event"
    action: str
    trace_id: str = field(default_factory=new_trace_id)
    scope_id: str = ""
    duration_ms: float | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["event_name"] = self.event_name
        return data


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextEvent(ProtoPromptEvent):
    event_name: ClassVar[str] = "context"


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrieveEvent(ProtoPromptEvent):
    event_name: ClassVar[str] = "retrieve"


@dataclass(frozen=True, slots=True, kw_only=True)
class CompressEvent(ProtoPromptEvent):
    event_name: ClassVar[str] = "compress"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileEvent(ProtoPromptEvent):
    event_name: ClassVar[str] = "profile"


@dataclass(frozen=True, slots=True, kw_only=True)
class RecallEvent(ProtoPromptEvent):
    event_name: ClassVar[str] = "recall"


@dataclass(frozen=True, slots=True, kw_only=True)
class EvictEvent(ProtoPromptEvent):
    event_name: ClassVar[str] = "evict"


@dataclass(frozen=True, slots=True, kw_only=True)
class CacheEvent(ProtoPromptEvent):
    event_name: ClassVar[str] = "cache"


Event: TypeAlias = (
    ContextEvent
    | RetrieveEvent
    | CompressEvent
    | ProfileEvent
    | RecallEvent
    | EvictEvent
    | CacheEvent
)


class EventSink(Protocol):
    def __call__(self, event: Event) -> None:
        ...


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    """Deny-by-default policy for common content-bearing attribute keys."""

    denied_keys: frozenset[str] = frozenset({
        "api_key",
        "content",
        "credential",
        "document",
        "documents",
        "message",
        "messages",
        "profile",
        "prompt",
        "secret",
        "text",
        "token",
        "value",
    })
    replacement: str = "[REDACTED]"

    def denies(self, key: str) -> bool:
        """Return whether an attribute name is likely to carry raw content."""
        normalized = key.lower().strip()
        if normalized in self.denied_keys:
            return True
        content_suffixes = (
            "_api_key",
            "_content",
            "_credential",
            "_document",
            "_message",
            "_profile",
            "_prompt",
            "_secret",
            "_text",
        )
        return normalized.startswith("raw_") or normalized.endswith(content_suffixes)

    def sanitize(self, value: Any, *, key: str = "") -> Any:
        if self.denies(key):
            return self.replacement
        if isinstance(value, Mapping):
            return {
                str(child_key): self.sanitize(child, key=str(child_key))
                for child_key, child in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.sanitize(item) for item in value]
        return value


DEFAULT_REDACTION_POLICY = RedactionPolicy()


class EventDispatcher:
    """Apply redaction and fan events out to one or more sinks."""

    def __init__(
        self,
        *sinks: EventSink,
        redaction: RedactionPolicy = DEFAULT_REDACTION_POLICY,
    ) -> None:
        self._sinks = tuple(sink for sink in sinks if sink is not None)
        self._redaction = redaction

    def emit(self, event: Event) -> None:
        safe = replace(
            event,
            attributes=self._redaction.sanitize(dict(event.attributes)),
        )
        for sink in self._sinks:
            try:
                sink(safe)
            except Exception:
                logger.exception("protoprompt event sink failed")


def dispatch(event_sink: EventSink | EventDispatcher | None, event: Event) -> None:
    """Emit through a dispatcher or safely wrap a plain callable sink."""
    if event_sink is None:
        return
    if isinstance(event_sink, EventDispatcher):
        event_sink.emit(event)
    else:
        EventDispatcher(event_sink).emit(event)


def scope_id(scope: MemoryScope | None) -> str:
    """Return a stable opaque correlation id without exporting raw scope."""
    return scope.correlation_id() if scope is not None else ""
