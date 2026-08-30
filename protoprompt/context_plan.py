"""Immutable planning contracts for explainable bounded context.

The plan retains the rendered request so it can be projected back to the
existing ``ContextOutput``/message APIs.  Its :meth:`ContextPlan.explain`
method is intentionally content-free and safe to serialize into developer
telemetry or a UI.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from protoprompt.tokens.message_payload import json_safe_value


ContextDecision = Literal["included", "excluded", "truncated", "reserved"]


@dataclass(frozen=True, slots=True)
class ContextBlockDecision:
    """One deterministic choice made while building a request.

    ``token_cost`` is the allocator's marginal cost.  For arbitrary provider
    counters it is not required to sum to the final receipt; the receipt is
    the authoritative full-message accounting figure.
    """

    block_id: str
    origin: str
    decision: ContextDecision
    reason: str
    token_cost: int = 0
    candidate_tokens: int | None = None
    source_id: str | None = None
    score: float | None = None

    def __post_init__(self) -> None:
        if not self.block_id:
            raise ValueError("block_id must not be empty")
        if not self.origin:
            raise ValueError("origin must not be empty")
        if not self.reason:
            raise ValueError("reason must not be empty")
        if self.token_cost < 0:
            raise ValueError("token_cost must be non-negative")
        if self.candidate_tokens is not None and self.candidate_tokens < 0:
            raise ValueError("candidate_tokens must be non-negative")
        if self.score is not None and not math.isfinite(self.score):
            raise ValueError("score must be finite when provided")

    @property
    def status(self) -> ContextDecision:
        """Alias kept for callers that read a decision as a status."""
        return self.decision

    @property
    def reason_code(self) -> str:
        """Alias for integrations that label stable reasons as codes."""
        return self.reason

    def explain(self) -> dict[str, object]:
        """Return JSON-safe metadata without any candidate content."""
        result: dict[str, object] = {
            "block_id": self.block_id,
            "origin": self.origin,
            "decision": self.decision,
            "reason": self.reason,
            "token_cost": self.token_cost,
        }
        if self.candidate_tokens is not None:
            result["candidate_tokens"] = self.candidate_tokens
        if self.source_id is not None:
            result["source_id"] = self.source_id
        if self.score is not None:
            result["score"] = self.score
        return result


@dataclass(frozen=True, slots=True)
class ContextRequestReceipt:
    """Exact accounting for one fully rendered provider request."""

    trace_id: str
    max_tokens: int
    input_tokens: int
    output_reserve_tokens: int
    remaining_tokens: int
    context_tokens: int
    history_tokens: int
    final_input_tokens: int

    def __post_init__(self) -> None:
        if not self.trace_id:
            raise ValueError("trace_id must not be empty")
        for field_name in (
            "max_tokens",
            "input_tokens",
            "output_reserve_tokens",
            "remaining_tokens",
            "context_tokens",
            "history_tokens",
            "final_input_tokens",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")
        if self.input_tokens + self.output_reserve_tokens > self.max_tokens:
            raise ValueError("request exceeds its configured token budget")
        if (
            self.input_tokens
            + self.output_reserve_tokens
            + self.remaining_tokens
            != self.max_tokens
        ):
            raise ValueError("receipt token totals must reconcile to max_tokens")

    def explain(self) -> dict[str, object]:
        return {
            "trace_id": self.trace_id,
            "max_tokens": self.max_tokens,
            "input_tokens": self.input_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
            "remaining_tokens": self.remaining_tokens,
            "context_tokens": self.context_tokens,
            "history_tokens": self.history_tokens,
            "final_input_tokens": self.final_input_tokens,
        }


@dataclass(frozen=True, slots=True)
class ContextDataLaneReceipt:
    """Content-free accounting for one host-owned request data lane.

    A lane is a mandatory, separately rendered provider-message group such as
    experimental Ledger reference data. Its standalone token measurement is
    explanatory only: :class:`ContextRequestReceipt` remains authoritative
    for the whole provider request, including any non-additive counter
    behaviour across message boundaries.
    """

    lane_id: str
    origin: str
    media_type: str
    message_count: int
    input_tokens: int
    data_tokens: int
    data_bytes: int
    record_count: int

    def __post_init__(self) -> None:
        for field_name in ("lane_id", "origin", "media_type"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        for field_name in (
            "message_count",
            "input_tokens",
            "data_tokens",
            "data_bytes",
            "record_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.message_count < 1:
            raise ValueError("message_count must be at least one")

    def explain(self) -> dict[str, object]:
        """Return JSON-safe lane metadata without payload, scope, or IDs."""

        return {
            "lane_id": self.lane_id,
            "origin": self.origin,
            "media_type": self.media_type,
            "message_count": self.message_count,
            "input_tokens": self.input_tokens,
            "data_tokens": self.data_tokens,
            "data_bytes": self.data_bytes,
            "record_count": self.record_count,
        }


def _freeze_messages(messages: list[dict[str, Any]]) -> tuple[str, ...]:
    """Capture JSON-compatible messages independently of caller mutation."""
    frozen: list[str] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise TypeError(f"message {index} must be a mapping")
        normalized = json_safe_value(message, path=f"message[{index}]")
        frozen.append(json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ))
    return tuple(frozen)


def snapshot_portable_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deep-copy portable messages before an async planning boundary.

    The canonical JSON round trip makes a request independent of a caller that
    mutates a nested tool payload while retrieval is awaiting network or disk.
    It also enforces the documented JSON-compatible message contract early.
    """
    return [json.loads(message) for message in _freeze_messages(messages)]


@dataclass(frozen=True, slots=True)
class ContextPlan:
    """Immutable context selection and optional rendered request projection."""

    schema_version: int
    trace_id: str
    policy_id: str
    system_prompt: str = field(repr=False)
    rag_blocks: tuple[str, ...] = field(default=(), repr=False)
    session_blocks: tuple[str, ...] = field(default=(), repr=False)
    profile_used: bool = False
    decisions: tuple[ContextBlockDecision, ...] = ()
    receipt: ContextRequestReceipt | None = None
    _rendered_messages: tuple[str, ...] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    # Appended after the legacy private rendered-message slot so existing
    # positional construction remains compatible. A plan is still not a
    # portable wire format; this is content-free explanatory metadata only.
    data_lanes: tuple[ContextDataLaneReceipt, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported ContextPlan schema version")
        if not self.trace_id:
            raise ValueError("trace_id must not be empty")
        if not self.policy_id:
            raise ValueError("policy_id must not be empty")
        if not isinstance(self.system_prompt, str):
            raise TypeError("system_prompt must be a string")
        rag_blocks = tuple(self.rag_blocks)
        session_blocks = tuple(self.session_blocks)
        decisions = tuple(self.decisions)
        data_lanes = tuple(self.data_lanes)
        if not all(isinstance(block, str) for block in rag_blocks):
            raise TypeError("rag_blocks must contain strings")
        if not all(isinstance(block, str) for block in session_blocks):
            raise TypeError("session_blocks must contain strings")
        if not all(isinstance(decision, ContextBlockDecision) for decision in decisions):
            raise TypeError("decisions must contain ContextBlockDecision values")
        if not all(isinstance(lane, ContextDataLaneReceipt) for lane in data_lanes):
            raise TypeError("data_lanes must contain ContextDataLaneReceipt values")
        object.__setattr__(self, "rag_blocks", rag_blocks)
        object.__setattr__(self, "session_blocks", session_blocks)
        object.__setattr__(self, "decisions", decisions)
        object.__setattr__(self, "data_lanes", data_lanes)
        if self._rendered_messages is not None:
            rendered_messages = tuple(self._rendered_messages)
            if not all(isinstance(message, str) for message in rendered_messages):
                raise TypeError("rendered messages must be canonical JSON strings")
            object.__setattr__(self, "_rendered_messages", rendered_messages)
        if self.receipt is not None and self.receipt.trace_id != self.trace_id:
            raise ValueError("receipt trace_id must match plan trace_id")

    def render_system_prompt(self) -> str:
        """Render the planned system context without re-running retrieval."""
        return self.system_prompt

    def render_messages(self) -> list[dict[str, Any]]:
        """Return an independent copy of the planned provider request.

        Context-only plans intentionally do not carry caller history or final
        input, so their caller must use :meth:`render_system_prompt` instead.
        """
        if self._rendered_messages is None:
            raise ValueError("context-only plans do not contain provider messages")
        return [json.loads(message) for message in self._rendered_messages]

    @classmethod
    def with_messages(
        cls,
        plan: "ContextPlan",
        messages: list[dict[str, Any]],
        receipt: ContextRequestReceipt,
        decisions: tuple[ContextBlockDecision, ...],
        data_lanes: tuple[ContextDataLaneReceipt, ...] = (),
    ) -> "ContextPlan":
        """Return a request plan whose payload is isolated from caller data."""
        if receipt.trace_id != plan.trace_id:
            raise ValueError("receipt trace_id must match plan trace_id")
        return cls(
            schema_version=plan.schema_version,
            trace_id=plan.trace_id,
            policy_id=plan.policy_id,
            system_prompt=plan.system_prompt,
            rag_blocks=plan.rag_blocks,
            session_blocks=plan.session_blocks,
            profile_used=plan.profile_used,
            decisions=decisions,
            receipt=receipt,
            data_lanes=data_lanes,
            _rendered_messages=_freeze_messages(messages),
        )

    def explain(self) -> dict[str, object]:
        """Return a fresh, content-free JSON-safe explanation."""
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "policy_id": self.policy_id,
            "profile_used": self.profile_used,
            "rag_block_count": len(self.rag_blocks),
            "session_block_count": len(self.session_blocks),
            "decisions": [decision.explain() for decision in self.decisions],
            "data_lanes": [lane.explain() for lane in self.data_lanes],
        }
        if self.receipt is not None:
            result["receipt"] = self.receipt.explain()
        return result
