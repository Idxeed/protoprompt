"""Exceptions raised by the context assembly pipeline."""

from __future__ import annotations


class TokenBudgetExceededError(RuntimeError):
    """Raised when a required request section cannot fit its token budget.

    System context, the caller's current turn, and an explicit output reserve
    are hard requirements.  Soft sections (RAG, session) are dropped by the
    allocator instead of overflowing the request.
    """

    def __init__(self, used: int, budget: int, section: str) -> None:
        super().__init__(
            f"Section '{section}' needs {used} tokens but budget is {budget}"
        )
        self.used = used
        self.budget = budget
        self.section = section
