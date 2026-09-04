"""Default regex-based token counter.

Heuristic, no external deps. Splits on word characters and punctuation,
then applies a per-script density multiplier to approximate the fact that
one CJK ideograph or one Cyrillic word covers more subword tokens than
one ASCII word in modern BPE tokenizers.
"""

from __future__ import annotations

import re

from protoprompt.tokens.message_payload import message_text
from protoprompt.tokens.protocol import TokenCounter

_WORD_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_PER_MESSAGE_OVERHEAD = 4

_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_CJK_RE = re.compile(r"[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]")


class RegexTokenCounter:
    """Cheap multilingual token estimate.

    Density multipliers are empirical:
    - ASCII word ~ 1.0 tokens/word
    - Cyrillic word ~ 1.3 tokens/word (richer morphology)
    - CJK ideograph ~ 1.5 tokens/char (each char often a token on its own)
    """

    def count(self, text: str) -> int:
        if not text:
            return 0
        # Every ASCII token takes the existing ``else`` branch below: neither
        # the CJK nor Cyrillic density adjustment can apply.  Keep that common
        # path in the regex engine instead of performing two Unicode searches
        # for every word and punctuation token.  This is deliberately an
        # exact specialization, not a different estimate; ``_WORD_RE`` still
        # defines the token boundaries.
        if text.isascii():
            return sum(1 for _ in _WORD_RE.finditer(text))
        tokens = 0
        for match in _WORD_RE.finditer(text):
            chunk = match.group(0)
            if _CJK_RE.search(chunk):
                tokens += max(1, int(len(chunk) * 1.5))
            elif _CYRILLIC_RE.search(chunk):
                tokens += max(1, int(len(chunk) * 0.6 * 1.3))
            else:
                tokens += 1
        return tokens

    def count_messages(self, messages: list[dict]) -> int:
        total = 0
        for msg in messages:
            total += self.count(message_text(msg)) + _PER_MESSAGE_OVERHEAD
        return total
