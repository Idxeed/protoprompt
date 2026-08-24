from __future__ import annotations

import logging
from dataclasses import dataclass, field

from protoprompt.context import ContextInput, ContextOutput
from protoprompt.exceptions import TokenBudgetExceededError
from protoprompt.hooks import ContextHooks, fire
from protoprompt.injector import ContextBuilder
from protoprompt.llm import LLMClientProtocol
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
        llm: LLMClientProtocol,
        counter: TokenCounter | None = None,
        max_tokens: int = 4096,
        priorities: tuple[Priority, ...] = DEFAULT_PRIORITIES,
        hooks: ContextHooks | None = None,
    ) -> None:
        super().__init__(store, llm, hooks=hooks)
        self._counter: TokenCounter = counter or RegexTokenCounter()
        self._max_tokens = max_tokens
        self._priorities = priorities

    async def build(self, inp: ContextInput) -> ContextOutput:
        report = BudgetReport(budget=self._max_tokens)

        system_cost = self._counter.count(inp.system_prompt) if inp.system_prompt else 0
        if system_cost > self._max_tokens:
            raise TokenBudgetExceededError(system_cost, self._max_tokens, SEGMENT_SYSTEM)
        report.section_tokens[SEGMENT_SYSTEM] = system_cost
        remaining = self._max_tokens - system_cost
        fire(self._hooks.on_section_used, SEGMENT_SYSTEM, system_cost)

        if inp.include_profile and inp.profile_text:
            profile_block = f"Профиль пользователя:\n{inp.profile_text}"
            profile_cost = self._counter.count(profile_block)
            if profile_cost > remaining:
                logger.warning(
                    "Profile block (%d tokens) exceeds remaining budget (%d); dropping",
                    profile_cost, remaining,
                )
                report.dropped_blocks.append(SEGMENT_PROFILE)
                profile_block = ""
                fire(self._hooks.on_block_dropped, SEGMENT_PROFILE, "over_budget")
            else:
                report.section_tokens[SEGMENT_PROFILE] = profile_cost
                remaining -= profile_cost
                fire(self._hooks.on_section_used, SEGMENT_PROFILE, profile_cost)
        else:
            profile_block = ""

        pool: dict[Priority, list[_Candidate]] = {p: [] for p in self._priorities}

        if (inp.include_rag and inp.doc_ids) or (inp.include_session and inp.chat_id):
            query_emb = (await self._llm.embed([inp.query], model=inp.embedding_model))[0]

            if inp.include_rag and inp.doc_ids:
                str_doc_ids = [str(d) for d in inp.doc_ids]
                where = (
                    {"doc_id": {"$in": str_doc_ids}}
                    if len(str_doc_ids) > 1
                    else {"doc_id": str_doc_ids[0]}
                )
                rag_hits = await await_if_needed(self._store.query(
                    query_emb, top_k=max(1, inp.top_k_rag * 2), where=where
                ))
                for i, hit in enumerate(rag_hits):
                    pool[SEGMENT_RAG].append(_Candidate(
                        section=SEGMENT_RAG,
                        text=hit["document"],
                        label=f"rag[{i}]",
                    ))

            if inp.include_session and inp.chat_id:
                session_hits = await await_if_needed(self._store.query(
                    query_emb,
                    top_k=max(1, inp.top_k_session * 2),
                    where={"doc_id": f"session_{inp.chat_id}"},
                ))
                for i, hit in enumerate(session_hits):
                    pool[SEGMENT_SESSION].append(_Candidate(
                        section=SEGMENT_SESSION,
                        text=hit["document"],
                        label=f"session[{i}]",
                    ))

        kept_rag: list[str] = []
        kept_session: list[str] = []

        for section in self._priorities:
            if section in (SEGMENT_SYSTEM, SEGMENT_PROFILE):
                continue
            if section not in pool or not pool[section]:
                continue
            for cand in pool[section]:
                cost = self._counter.count(cand.text)
                if cost <= remaining:
                    if cand.section == SEGMENT_RAG:
                        kept_rag.append(cand.text)
                    elif cand.section == SEGMENT_SESSION:
                        kept_session.append(cand.text)
                    remaining -= cost
                    report.section_tokens[cand.label] = cost
                    fire(self._hooks.on_section_used, cand.label, cost)
                else:
                    trimmed = self._truncate_to_budget(cand.text, remaining)
                    if trimmed:
                        if cand.section == SEGMENT_RAG:
                            kept_rag.append(trimmed)
                        elif cand.section == SEGMENT_SESSION:
                            kept_session.append(trimmed)
                        report.section_tokens[cand.label] = remaining
                        fire(self._hooks.on_section_used, cand.label, remaining)
                        remaining = 0
                    else:
                        report.dropped_blocks.append(cand.label)
                        fire(self._hooks.on_block_dropped, cand.label,
                             "truncated_empty" if remaining <= 0 else "over_budget")
                    break
            if remaining <= 0:
                for later_section in self._priorities:
                    if later_section <= section:
                        continue
                    for cand in pool.get(later_section, []):
                        report.dropped_blocks.append(cand.label)
                        fire(self._hooks.on_block_dropped, cand.label, "budget_exhausted")
                break

        report.used_tokens = self._max_tokens - remaining
        report.remaining_tokens = remaining

        parts: list[str] = []
        if inp.system_prompt:
            parts.append(inp.system_prompt)
        if profile_block:
            parts.append(profile_block)
        if kept_rag:
            parts.append("\n\n---\n\n".join(kept_rag))
        if kept_session:
            parts.append("История диалога (сжатая):\n" + "\n---\n".join(kept_session))

        fire(self._hooks.on_build_done, report)
        self._last_report = report

        return ContextOutput(
            system_prompt="\n\n".join(parts),
            rag_blocks=kept_rag,
            session_blocks=kept_session,
            profile_used=bool(profile_block),
            budget_report=report,
        )

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
