"""OpenAI Agents SDK session and budgeted-input integration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from protoprompt.context import ContextInput
from protoprompt.injector_budgeted import TokenBudgetedContextBuilder
from protoprompt.tokens.protocol import TokenCounter


def _sqlite_session_class():
    try:
        from agents import SQLiteSession
    except ImportError as exc:
        raise ImportError(
            "The OpenAI Agents adapter requires 'openai-agents'. "
            "Install with: pip install 'protoprompt[agents]'"
        ) from exc
    return SQLiteSession


class ProtoPromptSession:
    """An Agents ``Session`` backed by the upstream SQLite implementation.

    Conversation history remains lossless and ordered. Protoprompt recall is
    added only to the model view by :func:`create_session_input_callback`, so
    ``pop_item`` corrections and ``clear_session`` preserve upstream semantics.
    Clearing a session intentionally does not delete long-term scoped memory.
    """

    def __init__(
        self,
        session_id: str,
        db_path: str | Path = ":memory:",
        *,
        inner_session: Any | None = None,
        session_settings: Any | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        self.session_id = session_id
        if inner_session is None:
            SQLiteSession = _sqlite_session_class()
            inner_session = SQLiteSession(
                session_id,
                db_path,
                session_settings=session_settings,
            )
        self._inner = inner_session
        self.session_settings = (
            session_settings
            if session_settings is not None
            else getattr(inner_session, "session_settings", None)
        )

    @property
    def inner_session(self) -> Any:
        return self._inner

    async def get_items(self, limit: int | None = None) -> list[dict[str, Any]]:
        return list(await self._inner.get_items(limit=limit))

    async def add_items(self, items: list[dict[str, Any]]) -> None:
        await self._inner.add_items(list(items))

    async def pop_item(self) -> dict[str, Any] | None:
        return await self._inner.pop_item()

    async def clear_session(self) -> None:
        await self._inner.clear_session()

    def input_callback(
        self,
        builder: TokenBudgetedContextBuilder,
        **kwargs: Any,
    ) -> Callable[[list[dict], list[dict]], Any]:
        """Create a budgeted callback already bound to this session id."""
        return create_session_input_callback(
            builder,
            session_id=self.session_id,
            **kwargs,
        )


def create_session_input_callback(
    builder: TokenBudgetedContextBuilder,
    *,
    session_id: str,
    system_prompt: str = "",
    counter: TokenCounter | None = None,
    context_factory: Callable[[str], ContextInput] | None = None,
    output_reserve: int | None = None,
) -> Callable[[list[dict], list[dict]], Any]:
    """Build a fully budgeted model-facing request for an Agents session.

    The callback changes only the model-facing list returned to the Agents SDK.
    The SDK continues to persist the original ``new_input`` items exactly once.
    ``new_input`` is treated as a mandatory final turn (including tool calls
    and rich content); system context and history are allocated only from what
    remains after it and ``output_reserve``.  Passing ``counter`` overrides the
    builder counter for this one complete request, rather than applying a
    second, inconsistent history-only estimate.
    """

    async def session_input_callback(
        history: list[dict],
        new_input: list[dict],
    ) -> list[dict]:
        query = _latest_text(new_input)
        inp = (
            context_factory(query)
            if context_factory is not None
            else ContextInput(
                query=query,
                chat_id=session_id,
                system_prompt=system_prompt,
                include_profile=False,
            )
        )
        return await builder.build_messages(
            inp,
            history=history,
            final_messages=list(new_input),
            output_reserve=output_reserve,
            counter=counter,
        )

    return session_input_callback


def _latest_text(items: list[dict]) -> str:
    for item in reversed(items):
        text = _content_text(item.get("content"))
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
            elif isinstance(block, dict):
                for key in ("text", "input_text", "output_text"):
                    value = block.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
        return "\n".join(parts)
    return ""
