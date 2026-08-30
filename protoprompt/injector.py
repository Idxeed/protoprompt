from __future__ import annotations

import logging
from time import perf_counter

from protoprompt.context import ContextInput, ContextOutput
from protoprompt.context_plan import ContextRequestReceipt
from protoprompt.events import (
    ContextEvent,
    EventDispatcher,
    EventSink,
    RetrieveEvent,
    dispatch,
    elapsed_ms,
    new_trace_id,
    scope_id,
)
from protoprompt.hooks import ContextHooks, fire
from protoprompt.i18n import section_header
from protoprompt.llm import EmbeddingClientProtocol
from protoprompt.profile.render import render
from protoprompt.rag.retriever import Retriever
from protoprompt.rag.types import RetrievedChunk
from protoprompt.scope import MemoryScope, scoped_doc_id
from protoprompt.store.protocol import StoreProtocol, await_if_needed

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Default context assembler.

    RAG blocks (if any) are queried, session memory is queried when a
    ``chat_id`` is supplied, and the system prompt is the anchor. The
    query is embedded once and reused for both retrievals.

    RAG retrieval goes through a :class:`~protoprompt.rag.retriever.Retriever`;
    pass ``retriever=`` to customize chunking/reranking/scope, otherwise a
    default is built from ``store`` + ``llm``.

    The store may be sync (``StoreProtocol``) or async
    (``AsyncStoreProtocol``); blocking backends wrapped via
    :func:`protoprompt.store.as_async` will not stall the event loop.
    """

    def __init__(
        self,
        store: StoreProtocol,
        llm: EmbeddingClientProtocol,
        hooks: ContextHooks | None = None,
        retriever: Retriever | None = None,
        *,
        scope: MemoryScope | None = None,
        event_sink: EventSink | EventDispatcher | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._hooks = hooks or ContextHooks()
        retriever_scope = retriever.scope if retriever is not None else None
        if scope is not None and retriever_scope is not None and scope != retriever_scope:
            raise ValueError("ContextBuilder scope conflicts with retriever scope")
        self._scope = scope if scope is not None else retriever_scope
        self._event_sink = event_sink
        self._retriever = retriever or Retriever(
            store, llm, scope=self._scope, event_sink=event_sink
        )
        self._last_report: "object | None" = None
        self._last_receipt: ContextRequestReceipt | None = None

    @property
    def last_report(self) -> "object | None":
        """``BudgetReport`` of the most recent ``build``/``build_messages``
        call (always ``None`` on the non-budgeted base builder).
        """
        return self._last_report

    @property
    def last_receipt(self) -> ContextRequestReceipt | None:
        """Latest immutable request receipt, retained only for convenience.

        New concurrent code should use the receipt returned by the individual
        planning call; this compatibility property can naturally be replaced
        by a later build on the same builder.
        """
        return self._last_receipt

    @property
    def scope(self) -> MemoryScope | None:
        """Return the host-configured retrieval scope, if one is pinned.

        This exposes only the immutable scope identity already used by the
        builder and its retriever. It never accepts a per-request override.
        """

        return self._scope

    async def build(self, inp: ContextInput) -> ContextOutput:
        started_at = perf_counter()
        trace_id = new_trace_id()
        parts: list[str] = [inp.system_prompt] if inp.system_prompt else []
        rag_chunks: list[RetrievedChunk] = []
        rag_blocks: list[str] = []
        session_blocks: list[str] = []
        profile_used = False

        query_emb: list[float] | None = None
        if inp.include_rag or (inp.include_session and inp.chat_id):
            query_emb = (await self._llm.embed([inp.query], model=inp.embedding_model))[0]

        if inp.include_rag and query_emb is not None:
            rag_chunks = await self._retriever.retrieve_embedded(
                query_emb,
                query_text=inp.query,
                top_k=inp.top_k_rag,
                doc_ids=inp.doc_ids,
                score_threshold=inp.score_threshold,
                trace_id=trace_id,
            )
            if rag_chunks:
                rag_blocks = [c.text for c in rag_chunks]
                parts.append("\n\n---\n\n".join(rag_blocks))

        if inp.include_session and inp.chat_id and query_emb is not None:
            retrieve_started_at = perf_counter()
            session_results = await await_if_needed(self._store.query(
                query_emb,
                top_k=inp.top_k_session,
                where=self._session_where(inp.chat_id),
            ))
            dispatch(self._event_sink, RetrieveEvent(
                action="completed",
                trace_id=trace_id,
                scope_id=scope_id(self._scope),
                duration_ms=elapsed_ms(retrieve_started_at),
                attributes={
                    "channel": "session",
                    "top_k": inp.top_k_session,
                    "hit_count": len(session_results),
                    "doc_filter_count": 1,
                    "threshold_applied": False,
                },
            ))
            if session_results:
                session_texts = [r["document"] for r in session_results]
                session_blocks = session_texts
                parts.append(
                    f"{section_header('session', inp.language)}\n"
                    + "\n---\n".join(session_texts)
                )

        if inp.include_profile:
            profile_block = ""
            if inp.profile is not None:
                profile_block = render(inp.profile, language=inp.language)
            elif inp.profile_text:
                profile_block = (
                    f"{section_header('profile', inp.language)}\n{inp.profile_text}"
                )
            if profile_block:
                parts.append(profile_block)
                profile_used = True

        output = ContextOutput(
            system_prompt="\n\n".join(parts),
            rag_chunks=rag_chunks,
            rag_blocks=rag_blocks,
            session_blocks=session_blocks,
            profile_used=profile_used,
        )
        dispatch(self._event_sink, ContextEvent(
            action="completed",
            trace_id=trace_id,
            scope_id=scope_id(self._scope),
            duration_ms=elapsed_ms(started_at),
            attributes={
                "budgeted": False,
                "rag_block_count": len(rag_blocks),
                "session_block_count": len(session_blocks),
                "profile_used": profile_used,
            },
        ))
        fire(self._hooks.on_build_done, None)
        return output

    async def build_messages(
        self,
        inp: ContextInput,
        history: list[dict] | None = None,
        user_message: str | None = None,
    ) -> list[dict]:
        """Assemble an OpenAI-style message list ready to send.

        Order: system prompt (from :meth:`build`), then ``history``
        verbatim, then ``user_message``. No trimming happens here; use
        :class:`TokenBudgetedContextBuilder` for budget-aware history.
        """
        out = await self.build(inp)
        messages: list[dict] = []
        if out.system_prompt:
            messages.append({"role": "system", "content": out.system_prompt})
        messages.extend(history or [])
        if user_message:
            messages.append({"role": "user", "content": user_message})
        return messages

    def _session_where(self, chat_id: str) -> dict:
        where = {"doc_id": scoped_doc_id(f"session_{chat_id}", self._scope)}
        return self._scope.merge_where(where) if self._scope is not None else where
