from __future__ import annotations

import logging

from protoprompt.context import ContextInput, ContextOutput
from protoprompt.hooks import ContextHooks, fire
from protoprompt.llm import LLMClientProtocol
from protoprompt.store.protocol import StoreProtocol, await_if_needed

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Default context assembler.

    RAG blocks (if any) are queried, session memory is queried when a
    ``chat_id`` is supplied, and the system prompt is the anchor. The
    query is embedded once and reused for both retrievals.

    The store may be sync (``StoreProtocol``) or async
    (``AsyncStoreProtocol``); blocking backends wrapped via
    :func:`protoprompt.store.as_async` will not stall the event loop.
    """

    def __init__(
        self,
        store: StoreProtocol,
        llm: LLMClientProtocol,
        hooks: ContextHooks | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._hooks = hooks or ContextHooks()
        self._last_report: "object | None" = None

    @property
    def last_report(self) -> "object | None":
        """``BudgetReport`` of the most recent ``build``/``build_messages``
        call (always ``None`` on the non-budgeted base builder).
        """
        return self._last_report

    async def build(self, inp: ContextInput) -> ContextOutput:
        parts: list[str] = [inp.system_prompt] if inp.system_prompt else []
        rag_blocks: list[str] = []
        session_blocks: list[str] = []
        profile_used = False

        query_emb: list[float] | None = None
        if (inp.include_rag and inp.doc_ids) or (inp.include_session and inp.chat_id):
            query_emb = (await self._llm.embed([inp.query], model=inp.embedding_model))[0]

        if inp.include_rag and inp.doc_ids and query_emb is not None:
            str_doc_ids = [str(d) for d in inp.doc_ids]
            where = {"doc_id": {"$in": str_doc_ids}} if len(str_doc_ids) > 1 else {"doc_id": str_doc_ids[0]}
            results = await await_if_needed(
                self._store.query(query_emb, top_k=inp.top_k_rag, where=where)
            )
            if results:
                rag_texts = [r["document"] for r in results]
                rag_blocks = rag_texts
                parts.append("\n\n---\n\n".join(rag_texts))

        if inp.include_session and inp.chat_id and query_emb is not None:
            session_results = await await_if_needed(self._store.query(
                query_emb,
                top_k=inp.top_k_session,
                where={"doc_id": f"session_{inp.chat_id}"},
            ))
            if session_results:
                session_texts = [r["document"] for r in session_results]
                session_blocks = session_texts
                parts.append(
                    "История диалога (сжатая):\n" + "\n---\n".join(session_texts)
                )

        if inp.include_profile and inp.profile_text:
            parts.append(f"Профиль пользователя:\n{inp.profile_text}")
            profile_used = True

        fire(self._hooks.on_build_done, None)

        return ContextOutput(
            system_prompt="\n\n".join(parts),
            rag_blocks=rag_blocks,
            session_blocks=session_blocks,
            profile_used=profile_used,
        )

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
