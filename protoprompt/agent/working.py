"""WorkingMemory: scored, reversible memory for autonomous agents.

Hot set lives in RAM, scored by :class:`MemoryScorer`. When the token
budget overflows, the lowest-scoring non-pinned items are evicted into
the cold zone (a ``StoreProtocol``) and recorded in a ``Manifest``.
``recall`` pulls cold items back as fresh hot items — forgetting is a
demotion, not a deletion.

Self-notes (``note()``) are pinned by default: the agent's own distilled
observations survive while raw tool output dies young.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable

from protoprompt.agent.goal import GoalTracker
from protoprompt.agent.manifest import Manifest
from protoprompt.agent.references import (
    ReferenceIndex,
    extract_definitions,
    extract_identifiers,
)
from protoprompt.agent.scorer import MemoryScorer, ScorerWeights
from protoprompt.agent.types import (
    AssembledContext,
    ContextBlock,
    Kind,
    MemoryItem,
)
from protoprompt.llm import LLMClientProtocol
from protoprompt.store.protocol import StoreProtocol, await_if_needed
from protoprompt.tokens.protocol import TokenCounter
from protoprompt.tokens.regex_counter import RegexTokenCounter

logger = logging.getLogger(__name__)

_COLD_NS_KEY = "cold_ns"
_COLD_DOC_PREFIX = "cold"
#: виды элементов, карантин для которых ослаблен (см. recall)
_IMPORTANT_KINDS = frozenset({"edit", "note", "recalled"})

TraceCallback = Callable[[str, dict[str, Any]], None]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class WorkingMemory:
    def __init__(
        self,
        store: StoreProtocol | None = None,
        llm: LLMClientProtocol | None = None,
        counter: TokenCounter | None = None,
        max_tokens: int = 2048,
        weights: ScorerWeights | None = None,
        namespace: str = "agent",
        embed_model: str = "nomic-embed-text",
        trace: TraceCallback | None = None,
        dedup_threshold: float = 0.92,
        max_pinned_tokens: int | None = None,
        recall_cooldown_steps: int = 0,
        recall_bypass_sim: float | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._counter: TokenCounter = counter or RegexTokenCounter()
        self._max_tokens = max_tokens
        self._namespace = namespace
        self._embed_model = embed_model
        self.scorer = MemoryScorer(weights)
        self.goal = GoalTracker(llm, embed_model)
        self.manifest = Manifest()
        self._index = ReferenceIndex()
        self._items: dict[str, MemoryItem] = {}
        self._step = 0
        self.evictions = 0
        self._trace = trace
        #: cosine above which a new note is considered a duplicate
        self.dedup_threshold = dedup_threshold
        #: when set, auto-unpins oldest pinned items beyond this budget
        self.max_pinned_tokens = max_pinned_tokens
        #: свежевыгнанное нельзя вернуть это число шагов
        self.recall_cooldown_steps = recall_cooldown_steps
        #: порог похожести запроса к холодному документу, выше которого
        #: карантин обходится (целенаправленный поиск ≠ слепый рерид)
        self.recall_bypass_sim = recall_bypass_sim

    def _emit(self, event: str, **data: Any) -> None:
        if self._trace is not None:
            try:
                self._trace(event, data)
            except Exception:
                logger.exception("trace callback failed")

    # ── introspection ────────────────────────────────────────────

    @property
    def items(self) -> dict[str, MemoryItem]:
        return self._items

    @property
    def step(self) -> int:
        return self._step

    @property
    def used_tokens(self) -> int:
        return sum(i.tokens for i in self._items.values())

    # ── goal ─────────────────────────────────────────────────────

    async def set_goal(self, text: str) -> None:
        await self.goal.update(text)

    # ── writing ──────────────────────────────────────────────────

    async def add(
        self,
        kind: Kind,
        text: str,
        *,
        summary: str = "",
        pin: bool = False,
    ) -> str:
        """Record a new memory item and rebalance the budget."""
        self._step += 1
        item = MemoryItem(
            kind=kind,
            text=text,
            step=self._step,
            tokens=self._counter.count(text),
            refs=extract_identifiers(text),
            defs=extract_definitions(text),
            pinned=pin,
            summary=summary or "",
        )
        touched = self._index.on_add(item, self._items)
        for owner_id, names in touched:
            self._emit(
                "reference",
                source_id=item.id,
                target_id=owner_id,
                names=sorted(names),
                target_refs=self._items[owner_id].refcount,
                step=item.step,
            )

        if self._llm is not None:
            item.vector = (
                await self._llm.embed([text], model=self._embed_model)
            )[0]

        self._items[item.id] = item
        birth = self.scorer.explain(
            item, now=self._step, goal_vector=self.goal.vector
        )
        self._emit(
            "add",
            item_id=item.id,
            kind=item.kind,
            tokens=item.tokens,
            pinned=item.pinned,
            summary=item.summary or item.label,
            n_refs=len(item.refs),
            defs=sorted(item.defs),
            score=birth["total"],
            terms=birth,
        )
        if item.pinned:
            self._enforce_pin_cap()
        await self._enforce_budget()
        return item.id

    async def note(self, text: str, *, pin: bool = True) -> str:
        """The agent's own distillation — pinned unless asked otherwise.

        Near-duplicates of an existing live note are merged into it
        (touch + keep the longer text) instead of growing the budget.
        """
        if self._llm is not None:
            dup = await self._find_duplicate_note(text)
            if dup is not None:
                self._step += 1
                dup.last_touched = self._step
                if len(text) > len(dup.text):
                    old_len = dup.tokens
                    dup.text = text
                    dup.tokens = self._counter.count(text)
                    dup.summary = (dup.summary or "")[:0] or dup.summary
                    self._emit(
                        "dedup_replaced",
                        kept_id=dup.id,
                        tokens_before=old_len,
                        tokens_after=dup.tokens,
                    )
                else:
                    self._emit("dedup", kept_id=dup.id)
                return dup.id
        return await self.add("note", text, pin=pin)

    async def _find_duplicate_note(self, text: str) -> MemoryItem | None:
        if self.dedup_threshold <= 0:
            return None
        vec = (
            await self._llm.embed([text], model=self._embed_model)  # type: ignore[union-attr]
        )[0]
        best: MemoryItem | None = None
        best_sim = 0.0
        for item in self._items.values():
            if item.kind != "note" or item.vector is None:
                continue
            sim = _cosine(vec, item.vector)
            if sim > best_sim:
                best, best_sim = item, sim
        if best is not None and best_sim >= self.dedup_threshold:
            return best
        return None

    def _enforce_pin_cap(self) -> None:
        if self.max_pinned_tokens is None:
            return
        guard = 0
        while guard < 10_000:
            guard += 1
            pinned = [i for i in self._items.values() if i.pinned]
            spent = sum(i.tokens for i in pinned)
            if spent <= self.max_pinned_tokens or not pinned:
                return
            victim = min(pinned, key=lambda i: (i.step, i.id))
            victim.pinned = False
            self._emit(
                "unpin_auto",
                item_id=victim.id,
                pinned_tokens_before=spent,
                cap=self.max_pinned_tokens,
            )

    def pin(self, item_id: str) -> bool:
        item = self._items.get(item_id)
        if item is None:
            return False
        item.pinned = True
        return True

    def unpin(self, item_id: str) -> bool:
        item = self._items.get(item_id)
        if item is None:
            return False
        item.pinned = False
        return True

    def touch(self, item_id: str) -> bool:
        """Explicit relevance bump (e.g. the agent re-reads a file)."""
        item = self._items.get(item_id)
        if item is None:
            return False
        self._step += 1
        item.last_touched = self._step
        return True

    async def forget(self, item_id: str) -> bool:
        """Manual demotion to the cold zone."""
        return await self._evict_item(item_id, reason="manual")

    # ── reading ──────────────────────────────────────────────────

    async def assemble(self) -> AssembledContext:
        """Greedy pack the hottest items into the token budget."""
        await self._fill_missing_vectors()

        scored = sorted(
            self._items.values(),
            key=lambda i: self.scorer.score(
                i, now=self._step, goal_vector=self.goal.vector
            ),
            reverse=True,
        )

        context = AssembledContext(budget=self._max_tokens)
        remaining = self._max_tokens
        for item in scored:
            if item.tokens <= remaining:
                remaining -= item.tokens
                context.blocks.append(ContextBlock(
                    item_id=item.id,
                    kind=item.kind,
                    text=item.text,
                    score=round(self.scorer.score(
                        item, now=self._step,
                        goal_vector=self.goal.vector,
                    ), 3),
                ))
            else:
                context.skipped_ids.append(item.id)
        context.used_tokens = self._max_tokens - remaining
        context.manifest_lines = self.manifest.lines()
        return context

    async def recall(self, query: str, top_k: int = 3) -> list[str]:
        """Pull cold items back into the hot set (two channels, batch-planned).

        Канал 1 (детерминированный): идентификаторы запроса ищутся в
        индексе символов манифеста — попадание обходит и карантин, и
        пороги похожести. Работает даже без LLM (текст достаётся через
        ``store.get``).

        Канал 2 (семантический фолбэк): векторный поиск по стору с
        обычными правилами карантина/обхода.

        Место под весь батч освобождается ЗАРАНЕЕ вытеснением слабейших
        старых жильцов, поэтому каскадных вытеснений внутри батча нет.
        """
        restored: list[str] = []
        selected: list[dict] = []
        seen_lineages: set[str] = set()

        def _known(lineage: str) -> bool:
            return bool(lineage) and (
                lineage in seen_lineages
                or any((i.lineage or i.id) == lineage
                       for i in self._items.values())
            )

        # ── канал 1: символы ────────────────────────────────────
        q_idents = extract_identifiers(query)
        getter = getattr(self._store, "get", None)
        for entry in self.manifest.by_symbols(q_idents):
            if len(selected) >= top_k:
                break
            lineage = entry.lineage or entry.item_id
            if _known(lineage):
                continue
            doc = None
            if self._store is not None and callable(getter):
                doc = getter(f"{self._namespace}:{_COLD_DOC_PREFIX}:{lineage}")
                if doc is None and entry.item_id != lineage:
                    doc = getter(
                        f"{self._namespace}:{_COLD_DOC_PREFIX}:{entry.item_id}"
                    )
            if doc is None:
                # текст недоступен — возвращаем хотя бы саммари заметкой
                summary_text = f"(холодный запасник) {entry.summary}"
                selected.append({
                    "text": summary_text,
                    "tok": self._counter.count(summary_text),
                    "meta": {"summary": entry.summary,
                             "orig_id": entry.item_id,
                             "kind": entry.kind},
                    "lineage": lineage, "sim": None, "channel": "symbol",
                })
                seen_lineages.add(lineage)
                continue
            tok = self._counter.count(doc["document"])
            if tok > self._max_tokens:
                self._emit(
                    "recall_skipped",
                    orig_id=str(entry.item_id),
                    reason="never_fits", would_cost=tok,
                )
                continue
            meta = dict(doc.get("metadata") or {})
            meta.setdefault("orig_id", entry.item_id)
            meta.setdefault("kind", entry.kind)
            meta.setdefault("summary", entry.summary)
            selected.append({
                "text": doc["document"], "tok": tok, "meta": meta,
                "lineage": lineage, "sim": None, "channel": "symbol",
            })
            seen_lineages.add(lineage)

        # ── офлайн-фолбэк: keyword по summaries ─────────────────
        if (self._llm is None or self._store is None) and not selected:
            for entry in self.manifest.search(query)[:top_k]:
                new_id = await self.note(
                    f"(холодный запасник) {entry.summary}", pin=False,
                )
                self.manifest.restore(entry.item_id)
                restored.append(new_id)
            return restored

        # ── канал 2: семантика (фолбэк до top_k) ────────────────
        if self._llm is not None and self._store is not None \
                and len(selected) < top_k:
            qvec = (await self._llm.embed([query],
                                          model=self._embed_model))[0]
            hits = await await_if_needed(self._store.query(
                qvec, top_k=max(top_k * 3, top_k + 4),
                where={_COLD_NS_KEY: self._namespace},
            ))
            for hit in hits:
                if len(selected) >= top_k:
                    break
                meta = hit.get("metadata", {})
                evicted_at = int(meta.get("evicted_at", 0))
                under_cooldown = (
                    self.recall_cooldown_steps > 0
                    and self._step - evicted_at < self.recall_cooldown_steps
                )
                important = bool(meta.get("important", False))
                sim = hit.get("score")
                strong = (
                    self.recall_bypass_sim is not None
                    and isinstance(sim, (int, float))
                    and sim >= self.recall_bypass_sim
                )
                if under_cooldown and not important and not strong:
                    self._emit(
                        "recall_cooldown",
                        orig_id=str(meta.get("orig_id", "")),
                        evicted_at=evicted_at,
                        step=self._step,
                        cooldown=self.recall_cooldown_steps,
                    )
                    continue
                text = hit.get("document", "")
                if not text:
                    continue
                tok = self._counter.count(text)
                if tok > self._max_tokens:
                    self._emit(
                        "recall_skipped",
                        orig_id=str(meta.get("orig_id", "")),
                        reason="never_fits", would_cost=tok,
                    )
                    continue
                lineage = str(meta.get("lineage")
                              or meta.get("orig_id") or "")
                if _known(lineage):
                    continue
                selected.append({
                    "text": text, "tok": tok, "meta": meta,
                    "lineage": lineage, "sim": sim, "channel": "semantic",
                })
                seen_lineages.add(lineage)

        if not selected:
            return restored

        # ── батч не может превышать бюджет физически ────────────
        # иначе коммит пойдёт поверх лимита и enforce выкосяит
        # только что восстановленные элементы (каскад churn)
        total = sum(s["tok"] for s in selected)
        while total > self._max_tokens and len(selected) > 1:
            dropped = selected.pop()          # низший приоритет — с хвоста
            total -= dropped["tok"]
            self._emit(
                "recall_skipped",
                orig_id=str(dropped["meta"].get("orig_id", "")),
                reason="batch_over_budget",
                would_cost=dropped["tok"],
            )

        # ── предосвобождение места под весь батч ────────────────
        staged_lineages = {s["lineage"] for s in selected if s["lineage"]}
        need = sum(s["tok"] for s in selected) - (
            self._max_tokens - self.used_tokens
        )
        guard = 0
        while need > 0 and guard < 10_000:
            guard += 1
            victims = [
                i for i in self._items.values()
                if not i.pinned
                and (i.lineage or i.id) not in staged_lineages
            ]
            if not victims:
                logger.warning("recall: no eviction candidates left")
                break
            victim = min(
                victims,
                key=lambda i: self.scorer.score(
                    i, now=self._step, goal_vector=self.goal.vector
                ),
            )
            terms = self.scorer.explain(
                victim, now=self._step, goal_vector=self.goal.vector
            )
            need -= victim.tokens
            await self._evict_item(victim.id, reason="make_room_for_recall",
                                   terms=terms)

        # ── коммит батча: места хватает, enforce не срабатывает ──
        for cand in selected:
            meta = cand["meta"]
            new_id = await self.add(
                "recalled", cand["text"],
                summary=f"recall: {meta.get('summary', '')}",
            )
            item = self._items[new_id]
            try:
                item.refcount = int(meta.get("refcount", 0))
                item.recall_count = int(meta.get("recall_count", 0)) + 1
                item.last_touched = self._step
                item.lineage = str(meta.get("lineage")
                                   or meta.get("orig_id") or new_id)
            except (TypeError, ValueError):
                pass
            orig_id = str(meta.get("orig_id", ""))
            if orig_id:
                self.manifest.restore(orig_id)
            restored.append(new_id)
            self._emit(
                "recall",
                query=query,
                restored_id=new_id,
                cold_orig_id=orig_id,
                kind="recalled",
                similarity=cand.get("sim"),
                channel=cand["channel"],
                recall_count=item.recall_count,
            )
        return restored
    # ── crash safety ─────────────────────────────────────────────

    def export_state(self) -> dict:
        """JSON-serializable snapshot of the hot set (cold zone lives in
        the store already). Feed to :meth:`import_state` after a restart.
        """
        return {
            "step": self._step,
            "evictions": self.evictions,
            "namespace": self._namespace,
            "goal_text": self.goal.text,
            "goal_vector": self.goal.vector,
            "items": [
                {
                    "id": i.id,
                    "kind": i.kind,
                    "text": i.text,
                    "step": i.step,
                    "tokens": i.tokens,
                    "refs": sorted(i.refs),
                    "defs": sorted(i.defs),
                    "pinned": i.pinned,
                    "summary": i.summary,
                    "refcount": i.refcount,
                    "last_touched": i.last_touched,
                    "recall_count": i.recall_count,
                    "lineage": i.lineage,
                    "vector": i.vector,
                }
                for i in self._items.values()
            ],
        }

    def import_state(self, state: dict) -> None:
        """Restore a snapshot produced by :meth:`export_state`."""
        self._step = int(state.get("step", 0))
        self.evictions = int(state.get("evictions", 0))
        self.goal.text = state.get("goal_text", "")
        gv = state.get("goal_vector")
        self.goal.vector = list(gv) if gv else None
        self._items.clear()
        for raw in state.get("items", []):
            item = MemoryItem(
                kind=raw["kind"],
                text=raw["text"],
                step=int(raw["step"]),
                id=raw["id"],
                tokens=int(raw["tokens"]),
                refs=frozenset(raw["refs"]),
                defs=frozenset(raw["defs"]),
                pinned=bool(raw["pinned"]),
                summary=raw.get("summary", ""),
                refcount=int(raw.get("refcount", 0)),
                last_touched=int(raw.get("last_touched", -1)),
                recall_count=int(raw.get("recall_count", 0)),
                lineage=raw.get("lineage", ""),
                vector=list(raw["vector"]) if raw.get("vector") else None,
            )
            self._items[item.id] = item
            for name in item.defs:
                self._index.register_defs(item.id, [name])

    # ── internals ────────────────────────────────────────────────

    async def _fill_missing_vectors(self) -> None:
        if self._llm is None:
            return
        pending = [i for i in self._items.values() if i.vector is None]
        if not pending:
            return
        vectors = await self._llm.embed(
            [i.text for i in pending], model=self._embed_model
        )
        for item, vec in zip(pending, vectors):
            item.vector = vec

    async def _enforce_budget(self) -> None:
        guard = 0
        while self.used_tokens > self._max_tokens and guard < 10_000:
            guard += 1
            victims = [i for i in self._items.values() if not i.pinned]
            if not victims:
                logger.warning(
                    "budget overflow with everything pinned (%d tok)",
                    self.used_tokens,
                )
                return
            victim = min(
                victims,
                key=lambda i: self.scorer.score(
                    i, now=self._step, goal_vector=self.goal.vector
                ),
            )
            terms = self.scorer.explain(
                victim, now=self._step, goal_vector=self.goal.vector
            )
            await self._evict_item(victim.id, reason="over_budget", terms=terms)

    async def _evict_item(
        self,
        item_id: str,
        *,
        reason: str,
        terms: dict[str, float] | None = None,
    ) -> bool:
        item = self._items.pop(item_id, None)
        if item is None:
            return False
        self._index.forget(item_id)
        self.evictions += 1
        lineage = item.lineage or item.id

        if self._store is not None:
            doc_id = f"{self._namespace}:{_COLD_DOC_PREFIX}:{lineage}"
            meta = {
                _COLD_NS_KEY: self._namespace,
                "orig_id": item.id,
                "kind": item.kind,
                "summary": item.summary or item.label,
                "evicted_at": self._step,
                "reason": reason,
                "refcount": item.refcount,
                "recall_count": item.recall_count,
                "orig_step": item.step,
                "lineage": lineage,
                "important": (
                    item.refcount > 0
                    or item.kind in _IMPORTANT_KINDS
                    or item.pinned
                ),
                "symbols": sorted(item.defs | (item.refs & item.defs)),
            }
            vector = item.vector or [0.0]
            # перезаписываем прежнюю копию этой родословной, если была
            await await_if_needed(self._store.delete(doc_id))
            await await_if_needed(self._store.add(doc_id, [item.text], [vector], meta))

        self.manifest.record(
            item_id=item.id,
            kind=item.kind,
            summary=item.summary or item.label,
            tokens=item.tokens,
            evicted_at=self._step,
            symbols=item.defs,
            lineage=lineage,
            important=(
                item.refcount > 0
                or item.kind in _IMPORTANT_KINDS
                or item.pinned
            ),
        )
        self._emit(
            "evict",
            item_id=item.id,
            kind=item.kind,
            tokens=item.tokens,
            summary=item.summary or item.label,
            reason=reason,
            step=self._step,
            terms=terms or {},
            cold_doc=f"{self._namespace}:{_COLD_DOC_PREFIX}:{item.lineage or item.id}",
        )
        logger.info("evicted %s [%s] (%s)", item.id, item.kind, reason)
        return True
