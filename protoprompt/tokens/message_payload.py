"""Deterministic serialization of portable chat messages for token estimates.

Providers receive more than ``message.content``: assistant tool calls, tool
call identifiers, names, and rich content blocks all consume request space.
The counters use this module only for estimation; it never changes the message
object that an integration sends to a provider.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


def message_text(message: Mapping[str, Any]) -> str:
    """Return a stable, complete representation for token counting.

    Plain string ``content`` keeps its historical representation.  Every other
    content shape and every provider-bound message field except ``role`` is
    canonical JSON, so ``tool_calls`` and rich content cannot disappear from a
    budget estimate.  Portable messages must contain JSON-compatible values;
    rejecting arbitrary Python objects is safer than counting a nondeterministic
    ``repr`` that may understate the provider payload.
    """
    if not isinstance(message, Mapping):
        raise TypeError("Portable chat messages must be mappings")

    content_text = _content_text(message.get("content", ""))
    extras = {
        key: value
        for key, value in message.items()
        if key not in {"role", "content"}
    }
    if not extras:
        return content_text

    extras_text = _canonical_json(extras, path="message")
    return f"{content_text}\n{extras_text}" if content_text else extras_text


def _content_text(content: Any) -> str:
    """Keep the established text-block fast path, serialize all else.

    ``[{"text": "..."}]`` is the existing portable text-block shape and has
    always counted as its text alone.  Rich blocks (for example image/audio
    references or typed input blocks) can carry additional provider payload,
    so they intentionally take the canonical JSON path.
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        text_blocks: list[str] = []
        for block in content:
            if isinstance(block, str):
                text_blocks.append(block)
                continue
            if (
                isinstance(block, Mapping)
                and set(block) == {"text"}
                and isinstance(block["text"], str)
            ):
                text_blocks.append(block["text"])
                continue
            return _canonical_json(content, path="content")
        return "".join(text_blocks)
    return _canonical_json(content, path="content")


def _canonical_json(value: Any, *, path: str) -> str:
    return json.dumps(
        _json_value(value, path=path),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_value(value: Any, *, path: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Non-finite float in portable chat message at {path}")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"Portable chat message keys must be strings at {path}"
                )
            normalized[key] = _json_value(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [
            _json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(
        "Portable chat message values must be JSON-compatible "
        f"(unsupported {type(value).__name__} at {path})"
    )
