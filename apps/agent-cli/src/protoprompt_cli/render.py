"""Отрисовка таблиц памяти и трейса событий.

Форматирование детерминированное; ANSI-цвета включаются флагом ``color``
(в тестах выключены).
"""

from __future__ import annotations

from protoprompt.agent import WorkingMemory
from protoprompt.agent.types import MemoryItem

GREEN, YELLOW, RED, CYAN, MAGENTA, GRAY, BOLD, RST = (
    "\x1b[92m", "\x1b[93m", "\x1b[91m", "\x1b[96m",
    "\x1b[95m", "\x1b[90m", "\x1b[1m", "\x1b[0m",
)
KIND_COLOR = {
    "edit": GREEN, "note": MAGENTA, "recalled": CYAN, "file": GREEN,
    "test_result": YELLOW, "tool_output": YELLOW, "log": GRAY,
}


def _c(color: str, enabled: bool, text: str) -> str:
    return f"{color}{text}{RST}" if enabled else text


def terms_line(terms: dict, color: bool = False) -> str:
    cells = []
    for name in ("kind", "refs", "semantic", "recency", "size"):
        value = terms.get(name, 0.0)
        cells.append(f"{name} {value:+.2f}")
    total = terms.get("total", 0.0)
    col = GREEN if total >= 2.0 else YELLOW if total >= 1.0 else RED
    return " ".join(cells) + f" => {_c(col, color, f'{total:.2f}')}"


def memory_table(mem: WorkingMemory, *, color: bool = False) -> list[str]:
    """Строки таблицы горячего набора, отсортированные по score."""
    rows = []
    for item in mem.items.values():
        terms = mem.scorer.explain(item, now=mem.step, goal_vector=mem.goal.vector)
        rows.append((terms["total"], item, terms))
    rows.sort(key=lambda row: row[0], reverse=True)

    header = (
        f"{'id':<9}{'kind':<13}{'tok':>5} {'pin':>3} {'score':>7}  label"
    )
    lines = [header, "-" * 72]
    for total, item, terms in rows:
        pin = "*" if item.pinned else " "
        lines.append(
            f"{item.id:<9}{item.kind:<13}{item.tokens:>5} {pin:>3} "
            f"{_c(GREEN if total >= 2 else YELLOW if total >= 1 else RED, color, f'{total:7.2f}')}  "
            f"{item.label[:56]}"
        )
    lines.append(
        f"used={mem.used_tokens} tok · evictions={mem.evictions} · "
        f"cold={len(mem.manifest.entries)} · goal={mem.goal.text[:40] or '-'}"
    )
    lines.append(budget_bar(mem, _budget_of(mem)))
    return lines


def cold_table(mem: WorkingMemory) -> list[str]:
    """Строки холодильника из Manifest."""
    entries = mem.manifest.entries
    if not entries:
        return ["(холодильник пуст)"]
    lines = [f"{'id':<9}{'kind':<13}{'tok':>5} {'step':>5}  summary"]
    for entry in entries:
        lines.append(
            f"{entry.item_id:<9}{entry.kind:<13}{entry.tokens:>5} "
            f"{entry.evicted_at:>5}  {entry.summary[:56]}"
        )
    return lines


def budget_bar(mem: WorkingMemory, budget: int) -> str:
    ratio = min(1.0, mem.used_tokens / budget) if budget else 0.0
    fill = round(20 * ratio)
    return "#" * fill + "." * (20 - fill)


def _budget_of(mem: WorkingMemory) -> int:
    return int(getattr(mem, "_max_tokens", 0))


def context_lines(context, *, color: bool = False) -> list[str]:
    """Строки того, что реально войдёт в контекст (аналог /context)."""
    lines = [f"бюджет: {context.used_tokens}/{context.budget} токенов"]
    for block in context.blocks:
        header = f"[{block.kind} · {block.item_id} · {block.score:+.2f}]"
        lines.append(f"{header}\n{block.text[:72]}")
    if context.skipped_ids:
        lines.append(f"(не влезло: {len(context.skipped_ids)} элементов)")
    if context.manifest_lines:
        lines.append("(холодная зона доступна через /recall)")
    return lines


def format_item(item: MemoryItem) -> str:
    pin = " PINNED" if item.pinned else ""
    return f"{item.id} [{item.kind}] {item.label}{pin}"