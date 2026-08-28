from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from time import perf_counter

from protoprompt.context import ContextInput, ContextOutput
from protoprompt.exceptions import TokenBudgetExceededError
from protoprompt.events import ContextEvent, EventDispatcher, EventSink, RetrieveEvent, dispatch, elapsed_ms, new_trace_id, scope_id
from protoprompt.hooks import ContextHooks, fire
from protoprompt.i18n import section_header
from protoprompt.injector import ContextBuilder
from protoprompt.llm import EmbeddingClientProtocol
from protoprompt.profile.render import render
from protoprompt.rag.retriever import Retriever
from protoprompt.rag.types import RetrievedChunk
from protoprompt.scope import MemoryScope
from protoprompt.store.protocol import StoreProtocol, await_if_needed
from protoprompt.tokens.protocol import TokenCounter
from protoprompt.tokens.regex_counter import RegexTokenCounter

logger = logging.getLogger(__name__)

Priority = str
SEGMENT_RAG = "rag"
SEGMENT_SESSION = "session"
SEGMENT_PROFILE = "profile"
SEGMENT_SYSTEM = "system"

DEFAULT_PRIORITIES: tuple[Priority, ...] = (
    SEGMENT_SYSTEM,
    SEGMENT_PROFILE,
    SEGMENT_SESSION,
    SEGMENT_RAG,
)


@dataclass
class BudgetReport:
    """Observability for a single context build.

    ``used_tokens`` is the final size of the assembled ``system_prompt``
    counted via the supplied ``TokenCounter``. ``dropped_blocks`` lists
    block identifiers that did not fit; UI may surface this to the user.
    ``history_kept``/``history_tokens`` are filled by
    :meth:`TokenBudgetedContextBuilder.build_messages`.
    """

    used_tokens: int = 0
    budget: int = 0
    remaining_tokens: int = 0
    dropped_blocks: list[str] = field(default_factory=list)
    section_tokens: dict[str, int] = field(default_factory=dict)
    history_kept: int = 0
    history_tokens: int = 0


@dataclass
class _Candidate:
    section: Priority
    text: str
    label: str
    chunk: RetrievedChunk | None = None


class TokenBudgetedContextBuilder(ContextBuilder):
    """ContextBuilder that enforces a hard token ceiling on the final
    ``system_prompt``.

    Behaviour:
    1. ``system_prompt`` is mandatory and never truncated. If it does not
       fit into ``max_tokens`` alone, ``TokenBudgetExceededError`` is
       raised.
    2. ``profile_text`` is appended in full when ``include_profile`` is
       true; if it would push us over budget, the profile is dropped and
       ``dropped_blocks`` is updated (not raised — profile is a hint).
    3. RAG and session blocks are pooled (top_k * 2) and allocated
       greedily in priority order. The last accepted block is truncated
       at a word boundary if it does not fit whole.
    4. ``BudgetReport`` is attached to the returned ``ContextOutput``
       via ``budget_report`` so the caller can surface a usage indicator.
    5. ``build_messages`` additionally trims ``history`` oldest-first to
       whatever budget remains after assembly; the newest user message
       is always kept.
    """

    def __init__(
        self,
        store: StoreProtocol,
        llm: EmbeddingClientProtocol,
        counter: TokenCounter | None = None,
        max_tokens: int = 4096,
        priorities: tuple[Priority, ...] = DEFAULT_PRIORITIES,
        hooks: ContextHooks | None = None,
        retriever: Retriever | None = None,
        *,
        scope: MemoryScope | None = None,
        event_sink: EventSink | EventDispatcher | None = None,
    ) -> None:
        super().__init__(
            store,
            llm,
            hooks=hooks,
            retriever=retriever,
            scope=scope,
            event_sink=event_sink,
        )
        self._counter: TokenCounter = counter or RegexTokenCounter()
        self._max_tokens = max_tokens
        self._priorities = priorities

    async def build(self, inp: ContextInput) -> ContextOutput:
        started_at = perf_counter()
        trace_id = new_trace_id()
        report = BudgetReport(budget=self._max_tokens)

        system_cost = self._counter.count(inp.system_prompt) if inp.system_prompt else 0
        if system_cost > self._max_tokens:
            raise TokenBudgetExceededError(system_cost, self._max_tokens, SEGMENT_SYSTEM)
        report.section_tokens[SEGMENT_SYSTEM] = system_cost
        fire(self._hooks.on_section_used, SEGMENT_SYSTEM, system_cost)

        requested_profile = ""
        if inp.include_profile:
            if inp.profile is not None:
                requested_profile = render(inp.profile, language=inp.language)
            elif inp.profile_text:
                requested_profile = (
                    f"{section_header('profile', inp.language)}\n{inp.profile_text}"
                )

        profile_block = ""
        used_tokens = system_cost
        if requested_profile:
            proposed = self._assemble_prompt(
                inp.system_prompt, requested_profile, [], [], inp.language
            )
            proposed_cost = self._counter.count(proposed)
            if proposed_cost > self._max_tokens:
                logger.warning(
                    "Profile block would exceed context budget (%d > %d); dropping",
                    proposed_cost,
                    self._max_tokens,
                )
                report.dropped_blocks.append(SEGMENT_PROFILE)
                fire(self._hooks.on_block_dropped, SEGMENT_PROFILE, "over_budget")
            else:
                profile_block = requested_profile
                profile_cost = proposed_cost - used_tokens
                used_tokens = proposed_cost
                report.section_tokens[SEGMENT_PROFILE] = profile_cost
                fire(self._hooks.on_section_used, SEGMENT_PROFILE, profile_cost)

        pool: dict[Priority, list[_Candidate]] = {
            SEGMENT_RAG: [],
            SEGMENT_SESSION: [],
        }

        if inp.include_rag or (inp.include_session and inp.chat_id):
            query_emb = (await self._llm.embed([inp.query], model=inp.embedding_model))[0]

            if inp.include_rag:
                rag_chunks = await self._retriever.retrieve_embedded(
                    query_emb,
                    query_text=inp.query,
                    top_k=max(1, inp.top_k_rag * 2),
                    doc_ids=inp.doc_ids,
                    score_threshold=inp.score_threshold,
                    trace_id=trace_id,
                )
                for i, chunk in enumerate(rag_chunks):
                    pool[SEGMENT_RAG].append(_Candidate(
                        section=SEGMENT_RAG,
                        text=chunk.text,
                        label=f"rag[{i}]",
                        chunk=chunk,
                    ))

            if inp.include_session and inp.chat_id:
                retrieve_started_at = perf_counter()
                session_hits = await await_if_needed(self._store.query(
                    query_emb,
                    top_k=max(1, inp.top_k_session * 2),
                    where=self._session_where(inp.chat_id),
                ))
                dispatch(self._event_sink, RetrieveEvent(
                    action="completed",
                    trace_id=trace_id,
                    scope_id=scope_id(self._scope),
                    duration_ms=elapsed_ms(retrieve_started_at),
                    attributes={
                        "channel": "session",
                        "top_k": max(1, inp.top_k_session * 2),
                        "hit_count": len(session_hits),
                        "doc_filter_count": 1,
                        "threshold_applied": False,
                    },
                ))
                for i, hit in enumerate(session_hits):
                    pool[SEGMENT_SESSION].append(_Candidate(
                        section=SEGMENT_SESSION,
                        text=hit["document"],
                        label=f"session[{i}]",
                    ))

        kept_rag: list[str] = []
        kept_rag_chunks: list[RetrievedChunk] = []
        kept_session: list[str] = []

        def proposed_total(cand: _Candidate, text: str) -> int:
            rag = [*kept_rag, text] if cand.section == SEGMENT_RAG else kept_rag
            session = (
                [*kept_session, text]
                if cand.section == SEGMENT_SESSION
                else kept_session
            )
            assembled = self._assemble_prompt(
                inp.system_prompt, profile_block, rag, session, inp.language
            )
            return self._counter.count(assembled)

        def accept(cand: _Candidate, text: str, total: int) -> None:
            nonlocal used_tokens
            incremental_cost = total - used_tokens
            if cand.section == SEGMENT_RAG:
                kept_rag.append(text)
                if cand.chunk is not None:
                    kept_rag_chunks.append(
                        cand.chunk if text == cand.text else replace(cand.chunk, text=text)
                    )
            else:
                kept_session.append(text)
            used_tokens = total
            report.section_tokens[cand.label] = incremental_cost
            fire(self._hooks.on_section_used, cand.label, incremental_cost)

        def drop(cand: _Candidate, reason: str) -> None:
            if cand.label not in report.dropped_blocks:
                report.dropped_blocks.append(cand.label)
                fire(self._hooks.on_block_dropped, cand.label, reason)

        active_sections = [
            section
            for section in self._priorities
            if section in (SEGMENT_RAG, SEGMENT_SESSION)
        ]
        stop_all = False
        for section_index, section in enumerate(active_sections):
            if not pool[section]:
                continue
            for candidate_index, cand in enumerate(pool[section]):
                total = proposed_total(cand, cand.text)
                if total <= self._max_tokens:
                    accept(cand, cand.text, total)
                    continue

                trimmed = self._truncate_to_fit(
                    cand.text,
                    lambda value: proposed_total(cand, value) <= self._max_tokens,
                )
                if trimmed:
                    accept(cand, trimmed, proposed_total(cand, trimmed))
                    for skipped in pool[section][candidate_index + 1:]:
                        drop(skipped, "budget_exhausted")
                    for later in active_sections[section_index + 1:]:
                        for skipped in pool[later]:
                            drop(skipped, "budget_exhausted")
                    stop_all = True
                else:
                    drop(cand, "over_budget")
                    for skipped in pool[section][candidate_index + 1:]:
                        drop(skipped, "over_budget")
                break
            if stop_all:
                break

        system_prompt = self._assemble_prompt(
            inp.system_prompt, profile_block, kept_rag, kept_session, inp.language
        )
        report.used_tokens = self._counter.count(system_prompt)
        report.remaining_tokens = self._max_tokens - report.used_tokens

        output = ContextOutput(
            system_prompt=system_prompt,
            rag_chunks=kept_rag_chunks,
            rag_blocks=kept_rag,
            session_blocks=kept_session,
            profile_used=bool(profile_block),
            budget_report=report,
        )
        dispatch(self._event_sink, ContextEvent(
            action="completed",
            trace_id=trace_id,
            scope_id=scope_id(self._scope),
            duration_ms=elapsed_ms(started_at),
            attributes={
                "budgeted": True,
                "budget": report.budget,
                "used_tokens": report.used_tokens,
                "remaining_tokens": report.remaining_tokens,
                "dropped_block_count": len(report.dropped_blocks),
                "rag_block_count": len(kept_rag),
                "session_block_count": len(kept_session),
                "profile_used": bool(profile_block),
            },
        ))
        fire(self._hooks.on_build_done, report)
        self._last_report = report
        return output

    @staticmethod
    def _assemble_prompt(
        system_prompt: str,
        profile_block: str,
        rag_blocks: list[str],
        session_blocks: list[str],
        language: str,
    ) -> str:
        parts: list[str] = []
        if system_prompt:
            parts.append(system_prompt)
        if profile_block:
            parts.append(profile_block)
        if rag_blocks:
            parts.append("\n\n---\n\n".join(rag_blocks))
        if session_blocks:
            parts.append(
                f"{section_header('session', language)}\n"
                + "\n---\n".join(session_blocks)
            )
        return "\n\n".join(parts)

    @staticmethod
    def _truncate_to_fit(text: str, fits) -> str:
        """Return the longest word-boundary prefix accepted by ``fits``."""
        words = text.split()
        low, high = 0, len(words)
        while low < high:
            mid = (low + high + 1) // 2
            candidate = " ".join(words[:mid]) + "…"
            if fits(candidate):
                low = mid
            else:
                high = mid - 1
        return " ".join(words[:low]) + "…" if low else ""

    async def build_messages(
        self,
        inp: ContextInput,
        history: list[dict] | None = None,
        user_message: str | None = None,
    ) -> list[dict]:
        """Build the prompt, then trim ``history`` oldest-first into the
        remaining token budget. The final ``user_message`` is always kept.
        """
        out = await self.build(inp)
        report = out.budget_report
        assert report is not None
        remaining = self._max_tokens - report.used_tokens

        messages: list[dict] = []
        if out.system_prompt:
            messages.append({"role": "system", "content": out.system_prompt})

        kept_history: list[dict] = []
        if history:
            kept_costs: list[int] = []
            spent = 0
            for i in range(len(history) - 1, -1, -1):
                cost = self._counter.count_messages([history[i]])
                if spent + cost > remaining:
                    fire(self._hooks.on_block_dropped, f"history[{i}]", "over_budget")
                    continue
                spent += cost
                kept_costs.append(cost)
                kept_history.insert(0, history[i])
            report.history_kept = len(kept_history)
            report.history_tokens = spent
            remaining -= spent
            messages.extend(kept_history)

        report.remaining_tokens = remaining

        if user_message:
            messages.append({"role": "user", "content": user_message})
        self._last_report = report
        return messages

    def _truncate_to_budget(self, text: str, budget: int) -> str:
        """Cut ``text`` so it fits into ``budget`` tokens, ending on a
        word boundary. Returns empty string if no content fits.
        """
        if budget <= 0:
            return ""
        words = text.split()
        out: list[str] = []
        used = 0
        for w in words:
            w_cost = self._counter.count(w) + 1
            if used + w_cost > budget:
                break
            out.append(w)
            used += w_cost
        if not out:
            return ""
        return " ".join(out) + "…"
