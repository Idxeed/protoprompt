"""Тесты отрисовки таблиц памяти."""

from __future__ import annotations

from protoprompt.agent import WorkingMemory

from protoprompt_cli import render


async def test_memory_table_lists_hot_items(mem_factory):
    mem = mem_factory()
    item_id = await mem.add("edit", "def fix(): pass", summary="фикс", pin=True)
    await mem.add("log", "мусорный лог")
    lines = render.memory_table(mem)
    text = "\n".join(lines)
    assert item_id in text
    assert "edit" in text
    assert "*" in text  # pin-маркер
    assert "used=" in text


async def test_memory_table_sorts_by_score(mem_factory):
    mem = mem_factory()
    await mem.add("edit", "def hot(): return 1")   # вес 3.0
    await mem.add("log", "шум " * 20)              # вес 0.5
    lines = render.memory_table(mem)
    positions = [lines.index(line) for line in lines
                 if "hot" in line or "шум" in line]
    assert positions == sorted(positions)


def test_cold_table_empty():
    mem = WorkingMemory(max_tokens=100)
    assert render.cold_table(mem) == ["(холодильник пуст)"]


async def test_cold_table_lists_entries(mem_factory):
    mem = mem_factory(max_tokens=30)
    await mem.add("log", "x " * 40)
    await mem.note("пин-заметка", pin=True)
    lines = render.cold_table(mem)
    assert any("log" in line for line in lines)


def test_terms_line_includes_total():
    terms = {"kind": 3.0, "refs": 0.0, "semantic": 0.0,
             "recency": 0.5, "size": -0.1, "total": 3.4}
    line = render.terms_line(terms)
    assert "kind +3.00" in line
    assert "=> 3.40" in line


def test_terms_line_colors_only_when_enabled():
    terms = {"kind": 1.0, "refs": 0.0, "semantic": 0.0,
             "recency": 0.0, "size": 0.0, "total": 1.0}
    plain = render.terms_line(terms, color=False)
    colored = render.terms_line(terms, color=True)
    assert "\x1b[" not in plain
    assert "\x1b[" in colored


def test_budget_bar():
    mem = WorkingMemory(max_tokens=100)
    assert render.budget_bar(mem, 100) == "." * 20
    assert len(render.budget_bar(mem, 0)) == 20


def test_format_item_includes_pin():
    from protoprompt.agent.types import MemoryItem

    item = MemoryItem(kind="note", text="важно", step=1, pinned=True)
    assert "PINNED" in render.format_item(item)


async def test_context_lines_shows_blocks_and_budget(mem_factory):
    mem = mem_factory()
    await mem.add("edit", "def hot(): pass", summary="правка")
    context = await mem.assemble()
    lines = render.context_lines(context)
    text = "\n".join(lines)
    assert "бюджет" in text
    assert "[edit" in text
    assert "правка" in text or "def hot" in text


def test_context_lines_empty_budget():
    from protoprompt.agent.types import AssembledContext

    context = AssembledContext(budget=100)
    lines = render.context_lines(context)
    assert any("0/100" in line for line in lines)