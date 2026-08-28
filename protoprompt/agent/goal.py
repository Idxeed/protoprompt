"""Current-goal tracker: the anchor for the semantic scoring term.

The goal vector changes rarely (when the agent states a new objective),
so re-embedding costs are negligible.
"""

from __future__ import annotations

from protoprompt.llm import EmbeddingClientProtocol


class GoalTracker:
    def __init__(
        self,
        llm: EmbeddingClientProtocol | None = None,
        embed_model: str = "nomic-embed-text",
    ) -> None:
        self._llm = llm
        self._embed_model = embed_model
        self.text: str = ""
        self.vector: list[float] | None = None

    async def update(self, text: str) -> None:
        """Set a new goal and refresh its embedding (if an LLM is wired)."""
        self.text = text
        self.vector = None
        if self._llm is not None and text:
            self.vector = (
                await self._llm.embed([text], model=self._embed_model)
            )[0]

    @property
    def ready(self) -> bool:
        return self.vector is not None
