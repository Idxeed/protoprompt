"""Provider-aware, local token budget estimates."""

from __future__ import annotations

from typing import Any

from protoprompt.tokens.message_payload import message_text
from protoprompt.tokens.regex_counter import RegexTokenCounter

_MESSAGE_OVERHEAD = {
    "openai": 3,
    "anthropic": 3,
    "google": 2,
    "bedrock": 3,
    "ollama": 4,
}


class ProviderTokenCounter:
    """Deterministic local estimate selected by provider.

    This class is intentionally synchronous and network-free. Native provider
    clients expose async ``count_tokens`` for exact, billable counts; use those
    at request-planning boundaries rather than during context assembly.
    """

    def __init__(
        self,
        provider: str,
        *,
        model: str = "",
        fallback: Any | None = None,
    ) -> None:
        normalized = provider.lower().replace("_", "-")
        aliases = {"gemini": "google", "aws-bedrock": "bedrock", "claude": "anthropic"}
        self.provider = aliases.get(normalized, normalized)
        self.model = model
        self._delegate = fallback
        if self._delegate is None and self.provider == "openai":
            try:
                # ``tiktoken_adapter`` keeps the optional dependency lazy.  Do
                # not import it at module load time: protoprompt's core must
                # remain dependency-free.
                from protoprompt.tokens.tiktoken_adapter import TiktokenCounter

                self._delegate = TiktokenCounter(model=model or None)
            except (ImportError, KeyError):
                # ``tiktoken`` is optional and may not know a newly released
                # model name yet.  In either case retain the deterministic,
                # dependency-free fallback rather than failing context
                # assembly.
                self._delegate = RegexTokenCounter()
        elif self._delegate is None:
            self._delegate = RegexTokenCounter()

    def count(self, text: str) -> int:
        return self._delegate.count(text)

    def count_messages(self, messages: list[dict]) -> int:
        overhead = _MESSAGE_OVERHEAD.get(self.provider, 4)
        return sum(
            self.count(message_text(message)) + overhead
            for message in messages
        )
