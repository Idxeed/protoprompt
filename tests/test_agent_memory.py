"""Tests for protoprompt.agent: reference tracking, significance
scoring, eviction, manifest/recall roundtrip, budget packing."""

from __future__ import annotations

import pytest

from protoprompt.agent import (
    KIND_WEIGHTS,
    MemoryScorer,
    ScorerWeights,
    WorkingMemory,
    extract_definitions,
    extract_identifiers,
)
from protoprompt import InMemStore, SqliteStore

from _mocks import MockLLM


# ── identifier extraction ────────────────────────────────────────


def test_extract_identifiers_filters_keywords():
    ids = extract_identifiers("def retry_it(self, sleep): return stop")
    assert "retry_it" in ids and "sleep" in ids and "stop" in ids
    assert "self" not in ids and "def" not in ids and "return" not in ids


def test_extract_definitions_finds_def_class_assign():
    text = (
        "class Retryer:\n"
        "    def __init__(self):\n"
        "        self.count = 0\n"
        "\n"
        "MAX_TRIES = 5\n"
        "async def wait_fn(x=1):\n"
        "    pass\n"
    )
    defs = extract_definitions(text)
    assert "Retryer" in defs
    assert "wait_fn" in defs
    assert "MAX_TRIES" in defs
    assert "count" not in defs  # attribute, not module-level assign


def test_extract_definitions_ignores_comparison():
    defs = extract_definitions("if x == 5:\n    pass\n")
    assert not defs


# ── scoring ──────────────────────────────────────────────────────


def _item(kind="log", tokens=50, refcount=0, step=1, last_touched=None):
    from protoprompt.agent.types import MemoryItem

    return MemoryItem(
        kind=kind, text="x", step=step, tokens=tokens,
        refcount=refcount,
        last_touched=(step if last_touched is None else last_touched),
    )


def test_scorer_kind_dominates_for_same_shape():
    scorer = MemoryScorer()
    edit = _item(kind="edit", step=3)
    log = _item(kind="log", step=4)  # fresher but worthless kind
    now = 5
    assert scorer.score(edit, now=now) > scorer.score(log, now=now)


def test_scorer_reference_beats_freshness():
    scorer = MemoryScorer()
    referenced_file = _item(kind="file", refcount=3, step=1, last_touched=9)
    fresh_junk = _item(kind="log", step=10)
    # referenced file was mentioned at step 9; junk born at step 10
    assert scorer.score(referenced_file, now=10) > scorer.score(fresh_junk, now=10)


def test_scorer_size_penalty_discourages_bloat():
    w = ScorerWeights(recency=0.0, size=0.6)
    scorer = MemoryScorer(w)
    big = _item(kind="tool_output", tokens=4000, step=1)
    small = _item(kind="tool_output", tokens=40, step=2)
    assert scorer.score(small, now=3) > scorer.score(big, now=3)


def test_scorer_semantic_term_uses_goal_vector():
    scorer = MemoryScorer()
    goal = [1.0] * 32
    on_topic = _item(kind="file", step=1)
    on_topic.vector = [1.0] * 32          # identical to the goal → sim ≈ 1
    off_topic = _item(kind="file", step=2)  # no vector → semantic term is 0
    assert scorer.score(on_topic, now=2, goal_vector=goal) > \
        scorer.score(off_topic, now=2, goal_vector=goal)


def test_all_builtin_kinds_have_weights():
    for kind in ("edit", "note", "recalled", "file",
                 "test_result", "tool_output", "log"):
        assert kind in KIND_WEIGHTS


# ── WorkingMemory behaviour ──────────────────────────────────────


@pytest.fixture
def memory_factory():
    def make(max_tokens=600, store=None, llm=None, **kw):
        return WorkingMemory(max_tokens=max_tokens, store=store, llm=llm, **kw)

    return make


async def test_reference_touching_via_index(memory_factory):
    mem = memory_factory()
    file_id = await mem.add("file", "def retry_it():\n    '''sleep=2'''\n")
    await mem.add("log", "calling retry_it failed with sleep=2 x 100 lines")
    item = mem.items[file_id]
    assert item.refcount >= 1
    assert item.last_touched == 2  # touched by the second add's step


async def test_eviction_kills_logs_keeps_edits(memory_factory):
    mem = memory_factory(max_tokens=100)
    edit_id = await mem.add(
        "edit", "def count_attempts(retry_state):\n    return retry_state.attempt_number\n",
        pin=False,
    )
    for i in range(4):
        await mem.add("log", f"log line chunk {i} " + "noise " * 30)
    assert edit_id in mem.items
    kinds_left = {i.kind for i in mem.items.values()}
    assert "edit" in kinds_left
    assert mem.evictions > 0
    assert mem.manifest.entries, "evicted logs must be recorded"


async def test_pinned_items_survive_pressure(memory_factory):
    mem = memory_factory(max_tokens=150)
    note_id = await mem.note("KEY FACT: the retry loop lives in base.py")
    for i in range(5):
        await mem.add("tool_output", "output " + "data " * 25)
    assert note_id in mem.items
    assert mem.items[note_id].pinned


async def test_budget_is_respected_after_every_add(memory_factory):
    mem = memory_factory(max_tokens=80)
    for i in range(6):
        await mem.add("tool_output", f"chunk {i}: " + "payload " * 20)
        live = sum(i.tokens for i in mem.items.values())
        assert live <= 200 or all(i.pinned for i in mem.items.values())


async def test_assemble_packs_hottest_first(memory_factory):
    mem = memory_factory(max_tokens=120)
    hot_id = await mem.add("edit", "edit body " * 3)
    await mem.add("log", "boring log " * 20)
    ctx = await mem.assemble()
    ids_in_ctx = [b.item_id for b in ctx.blocks]
    assert hot_id in ids_in_ctx
    assert ctx.used_tokens <= ctx.budget
    rendered = ctx.render()
    assert "[edit ·" in rendered


def _w(n: int) -> str:
    """Text of exactly n ASCII word-tokens."""
    return " ".join(["tok"] * n)


async def test_manifest_and_recall_roundtrip_with_store(memory_factory):
    store = SqliteStore()
    llm = MockLLM(embed_dim=16)
    mem = memory_factory(max_tokens=100, store=store, llm=llm)

    a_id = await mem.add("log", _w(60))          # fits alone
    b_id = await mem.add("tool_output", _w(35))  # 95/100
    c_id = await mem.add("log", _w(20))          # pressure: 115 -> evict
    assert a_id not in mem.items                 # oldest log died first
    assert len(mem.manifest.entries) == 1

    # exact same text -> identical mock vector -> guaranteed top hit
    new_ids = await mem.recall(_w(60))
    assert len(new_ids) == 1
    restored = mem.items[new_ids[0]]
    assert restored.kind == "recalled"
    assert restored.text == _w(60)
    assert not any(e.item_id == a_id for e in mem.manifest.entries)
    assert b_id in mem.items                     # warm set intact


async def test_recall_offline_keyword_fallback(memory_factory):
    mem = memory_factory(max_tokens=50)
    await mem.add("log", "trace of doom " + "x " * 100)
    new_ids = await mem.recall("doom")
    assert new_ids
    assert any("doom" in mem.items[n].text.lower() for n in new_ids)


async def test_set_goal_enables_semantic_term(memory_factory):
    llm = MockLLM(embed_dim=8)
    mem = memory_factory(llm=llm)
    await mem.set_goal("implement retry backoff helper")
    assert mem.goal.ready
    assert mem.goal.vector is not None


async def test_manual_forget_goes_to_cold_zone(memory_factory):
    store = InMemStore()
    mem = memory_factory(store=store)
    item_id = await mem.add("file", "some source file content")
    assert await mem.forget(item_id)
    assert item_id not in mem.items
    assert store.count() == 1


# ── edge paths ───────────────────────────────────────────────────


async def test_pin_unpin_touch_forget_on_unknown_ids(memory_factory):
    mem = memory_factory()
    assert mem.pin("nope") is False
    assert mem.unpin("nope") is False
    assert mem.touch("nope") is False
    assert await mem.forget("nope") is False


async def test_touch_bumps_last_touched(memory_factory):
    mem = memory_factory()
    item_id = await mem.add("file", "def alpha(): pass")
    before = mem.items[item_id].last_touched
    assert mem.touch(item_id) is True
    assert mem.items[item_id].last_touched > before


async def test_unpin_allows_eviction_again(memory_factory):
    mem = memory_factory(max_tokens=40)
    protected = await mem.add("log", _w(30), pin=True)
    await mem.add("log", _w(10))     # 40/40, pinned item untouchable
    await mem.add("log", _w(5))      # pressure -> freshest small log evicts peer
    assert protected in mem.items    # still guarded by the pin
    mem.unpin(protected)
    await mem.add("log", _w(15))     # now the ex-pinned item can die
    assert protected not in mem.items
    assert any(e.item_id == protected for e in mem.manifest.entries)


def test_custom_kind_gets_default_weight():
    from protoprompt.agent.types import MemoryItem

    exotic = MemoryItem(kind="vibes", text="x", step=1)
    assert exotic.kind_weight == 1.0


def test_scorer_zero_vectors_are_safe():
    scorer = MemoryScorer()
    item = _item(kind="file", step=1)
    item.vector = [0.0, 0.0]
    score = scorer.score(item, now=2, goal_vector=[0.0, 0.0])
    assert score >= 0.0                    # no NaN/crash on degenerate input


async def test_render_includes_cold_manifest_section(memory_factory):
    mem = memory_factory(max_tokens=30)
    await mem.add("log", _w(25))           # alone it fits...
    await mem.add("note", _w(10), pin=True)  # ...pressure evicts the log
    ctx = await mem.assemble()
    rendered = ctx.render()
    assert "холодильник" in rendered
    assert any("[log]" in line for line in ctx.manifest_lines)


async def test_reference_index_ignores_self_and_forgets(memory_factory):
    mem = memory_factory()
    # a single item mentioning its own definition must not self-touch
    item_id = await mem.add("file", "def solo_fn(): pass  # solo_fn")
    item = mem.items[item_id]
    assert item.refcount == 0
    assert await mem.forget(item_id)
    # index no longer routes mentions anywhere
    await mem.add("log", "calling solo_fn now")
    assert all(i.refcount == 0 for i in mem.items.values())


# ── observability trace ──────────────────────────────────────────


async def test_trace_emits_add_reference_evict_recall(memory_factory, monkeypatch):
    from protoprompt.agent.types import KIND_WEIGHTS

    store = SqliteStore()
    llm = MockLLM(embed_dim=8)
    events: list[tuple[str, dict]] = []
    mem = memory_factory(max_tokens=80, store=store, llm=llm,
                         weights=ScorerWeights(ref_half_life=1))
    mem._trace = lambda kind, data: events.append((kind, data))

    # восстановленные копии важнее живых файлов: иначе повторное
    # упоминание count_attempts из копии снова бустит источник,
    # и возврат гарантированно вытесняется сам (см. recall_churned)
    monkeypatch.setitem(KIND_WEIGHTS, "recalled", 3.0)

    a_id = await mem.add("file", "def count_attempts(rs):\n    return 0\n")
    b_id = await mem.add("log", "count_attempts called with rs=1 " + "y " * 70)
    assert b_id not in mem.items              # вытеснен под давлением

    restored = await mem.recall("count_attempts")
    kinds = [k for k, _ in events]
    assert "add" in kinds and "reference" in kinds and "evict" in kinds
    ref = next(d for k, d in events if k == "reference")
    assert ref["target_id"] == a_id
    assert "count_attempts" in ref["names"]
    ev = next(d for k, d in events if k == "evict")
    assert ev["terms"] and "total" in ev["terms"]
    rc = next((d for k, d in events if k == "recall"), None)
    assert rc is not None
    # восстановленный элемент пережил вытеснение соседей
    r = mem.items[restored[0]]
    assert r.recall_count == 1
    # explain() receipts agree with score()
    terms = mem.scorer.explain(r, now=mem.step, goal_vector=None)
    assert abs(terms["total"] - sum(v for k2, v in terms.items() if k2 != "total")) < 1e-9


def test_explain_terms_sum_to_score():
    scorer = MemoryScorer()
    item = _item(kind="edit", tokens=90, refcount=2, step=3)
    terms = scorer.explain(item, now=7, goal_vector=None)
    total = sum(v for name, v in terms.items() if name != "total")
    assert abs(terms["total"] - total) < 1e-9
