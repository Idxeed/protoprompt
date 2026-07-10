"""Optional tiktoken-based counter.

Importing this module requires the ``tiktoken`` package, installed via
``pip install "protoprompt[tiktoken]"``. The adapter uses the
``cl100k_base`` encoding by default, which matches GPT-4 / GPT-3.5-turbo
and gives a close-enough signal for most open models.
"""

from __future__ import annotations

from typing import Any

from protoprompt.tokens.protocol import TokenCounter

_PER_MESSAGE_OVERHEAD = 4


class TiktokenCounter:
    """Counter backed by tiktoken's ``cl100k_base`` encoding.

    Args:
        encoding: tiktoken encoding name. Defaults to ``cl100k_base``.
        model: optional model hint, takes precedence over ``encoding`` when
            ``tiktoken.encoding_for_model`` knows the model.
    """

    def __init__(
        self,
        encoding: str = "cl100k_base",
        model: str | None = None,
    ) -> None:
        try:
            import tiktoken
        except ImportError as exc:
            raise ImportError(
                "TiktokenCounter requires the 'tiktoken' package. "
                "Install with: pip install 'protoprompt[tiktoken]'"
            ) from exc

        if model is not None:
            self._enc = tiktoken.encoding_for_model(model)
        else:
            self._enc = tiktoken.get_encoding(encoding)

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._enc.encode(text))

    def count_messages(self, messages: list[dict]) -> int:
        total = 0
        for msg in messages:
            content: Any = msg.get("content", "")
            if not isinstance(content, str):
                content = str(content)
            total += self.count(content) + _PER_MESSAGE_OVERHEAD
        return total
