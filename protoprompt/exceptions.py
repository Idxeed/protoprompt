"""Exceptions raised by the context assembly pipeline."""

from __future__ import annotations


class TokenBudgetExceededError(RuntimeError):
    """Raised when a hard-required section (system prompt) does not fit
    into the configured token budget.

    Soft sections (RAG, session) are dropped silently by the budget
    allocator; only mandatory sections ever trigger this.
    """

    def __init__(self, used: int, budget: int, section: str) -> None:
        super().__init__(
            f"Section '{section}' needs {used} tokens but budget is {budget}"
        )
        self.used = used
        self.budget = budget
        self.section = section
