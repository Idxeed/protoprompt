from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from protoprompt.events import CompressEvent, EventDispatcher, EventSink, dispatch, elapsed_ms, new_trace_id, scope_id
from protoprompt.hooks import PipelineHooks, fire
from protoprompt.llm import (
    ChatClientProtocol,
    EmbeddingClientProtocol,
    LLMClientProtocol,
)
from protoprompt.session.compressor import Compressor
from protoprompt.session.strategy import HeuristicStrategy, StrategyProtocol
from protoprompt.session.types import CompressedBlock, Session
from protoprompt.scope import MemoryScope, scoped_doc_id, scoped_metadata
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
        llm: LLMClientProtocol | None = None,
        strategy: StrategyProtocol | None = None,
        compress_every_n: int = 10,
        embedding_model: str = "nomic-embed-text",
        hooks: PipelineHooks | None = None,
        *,
        chat_client: ChatClientProtocol | None = None,
        embedding_client: EmbeddingClientProtocol | None = None,
        scope: MemoryScope | None = None,
        event_sink: EventSink | EventDispatcher | None = None,
    ) -> None:
        """Create a compression pipeline.

        ``llm`` remains the backward-compatible composite argument. New code
        may supply independent ``chat_client`` and ``embedding_client``
        capabilities, which is useful when chat and embeddings come from
        different providers.
        """
        if llm is not None:
            chat_client = chat_client or llm
            embedding_client = embedding_client or llm
        if chat_client is None or embedding_client is None:
            raise ValueError(
                "Pipeline requires both chat_client and embedding_client "
                "when a composite llm is not supplied"
            )
        self._store = store
        self._chat_client = chat_client
        self._embedding_client = embedding_client
        self._compressor = Compressor(strategy or HeuristicStrategy())
        self._compress_every_n = compress_every_n
        self._embedding_model = embedding_model
        self._hooks = hooks or PipelineHooks()
        self._scope = scope
        self._event_sink = event_sink

    async def compress_and_store(self, session: Session) -> list[CompressedBlock]:
        started_at = perf_counter()
        trace_id = new_trace_id()
        if len(session.messages) < self._compress_every_n:
            dispatch(self._event_sink, CompressEvent(
                action="skipped",
                trace_id=trace_id,
                scope_id=scope_id(self._scope),
                duration_ms=elapsed_ms(started_at),
                attributes={
                    "message_count": len(session.messages),
                    "threshold": self._compress_every_n,
                    "reason": "below_threshold",
                },
            ))
            fire(self._hooks.on_skip_compress, session)
            return []

        dispatch(self._event_sink, CompressEvent(
            action="started",
            trace_id=trace_id,
            scope_id=scope_id(self._scope),
            attributes={"message_count": len(session.messages)},
        ))
        fire(self._hooks.on_before_compress, session)

        blocks = await self._compressor.compress(session, self._chat_client)
        if not blocks:
            return []

        logical_doc_id = f"session_{session.chat_id}"
        doc_id = scoped_doc_id(logical_doc_id, self._scope)
        new_doc_id = f"{doc_id}_new"
        texts = [b.text for b in blocks]
        embeddings = await self._embedding_client.embed(
            texts, model=self._embedding_model
        )

        meta: dict[str, Any] = scoped_metadata(self._scope, {
            "chat_id": session.chat_id,
            "strategy": session.strategy,
            "message_count": len(session.messages),
            "kind": "session",
        }, logical_doc_id=logical_doc_id)
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
        dispatch(self._event_sink, CompressEvent(
            action="completed",
            trace_id=trace_id,
            scope_id=scope_id(self._scope),
            duration_ms=elapsed_ms(started_at),
            attributes={
                "message_count": len(session.messages),
                "block_count": len(blocks),
            },
        ))
        fire(self._hooks.on_after_compress, session, blocks)
        return blocks

    def should_compress(self, message_count: int) -> bool:
        return message_count >= self._compress_every_n
