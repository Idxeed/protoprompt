"""ToolRunner: исполнение инструментов агента + модель прав.

Инструменты: bash, read, write, edit, glob, grep. Все пути заперты
в корень проекта (jail, по умолчанию включён). Права:
``ask`` (спросить через колбэк) / ``allow`` / ``deny``.
"""

from __future__ import annotations

import inspect
import asyncio
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

TOOLS = ("bash", "read", "write", "edit", "glob", "grep")

PERM_ASK = "ask"
PERM_ALLOW = "allow"
PERM_DENY = "deny"

DEFAULT_PERMS: dict[str, str] = {
    "read": PERM_ALLOW,
    "glob": PERM_ALLOW,
    "grep": PERM_ALLOW,
    "bash": PERM_ASK,
    "write": PERM_ASK,
    "edit": PERM_ASK,
}

MAX_OUTPUT = 8000
BASH_TIMEOUT = 120


class PermissionDenied(RuntimeError):
    """Право на инструмент не выдано."""


class OutOfProject(RuntimeError):
    """Путь за пределами корня проекта."""


@dataclass
class ToolResult:
    """Результат одного вызова инструмента."""

    ok: bool
    output: str
    tool: str = ""
    error: str = ""


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(обрезано, всего {len(text)} символов)"


class ToolRunner:
    def __init__(
        self,
        root: str | Path,
        perms: dict[str, str] | None = None,
        *,
        jail: bool = True,
        ask_callback=None,
        timeout: float = BASH_TIMEOUT,
        max_output: int = MAX_OUTPUT,
    ) -> None:
        self.root = Path(root).resolve()
        merged = dict(DEFAULT_PERMS)
        if perms:
            merged.update(perms)
        self.perms = merged
        self.jail = jail
        self.ask_callback = ask_callback
        self.timeout = timeout
        self.max_output = max_output

    def _resolve(self, path: str | Path) -> Path:
        p = Path(str(path))
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        if self.jail and not (p == self.root or self.root in p.parents):
            raise OutOfProject(f"path outside project root: {path}")
        return p

    async def _check_permission(self, action) -> str:
        mode = self.perms.get(action.name, PERM_ASK)
        if mode == PERM_ALLOW:
            return PERM_ALLOW
        if mode == PERM_DENY:
            return PERM_DENY
        if self.ask_callback is None:
            return PERM_DENY
        granted = self.ask_callback(action)
        if inspect.isawaitable(granted):
            granted = await granted
        return PERM_ALLOW if granted else PERM_DENY

    async def run(self, action) -> ToolResult:
        handler = getattr(self, f"_tool_{action.name}", None)
        if handler is None:
            return ToolResult(False, "", error=f"unknown tool: {action.name}")
        decision = await self._check_permission(action)
        if decision == PERM_DENY:
            return ToolResult(
                False, "", error=f"permission denied: {action.name}",
            )
        try:
            output = await handler(action)
            if isinstance(output, ToolResult):
                return output
            return ToolResult(True, output, tool=action.name)
        except PermissionDenied as exc:
            return ToolResult(False, "", error=str(exc))
        except OutOfProject as exc:
            return ToolResult(False, "", error=str(exc))
        except Exception as exc:
            return ToolResult(False, "", error=f"{type(exc).__name__}: {exc}")

    async def _tool_bash(self, action) -> str:
        cmd = action.body.strip()
        if not cmd:
            raise ValueError("empty bash command")
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            shell=True,
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=self.timeout,
            errors="replace",
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        status = f"exit={proc.returncode}"
        output = _clip(f"$ {cmd}\n{status}\n{combined}", self.max_output)
        return ToolResult(
            ok=proc.returncode == 0,
            output=output,
            tool=action.name,
            error=output if proc.returncode != 0 else "",
        )

    async def _tool_read(self, action) -> str:
        path = action.kwargs.get("path", action.body.strip())
        if not path:
            raise ValueError("read requires a path")
        target = self._resolve(path)
        if not await asyncio.to_thread(target.is_file):
            raise FileNotFoundError(f"no such file: {path}")
        content = await asyncio.to_thread(
            target.read_text, encoding="utf-8", errors="replace"
        )
        return _clip(f"# {target}\n{content}", self.max_output)

    async def _tool_write(self, action) -> str:
        path = action.kwargs.get("path", "")
        if not path:
            raise ValueError("write requires a path attribute")
        target = self._resolve(path)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, action.body, encoding="utf-8")
        return f"wrote {target} ({len(action.body)} chars)"

    async def _tool_edit(self, action) -> str:
        path = action.kwargs.get("path", "")
        old = action.kwargs.get("old", "")
        new = action.kwargs.get("new", "")
        if not path:
            raise ValueError("edit requires a path attribute")
        if not old:
            raise ValueError("edit requires an old attribute")
        target = self._resolve(path)
        if not await asyncio.to_thread(target.is_file):
            raise FileNotFoundError(f"no such file: {path}")
        content = await asyncio.to_thread(
            target.read_text, encoding="utf-8", errors="replace"
        )
        if old not in content:
            message = f"edit failed: pattern not found in {target}"
            return ToolResult(False, message, tool=action.name, error=message)
        updated = content.replace(old, new, 1)
        await asyncio.to_thread(target.write_text, updated, encoding="utf-8")
        return f"edited {target} (1 replacement)"

    async def _tool_glob(self, action) -> str:
        pattern = action.kwargs.get("pattern", action.body.strip())
        if not pattern:
            raise ValueError("glob requires a pattern")
        matches = await asyncio.to_thread(
            lambda: sorted(
                str(p.relative_to(self.root)).replace("\\", "/")
                for p in self.root.glob(pattern)
            )
        )
        return "\n".join(matches) if matches else f"no matches for {pattern!r}"

    async def _tool_grep(self, action) -> str:
        pattern = action.kwargs.get("pattern", "")
        sub = action.kwargs.get("path", "")
        if not pattern:
            raise ValueError("grep requires a pattern")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"bad regex: {exc}") from exc
        base = self._resolve(sub) if sub else self.root
        def search() -> str:
            lines = []
            for file in base.rglob("*"):
                if not file.is_file():
                    continue
                try:
                    text = file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if regex.search(line):
                        rel = str(file.relative_to(self.root)).replace("\\", "/")
                        lines.append(f"{rel}:{lineno}: {line.strip()}")
            return _clip("\n".join(lines) if lines else f"no matches for {pattern!r}",
                         self.max_output)

        return await asyncio.to_thread(search)
