"""Интерактивный launcher для запуска pp-agent без пути проекта."""

from __future__ import annotations

from pathlib import Path
from typing import Callable


def choose_project(
    current: str | Path,
    *,
    readline: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> Path | None:
    """Выбрать каталог проекта в простом TTY-совместимом меню."""
    cwd = Path(current).resolve()
    write("")
    write("pp-agent")
    write("────────────────────────────────────────")
    write(f"Текущий каталог: {cwd}")
    write("[1] Открыть текущий каталог")
    write("[2] Указать другой каталог")
    write("[q] Выйти")
    try:
        choice = readline("Выбор [1]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice in ("", "1"):
        return cwd
    if choice in ("q", "quit", "выход"):
        return None
    if choice != "2":
        write("Неизвестный выбор, открываю текущий каталог.")
        return cwd
    try:
        raw_path = readline("Путь к проекту: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw_path:
        return cwd
    selected = Path(raw_path).expanduser().resolve()
    if not selected.is_dir():
        write(f"Каталог не найден: {selected}")
        return None
    return selected
