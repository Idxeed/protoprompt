"""Персистентность: корень проекта, .protoprompt/, namespace.

Холодная зона живёт в ``SqliteStore`` (``agent.db``), горячий набор —
в ``state.json`` через ``export_state/import_state``. Два проекта
изолируются namespace = хэш корня.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

STATE_DIR = ".protoprompt"
COLD_DB = "agent.db"
STATE_JSON = "state.json"
PERMS_JSON = "perms.json"
SESSION_DIR = "sessions"
DEFAULT_SESSION = "default"


def find_root(start: str | Path | None = None) -> Path:
    """Ближайший git-root; фолбэк — сам каталог."""
    cur = Path(start or Path.cwd()).resolve()
    if cur.is_file():
        cur = cur.parent
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists():
            return candidate
    return cur


def namespace_for(root: str | Path) -> str:
    """sha256(корень)[:12] — ключ холодной зоны проекта."""
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()
    return digest[:12]


def state_dir(root: str | Path) -> Path:
    return Path(root) / STATE_DIR


def ensure_state_dir(root: str | Path) -> Path:
    directory = state_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def cold_db_path(root: str | Path) -> Path:
    return state_dir(root) / COLD_DB


def state_json_path(root: str | Path) -> Path:
    return state_dir(root) / STATE_JSON


def perms_json_path(root: str | Path) -> Path:
    return state_dir(root) / PERMS_JSON


def load_json(path: str | Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: str | Path, data: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_state(mem, root: str | Path) -> None:
    ensure_state_dir(root)
    save_json(state_json_path(root), mem.export_state())


def load_state(mem, root: str | Path) -> bool:
    """Восстановить горячий набор в ``mem``. True, если состояние было."""
    data = load_json(state_json_path(root))
    return _import_state_safely(mem, data)


# ── сессии ───────────────────────────────────────────────────────


def _sanitize_session(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return cleaned.strip("._") or DEFAULT_SESSION


def session_dir(root: str | Path) -> Path:
    return state_dir(root) / SESSION_DIR


def session_file(root: str | Path, name: str) -> Path:
    return session_dir(root) / f"{_sanitize_session(name)}.json"


def session_exists(root: str | Path, name: str) -> bool:
    return session_file(root, name).is_file()


def save_session(mem, root: str | Path, name: str = DEFAULT_SESSION) -> None:
    ensure_state_dir(root)
    save_json(session_file(root, name), mem.export_state())


def load_session(mem, root: str | Path, name: str = DEFAULT_SESSION) -> bool:
    data = load_json(session_file(root, name))
    return _import_state_safely(mem, data)


def _import_state_safely(mem, data: Any) -> bool:
    """Import a persisted snapshot without corrupting the active session.

    Session files are ordinary local JSON and can be cut off during a crash
    or hand-edited.  ``WorkingMemory.import_state`` clears its current state
    before iterating entries, so keep a valid rollback snapshot if a
    structurally malformed (but valid JSON) payload raises halfway through.
    """
    if not isinstance(data, dict) or not data:
        return False
    before = mem.export_state()
    try:
        mem.import_state(data)
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
        mem.import_state(before)
        return False
    return True


def list_sessions(root: str | Path) -> list[dict]:
    """Метаданные всех сессий проекта, свежие первыми."""
    directory = session_dir(root)
    entries = []
    if not directory.is_dir():
        return entries
    for path in sorted(directory.glob("*.json")):
        data = load_json(path, {}) or {}
        entries.append({
            "name": path.stem,
            "updated_at": path.stat().st_mtime,
            "items": len(data.get("items", [])),
            "goal": (data.get("goal_text") or "")[:60],
        })
    entries.sort(key=lambda e: e["updated_at"], reverse=True)
    return entries


def latest_session(root: str | Path) -> str | None:
    """Имя самой свежей сохранённой сессии."""
    sessions = list_sessions(root)
    return sessions[0]["name"] if sessions else None
