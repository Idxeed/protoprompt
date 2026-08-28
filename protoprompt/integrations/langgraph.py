"""Scope-safe LangGraph store and context-node integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict
from typing import Any

from protoprompt.context import ContextInput, ContextOutput
from protoprompt.injector import ContextBuilder
from protoprompt.scope import MemoryScope

try:
    from langgraph.store.base import (
        BaseStore,
        GetOp,
        Item,
        ListNamespacesOp,
        MatchCondition,
        PutOp,
        SearchItem,
        SearchOp,
    )
except ImportError as exc:  # pragma: no cover - exercised in an isolated import test
    raise ImportError(
        "The LangGraph adapter requires 'langgraph'. "
        "Install with: pip install 'protoprompt[langgraph]'"
    ) from exc


class ProtoPromptStoreAdapter(BaseStore):
    """Pin any LangGraph store to one host-controlled memory scope.

    LangGraph nodes continue to use the standard ``BaseStore`` API and see
    their original logical namespaces. The delegate receives an opaque scope
    prefix, including for batched and namespace-listing operations, so state
    supplied by a user or model cannot widen access to another scope.
    """

    def __init__(self, inner: BaseStore, *, scope: MemoryScope) -> None:
        if not isinstance(inner, BaseStore):
            raise TypeError("inner must implement langgraph.store.base.BaseStore")
        if scope.is_empty:
            raise ValueError("LangGraph stores require a non-empty MemoryScope")
        self.inner = inner
        self.scope = scope
        self._scope_prefix = ("__protoprompt_scope__", scope.correlation_id())
        self.supports_ttl = inner.supports_ttl
        self.ttl_config = inner.ttl_config

    def batch(self, ops: Iterable[Any]) -> list[Any]:
        original = list(ops)
        physical = [self._physical_op(op) for op in original]
        results = self.inner.batch(physical)
        return [
            self._logical_result(op, result)
            for op, result in zip(original, results, strict=True)
        ]

    async def abatch(self, ops: Iterable[Any]) -> list[Any]:
        original = list(ops)
        physical = [self._physical_op(op) for op in original]
        results = await self.inner.abatch(physical)
        return [
            self._logical_result(op, result)
            for op, result in zip(original, results, strict=True)
        ]

    def _physical_namespace(self, namespace: tuple[str, ...]) -> tuple[str, ...]:
        return self._scope_prefix + tuple(namespace)

    def _logical_namespace(self, namespace: tuple[str, ...]) -> tuple[str, ...]:
        prefix_length = len(self._scope_prefix)
        if tuple(namespace[:prefix_length]) != self._scope_prefix:
            raise RuntimeError("LangGraph delegate returned an out-of-scope namespace")
        return tuple(namespace[prefix_length:])

    def _physical_op(self, op: Any) -> Any:
        if isinstance(op, GetOp):
            return GetOp(
                self._physical_namespace(op.namespace),
                op.key,
                op.refresh_ttl,
            )
        if isinstance(op, PutOp):
            return PutOp(
                self._physical_namespace(op.namespace),
                op.key,
                op.value,
                op.index,
                op.ttl,
            )
        if isinstance(op, SearchOp):
            return SearchOp(
                self._physical_namespace(op.namespace_prefix),
                op.filter,
                op.limit,
                op.offset,
                op.query,
                op.refresh_ttl,
            )
        if isinstance(op, ListNamespacesOp):
            conditions: list[MatchCondition] = []
            has_prefix = False
            for condition in op.match_conditions or ():
                if condition.match_type == "prefix":
                    has_prefix = True
                    conditions.append(MatchCondition(
                        "prefix",
                        self._physical_namespace(tuple(condition.path)),
                    ))
                else:
                    conditions.append(condition)
            if not has_prefix:
                conditions.append(MatchCondition("prefix", self._scope_prefix))
            max_depth = (
                None
                if op.max_depth is None
                else op.max_depth + len(self._scope_prefix)
            )
            return ListNamespacesOp(
                tuple(conditions),
                max_depth,
                op.limit,
                op.offset,
            )
        raise TypeError(f"unsupported LangGraph store operation: {type(op).__name__}")

    def _logical_result(self, op: Any, result: Any) -> Any:
        if result is None:
            return None
        if isinstance(op, GetOp):
            return self._logical_item(result)
        if isinstance(op, SearchOp):
            return [self._logical_search_item(item) for item in result]
        if isinstance(op, ListNamespacesOp):
            return [self._logical_namespace(namespace) for namespace in result]
        return result

    def _logical_item(self, item: Item) -> Item:
        return Item(
            value=item.value,
            key=item.key,
            namespace=self._logical_namespace(item.namespace),
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    def _logical_search_item(self, item: SearchItem) -> SearchItem:
        return SearchItem(
            namespace=self._logical_namespace(item.namespace),
            key=item.key,
            value=item.value,
            created_at=item.created_at,
            updated_at=item.updated_at,
            score=item.score,
        )


ContextInputFactory = Callable[[Mapping[str, Any], str], ContextInput]


def create_build_context_node(
    builder: ContextBuilder,
    *,
    chat_id: str = "",
    system_prompt: str = "",
    language: str = "en",
    input_factory: ContextInputFactory | None = None,
    context_key: str = "context",
    provenance_key: str = "context_provenance",
) -> Callable[[Mapping[str, Any]], Any]:
    """Return an async LangGraph node that assembles scoped model context.

    The node reads ``state['query']`` when present, otherwise the newest text
    message. It writes a plain context string plus content-free provenance and
    deliberately does not mutate ``messages`` (which may have a reducer).
    """

    async def build_context(state: Mapping[str, Any]) -> dict[str, Any]:
        query = _query_from_state(state)
        inp = _context_input(
            state,
            query,
            chat_id=chat_id,
            system_prompt=system_prompt,
            language=language,
            input_factory=input_factory,
        )
        output = await builder.build(inp)
        return _node_result(
            output,
            context_key=context_key,
            provenance_key=provenance_key,
        )

    return build_context


def create_sync_build_context_node(
    builder: ContextBuilder,
    *,
    chat_id: str = "",
    system_prompt: str = "",
    language: str = "en",
    input_factory: ContextInputFactory | None = None,
    context_key: str = "context",
    provenance_key: str = "context_provenance",
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Return the synchronous counterpart for graphs invoked with ``invoke``.

    LangGraph executes a sync graph node in synchronous code. If called from a
    running event-loop thread, use :func:`create_build_context_node` and
    ``graph.ainvoke`` instead.
    """

    def build_context(state: Mapping[str, Any]) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "sync build_context cannot run inside an event loop; "
                "use create_build_context_node with graph.ainvoke"
            )
        query = _query_from_state(state)
        inp = _context_input(
            state,
            query,
            chat_id=chat_id,
            system_prompt=system_prompt,
            language=language,
            input_factory=input_factory,
        )
        output = asyncio.run(builder.build(inp))
        return _node_result(
            output,
            context_key=context_key,
            provenance_key=provenance_key,
        )

    return build_context


def _context_input(
    state: Mapping[str, Any],
    query: str,
    *,
    chat_id: str,
    system_prompt: str,
    language: str,
    input_factory: ContextInputFactory | None,
) -> ContextInput:
    if input_factory is not None:
        inp = input_factory(state, query)
        if not isinstance(inp, ContextInput):
            raise TypeError("input_factory must return ContextInput")
        return inp
    return ContextInput(
        query=query,
        chat_id=chat_id,
        system_prompt=system_prompt,
        language=language,
    )


def _node_result(
    output: ContextOutput,
    *,
    context_key: str,
    provenance_key: str,
) -> dict[str, Any]:
    provenance = {
        "rag": [
            {
                "doc_id": chunk.doc_id,
                "chunk_index": chunk.index,
                "score": chunk.score,
            }
            for chunk in output.rag_chunks
        ],
        "rag_block_count": len(output.rag_blocks),
        "session_block_count": len(output.session_blocks),
        "profile_used": output.profile_used,
        "budget": (
            asdict(output.budget_report)
            if output.budget_report is not None
            else None
        ),
    }
    return {
        context_key: output.system_prompt,
        provenance_key: provenance,
    }


def _query_from_state(state: Mapping[str, Any]) -> str:
    query = state.get("query")
    if isinstance(query, str) and query:
        return query
    messages = state.get("messages", ())
    if isinstance(messages, (str, bytes)):
        messages = ()
    for message in reversed(list(messages)):
        if isinstance(message, Mapping):
            content = message.get("content")
        else:
            content = getattr(message, "content", None)
        text = _content_text(content)
        if text:
            return text
    return ""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                for key in ("text", "input_text", "output_text"):
                    value = block.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
        return "\n".join(parts)
    return ""
