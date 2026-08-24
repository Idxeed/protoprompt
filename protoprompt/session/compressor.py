from __future__ import annotations

import logging

from protoprompt.llm import LLMClientProtocol
from protoprompt.session.strategy import HeuristicStrategy, StrategyProtocol
from protoprompt.session.types import CompressedBlock, Session

logger = logging.getLogger(__name__)


class Compressor:
    def __init__(self, strategy: StrategyProtocol | None = None) -> None:
        self._strategy = strategy or HeuristicStrategy()

    async def compress(self, session: Session, llm: LLMClientProtocol) -> list[CompressedBlock]:
        return await self._strategy.compress(session, llm)
