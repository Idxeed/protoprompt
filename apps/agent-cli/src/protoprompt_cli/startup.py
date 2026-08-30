"""Интерактивный launcher для запуска pp-agent без пути проекта."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from protoprompt_cli.persistence import safe_project_directory
from protoprompt_cli.terminal import escape_terminal_text


def choose_project(
    current: str | Path,
    *,
    readline: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> Path | None:
    """Выбрать каталог проекта в простом TTY-совместимом меню."""
    # The launcher is displayed before the REPL owns its normal output
    # boundary.  Project paths are filesystem data and may contain terminal
    # controls, so do not let this small preflight menu become an ANSI/OSC
    # bypass before a later permission prompt.
    safe_write = lambda line: write(escape_terminal_text(line))
    safe_readline = lambda prompt: readline(escape_terminal_text(prompt))
    try:
        cwd = safe_project_directory(current)
    except OSError:
        safe_write("Текущий каталог недоступен или содержит небезопасный reparse point.")
        return None
    safe_write("")
    safe_write("pp-agent")
    safe_write("────────────────────────────────────────")
    safe_write(f"Текущий каталог: {cwd}")
    safe_write("[1] Открыть текущий каталог")
    safe_write("[2] Указать другой каталог")
    safe_write("[q] Выйти")
    try:
        choice = safe_readline("Выбор [1]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice in ("", "1"):
        return cwd
    if choice in ("q", "quit", "выход"):
        return None
    if choice != "2":
        safe_write("Неизвестный выбор, открываю текущий каталог.")
        return cwd
    try:
        raw_path = safe_readline("Путь к проекту: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw_path:
        return cwd
    try:
        selected = safe_project_directory(Path(raw_path).expanduser())
    except OSError:
        safe_write(f"Каталог недоступен или небезопасен: {raw_path}")
        return None
    return selected
