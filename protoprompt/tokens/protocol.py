from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenCounter(Protocol):
    """Pluggable token counting.

    Implementations must be cheap (called once per context build) and
    deterministic for a given text. Accuracy is secondary: the goal is a
    reliable budget signal, not exact model tokenization.
    """

    def count(self, text: str) -> int:
        """Return approximate token count for a single string."""
        ...

    def count_messages(self, messages: list[dict]) -> int:
        """Return approximate token count for an OpenAI-style message list.

        Each message contributes its content tokens plus a per-message
        overhead (role markers, separators) which implementations choose.
        """
        ...
