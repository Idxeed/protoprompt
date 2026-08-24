"""Long-task safety: note dedup, pin caps, ref decay, recall cooldown,
provenance through cold storage, hot-set export/import."""

from __future__ import annotations

import pytest

from protoprompt.agent import WorkingMemory
from protoprompt.agent.scorer import MemoryScorer, ScorerWeights
from protoprompt import InMemStore, SqliteStore

from _mocks import MockLLM


def _w(n: int) -> str:
    return " ".join(["tok"] * n)


@pytest.fixture
def factory():
    def make(**kw):
        kw.setdefault("llm", MockLLM(embed_dim=16))
        return WorkingMemory(max_tokens=400, **kw)

    return make


# ── dedup ────────────────────────────────────────────────────────


async def test_identical_note_merges_not_added(factory):
    mem = factory()
    first = await mem.note("retry_it определён в tenacity/core.py строка 42")
    n_before = len(mem.items)
    second = await mem.note("retry_it определён в tenacity/core.py строка 42")
    assert first == second, "duplicate must merge into the original"
    assert len(mem.items) == n_before
    assert mem.items[first].last_touched == mem.step


class PrefixLLM:
    """Эмбеддер по первым 8 символам: одинаковый префикс -> cos=1."""

    async def chat(self, messages, model="", **options):
        return ""

    async def embed(self, texts, model=""):
        out = []
        for t in texts:
            seed = (t[:8] + "        ")[:8]
            out.append([(ord(ch) % 32) / 32 for ch in seed])
        return out


async def test_longer_duplicate_replaces_text():
    mem = WorkingMemory(llm=PrefixLLM(), max_tokens=400)
    short = await mem.note("короткая заметка про retry")
    long_text = "короткая заметка про retry, но теперь с важными деталями и цифрами"
    merged = await mem.note(long_text)
    assert merged == short
    assert mem.items[short].text == long_text
    assert mem.items[short].tokens > 10


async def test_different_note_passes_through(factory):
    class OneHotLLM:
        """Разные строки -> ортогональные векторы, одинаковые -> cos=1."""

        def __init__(self, dim: int = 64):
            self.dim = dim
            self.map: dict[str, list[float]] = {}

        async def chat(self, messages, model="", **options):
            return ""

        async def embed(self, texts, model=""):
            out = []
            for t in texts:
                if t not in self.map:
                    v = [0.0] * self.dim
                    v[len(self.map) % self.dim] = 1.0
                    self.map[t] = v
                out.append(list(self.map[t]))
            return out

    mem = WorkingMemory(store=None, llm=OneHotLLM(), max_tokens=400)
    a = await mem.note("заметка про кэш эмбеддингов и LRU выселение")
    b = await mem.note("совсем другая тема: асинхронный стор и потоки")
    assert a != b
    assert len(mem.items) == 2


async def test_dedup_only_touches_notes(factory):
    mem = factory()
    fid = await mem.add("file", "заметка про кэш эмбеддингов и LRU выселение")
    nid = await mem.note("заметка про кэш эмбеддингов и LRU выселение")
    assert fid != nid          # file with same text is NOT a duplicate
    assert len(mem.items) == 2


# ── pin cap ──────────────────────────────────────────────────────


async def test_pin_cap_unpins_oldest_first(factory):
    mem = factory(max_pinned_tokens=30)
    p1 = await mem.note("alpha " + _w(11))
    p2 = await mem.note("beta " + _w(11))
    assert all(mem.items[i].pinned for i in (p1, p2))     # 24 <= 30
    p3 = await mem.note("gamma " + _w(11))                 # 36 > 30
    assert not mem.items[p1].pinned                        # oldest released
    assert mem.items[p2].pinned and mem.items[p3].pinned


async def test_pin_cap_emits_event(factory):
    events: list[tuple[str, dict]] = []
    mem = factory(max_pinned_tokens=20)
    mem._trace = lambda k, d: events.append((k, d))
    await mem.note("first " + _w(14))
    await mem.note("second " + _w(14))                     # 30 > 20 -> unpin
    assert any(k == "unpin_auto" for k, _ in events)


async def test_without_cap_pins_are_immortal(factory):
    mem = factory()                                        # cap not set
    ids = [await mem.note(_w(10)) for _ in range(6)]
    assert all(mem.items[i].pinned for i in ids)


# ── ref decay ────────────────────────────────────────────────────


def test_ref_half_life_fades_old_links():
    item = _make_item(refcount=4, step=1, last_touched=1)
    steady = MemoryScorer(ScorerWeights(recency=0.0))
    fading = MemoryScorer(ScorerWeights(recency=0.0, ref_half_life=6))

    fresh = steady.explain(item, now=2)["refs"]
    old_steady = steady.explain(item, now=25)["refs"]
    old_fading = fading.explain(item, now=25)["refs"]

    assert fresh == old_steady                # без ручки ссылки вечны
    assert old_fading < fresh                 # с ручкой угасают
    assert old_fading == pytest.approx(fresh * 0.5 ** (24 / 6), rel=1e-6)


def _make_item(**kw):
    from protoprompt.agent.types import MemoryItem

    defaults = dict(kind="file", text="t", tokens=50)
    defaults.update(kw)
    return MemoryItem(**defaults)


# ── cooldown + provenance ────────────────────────────────────────


async def test_recall_cooldown_blocks_fresh_evictees():
    llm = MockLLM(embed_dim=8)
    store = SqliteStore()
    mem = WorkingMemory(store=store, llm=llm, max_tokens=40,
                        recall_cooldown_steps=100, namespace="cd")
    anchor = await mem.add("file", _w(4))
    junk = await mem.add("log", _w(30))               # 34/40: спокойно лежит
    await mem.forget(junk)                            # холод @шаг 2
    assert junk not in mem.items
    assert await mem.recall(_w(30)) == []             # ещё карантин (100 шагов)

    mem._step += 200                                  # карантин истёк
    restored = await mem.recall(_w(30))
    assert len(restored) == 1                         # 4+30=34 <= 40: влезает
    assert anchor in mem.items                        # якорь не пострадал


async def test_provenance_survives_cold_roundtrip():
    llm = MockLLM(embed_dim=8)
    store = SqliteStore()
    mem = WorkingMemory(store=store, llm=llm, max_tokens=80,
                        namespace="pv")
    anchor = await mem.add(
        "file", "def target_fn(): pass\n" + _w(5),
        summary="defines target_fn",
    )
    filler = await mem.add(
        "log", "mentions target_fn here\n" + _w(40),
        summary="упоминание target_fn",
    )
    await mem.forget(filler)                          # ensure it is cold

    restored_ids = await mem.recall("target_fn defines")
    assert restored_ids
    r = mem.items[restored_ids[0]]
    assert r.recall_count == 1                        # provenance tracked
    assert r.text.startswith("mentions target_fn")


async def test_recall_count_increases_across_cycles():
    llm = MockLLM(embed_dim=8)
    store = SqliteStore()
    mem = WorkingMemory(store=store, llm=llm, max_tokens=44,
                        recall_cooldown_steps=0, namespace="cyc")
    j = await mem.add("log", _w(40))
    await mem.add("tool_output", _w(2))               # 42/44
    await mem.forget(j)                               # детерминированно в холод

    first = await mem.recall(_w(40))                  # 2+40=42 fits
    r1 = mem.items[first[0]]
    assert r1.recall_count == 1

    await mem.forget(first[0])                        # обратно в холод
    second = await mem.recall(_w(40))
    r2 = mem.items[second[0]]
    assert r2.recall_count == 2                       # история возвратов цела


# ── export / import ──────────────────────────────────────────────


async def test_export_import_roundtrip(factory):
    mem = factory()
    await mem.set_goal("починить падающий тест retry")
    e1 = await mem.add("edit", "def fix(): return True", summary="фикс")
    n1 = await mem.note("важная заметка о структуре", pin=True)
    f1 = await mem.add("file", "# module\ndef helper(): pass")

    state = mem.export_state()

    fresh = WorkingMemory(store=None, llm=None, max_tokens=400,
                          namespace="restored")
    fresh.import_state(state)

    assert set(fresh.items) == {e1, n1, f1}
    assert fresh.items[n1].pinned
    assert fresh.items[e1].kind == "edit"
    assert fresh.goal.ready and fresh.goal.text.startswith("починить")
    assert fresh.step == mem.step


async def test_import_rebuilds_reference_index(factory):
    mem = factory()
    file_id = await mem.add("file", "def alpha_fn(): pass")
    await mem.add("log", "alpha_fn was called")
    state = mem.export_state()

    fresh = WorkingMemory(store=None, llm=None, max_tokens=400)
    fresh.import_state(state)
    assert fresh.items[file_id].refcount == 1         # links survived restart


async def test_export_is_json_serializable(factory):
    import json

    mem = factory()
    await mem.set_goal("цель")
    await mem.note("заметка", pin=True)
    await mem.add("file", "код")
    blob = json.dumps(mem.export_state(), ensure_ascii=False)
    revived = WorkingMemory(store=None, llm=None, max_tokens=400)
    revived.import_state(json.loads(blob))
    assert len(revived.items) == len(mem.items)


# ── cooldown vs important / semantic bypass ─────────────────────


class FixedSimLLM:
    """Эмбеддер с управляемой похожестью: query даёт заданный cos."""

    def __init__(self, sim: float, dim: int = 16):
        self.sim = sim
        self.dim = dim
        self.query_vectors: list[list[float]] = []

    async def chat(self, messages, model="", **options):
        return ""

    async def embed(self, texts, model=""):
        out = []
        for t in texts:
            if t.startswith("@query@"):
                base = [0.0] * self.dim
                base[0] = 1.0
                # косинус к базовому вектору хранилища = self.sim
                v = [0.0] * self.dim
                v[0] = self.sim
                v[1] = (1 - self.sim ** 2) ** 0.5
                self.query_vectors.append(v)
                out.append(v)
            else:
                out.append([1.0] + [0.0] * (self.dim - 1))
        return out


async def test_important_item_ignores_cooldown():
    llm = MockLLM(embed_dim=8)
    store = SqliteStore()
    mem = WorkingMemory(store=store, llm=llm, max_tokens=40,
                        recall_cooldown_steps=100, namespace="imp")
    anchor = await mem.add("file", _w(4))
    edit = await mem.add("edit", _w(30))          # важный вид
    await mem.forget(edit)                        # выгнан только что
    restored = await mem.recall(_w(30))
    assert len(restored) == 1                     # карантин обойдён
    assert mem.items[restored[0]].kind == "recalled"
    assert mem.items[anchor].pinned is False


async def test_junk_still_quarantined():
    llm = MockLLM(embed_dim=8)
    store = SqliteStore()
    mem = WorkingMemory(store=store, llm=llm, max_tokens=40,
                        recall_cooldown_steps=100, namespace="junk")
    anchor = await mem.add("file", _w(4))
    junk = await mem.add("log", _w(30))
    await mem.forget(junk)
    assert await mem.recall(_w(30)) == []         # мусор ждёт карантин


async def test_strong_similarity_bypasses_cooldown():
    llm_sim = FixedSimLLM(sim=0.97)               # запрос почти совпадает
    store = SqliteStore()
    mem = WorkingMemory(store=store, llm=FixedSimLLM(sim=0.0),
                        max_tokens=40, recall_cooldown_steps=100,
                        recall_bypass_sim=0.9, namespace="byp")
    anchor = await mem.add("file", _w(4))
    junk = await mem.add("log", _w(30))
    await mem.forget(junk)

    # подменяем embedder на тот, что даёт сильную похожесть запросу
    mem._llm = llm_sim
    mem.goal._llm = llm_sim
    restored = await mem.recall("@query@ точечный вопрос")
    assert len(restored) == 1                     # сильный сигнал прошёл
