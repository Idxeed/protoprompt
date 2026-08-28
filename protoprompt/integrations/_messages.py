"""Shared normalization for provider-native chat APIs.

The public protocol intentionally accepts OpenAI-shaped dictionaries because
that is the smallest useful interchange format.  Native providers disagree on
where system instructions live and how text blocks are represented, so the
adapters normalize once and keep provider-specific serialization explicit.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def text_content(content: Any) -> str:
    """Return the textual portion of a portable message content value."""

    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, Iterable) and not isinstance(content, (bytes, Mapping)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)
    return str(content)


def split_system_messages(
    messages: list[dict],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Separate system/developer instructions from conversational turns.

    Empty turns are retained: dropping them can change provider-side turn
    coalescing. Unknown roles fail early instead of being silently re-labelled.
    """

    system: list[str] = []
    turns: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if role in {"system", "developer"}:
            system.append(text_content(content))
            continue
        if role not in {"user", "assistant"}:
            raise ValueError(
                f"Unsupported portable chat role {role!r}; expected "
                "system, developer, user, or assistant"
            )
        turns.append({"role": role, "content": content})
    return system, turns


def text_blocks(content: Any) -> list[dict[str, str]]:
    """Convert portable content into native text blocks."""

    if isinstance(content, list):
        blocks: list[dict[str, str]] = []
        for block in content:
            if isinstance(block, str):
                blocks.append({"text": block})
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                blocks.append({"text": block["text"]})
        if blocks:
            return blocks
    return [{"text": text_content(content)}]


def response_text(blocks: Any) -> str:
    """Join text blocks returned by Anthropic/Bedrock-like APIs."""

    parts: list[str] = []
    for block in blocks or []:
        if isinstance(block, Mapping):
            value = block.get("text")
        else:
            value = getattr(block, "text", None)
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)
