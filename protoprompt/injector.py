from __future__ import annotations

import logging

from protoprompt.context import ContextInput, ContextOutput
from protoprompt.llm import LLMClientProtocol
from protoprompt.store.protocol import StoreProtocol

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Default context assembler.

    RAG blocks (if any) are queried, session memory is queried when a
    ``chat_id`` is supplied, and the system prompt is the anchor. The
    query is embedded once and reused for both retrievals.
    """

    def __init__(
        self,
        store: StoreProtocol,
        llm: LLMClientProtocol,
    ) -> None:
        self._store = store
        self._llm = llm

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
            results = self._store.query(query_emb, top_k=inp.top_k_rag, where=where)
            if results:
                rag_texts = [r["document"] for r in results]
                rag_blocks = rag_texts
                parts.append("\n\n---\n\n".join(rag_texts))

        if inp.include_session and inp.chat_id and query_emb is not None:
            session_results = self._store.query(
                query_emb,
                top_k=inp.top_k_session,
                where={"doc_id": f"session_{inp.chat_id}"},
            )
            if session_results:
                session_texts = [r["document"] for r in session_results]
                session_blocks = session_texts
                parts.append(
                    "История диалога (сжатая):\n" + "\n---\n".join(session_texts)
                )

        if inp.include_profile and inp.profile_text:
            parts.append(f"Профиль пользователя:\n{inp.profile_text}")
            profile_used = True

        return ContextOutput(
            system_prompt="\n\n".join(parts),
            rag_blocks=rag_blocks,
            session_blocks=session_blocks,
            profile_used=profile_used,
        )
