"""Cheap importance signals: identifier extraction and a reference index.

Extraction tries a real AST first (precise for valid Python) and falls
back to regex for non-code or truncated snippets. The reference index
counts later mentions of earlier definitions - GC by reference counting,
no LLM calls.
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoprompt.agent.types import MemoryItem

_WORD_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_DEF_RE = re.compile(
    r"^[ \t]*(?:async[ \t]+)?def[ \t]+(\w+)"
    r"|^[ \t]*class[ \t]+(\w+)",
    re.MULTILINE,
)
_ASSIGN_RE = re.compile(r"^[ \t]*([A-Za-z_]\w*)[ \t]*(?::[^=\n]+)?=[^=]",
                        re.MULTILINE)

_STOPWORDS = frozenset("""
    False None True and as assert async await break class continue def del
    elif else except finally for from global if import in is lambda nonlocal
    not or pass raise return try while with self cls int str float bool list
    dict set tuple len print range enumerate zip open super isinstance type
    repr format sorted reversed min max sum abs any all map filter object
    Exception ValueError TypeError KeyError RuntimeError StopIteration
""".split())


def _clean(names) -> frozenset[str]:
    return frozenset(n for n in names if n and n not in _STOPWORDS)


def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def extract_identifiers(text: str) -> frozenset[str]:
    """All plausibly meaningful identifiers mentioned in ``text``."""
    tree = _parse(text)
    if tree is None:
        return _clean(_WORD_RE.findall(text))

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            # сайт определения тоже является упоминанием символа
            names.add(node.name)
    return _clean(names)


def extract_definitions(text: str) -> frozenset[str]:
    """Names defined here: def/class targets and module-level assigns."""
    tree = _parse(text)
    if tree is None:
        names = {m.group(1) or m.group(2) for m in _DEF_RE.finditer(text)}
        names.update(_ASSIGN_RE.findall(text))
        return _clean(names)

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                names.add(node.target.id)
    return _clean(names)


class ReferenceIndex:
    """Maps known definition names to the items that define them."""

    def __init__(self) -> None:
        self._owners: dict[str, set[str]] = {}

    def on_add(
        self, item: "MemoryItem", items: dict
    ) -> list[tuple[str, frozenset[str]]]:
        """Register a fresh item.

        Bumps ``refcount``/``last_touched`` of every earlier item whose
        definitions are mentioned in ``item``; returns the touched ids
        paired with the names that matched (for observability).
        """
        touched: dict[str, set[str]] = {}
        for name in item.refs:
            for owner_id in self._owners.get(name, ()):
                if owner_id == item.id:
                    continue
                owner = items.get(owner_id)
                if owner is None:
                    continue
                owner.refcount += 1
                owner.last_touched = item.step
                touched.setdefault(owner_id, set()).add(name)
        self.register_defs(item.id, item.defs)
        return [(owner_id, frozenset(names))
                for owner_id, names in touched.items()]

    def register_defs(self, item_id: str, names) -> None:
        """Bulk index definitions without side effects (import path)."""
        for name in names:
            self._owners.setdefault(name, set()).add(item_id)

    def forget(self, item_id: str) -> None:
        """Drop index entries pointing at an evicted item."""
        for owners in self._owners.values():
            owners.discard(item_id)
