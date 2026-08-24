from __future__ import annotations

import logging
from typing import Any

from protoprompt.hooks import PipelineHooks, fire
from protoprompt.llm import LLMClientProtocol
from protoprompt.session.compressor import Compressor
from protoprompt.session.strategy import HeuristicStrategy, StrategyProtocol
from protoprompt.session.types import CompressedBlock, Session
from protoprompt.store.protocol import StoreProtocol, await_if_needed

logger = logging.getLogger(__name__)


class Pipeline:
    """Orchestrate session compression and persistence into a vector store.

    The pipeline decides when compression should run (``should_compress``)
    and is the only place that should write compressed session data to
    the store. The write is performed atomically: the new doc_id is
    written first, then the old one is removed. If the process dies in
    between, the next call overwrites both, so no chunk is lost.

    Sync and async stores are both accepted; blocking calls on a sync
    store are transparently dispatched through :func:`await_if_needed`
    (wrap it with :func:`protoprompt.store.as_async` to offload heavy
    backends onto worker threads).
    """

    def __init__(
        self,
        store: StoreProtocol,
        llm: LLMClientProtocol,
        strategy: StrategyProtocol | None = None,
        compress_every_n: int = 10,
        embedding_model: str = "nomic-embed-text",
        hooks: PipelineHooks | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._compressor = Compressor(strategy or HeuristicStrategy())
        self._compress_every_n = compress_every_n
        self._embedding_model = embedding_model
        self._hooks = hooks or PipelineHooks()

    async def compress_and_store(self, session: Session) -> list[CompressedBlock]:
        if len(session.messages) < self._compress_every_n:
            fire(self._hooks.on_skip_compress, session)
            return []

        fire(self._hooks.on_before_compress, session)

        blocks = await self._compressor.compress(session, self._llm)
        if not blocks:
            return []

        doc_id = f"session_{session.chat_id}"
        new_doc_id = f"{doc_id}_new"
        texts = [b.text for b in blocks]
        embeddings = await self._llm.embed(texts, model=self._embedding_model)

        meta: dict[str, Any] = {
            "chat_id": session.chat_id,
            "strategy": session.strategy,
            "message_count": len(session.messages),
        }
        await await_if_needed(self._store.add(new_doc_id, texts, embeddings, meta))
        await await_if_needed(self._store.delete(doc_id))
        await await_if_needed(self._store.add(doc_id, texts, embeddings, meta))
        await await_if_needed(self._store.delete(new_doc_id))

        logger.info(
            "Compressed session %s: %d messages -> %d blocks",
            session.chat_id,
            len(session.messages),
            len(blocks),
        )
        fire(self._hooks.on_after_compress, session, blocks)
        return blocks

    def should_compress(self, message_count: int) -> bool:
        return message_count >= self._compress_every_n
