"""Data structures for autonomous-agent working memory."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

Kind = str

#: Base value of an item by what it *is*: an edit the agent made is worth
#: more than a raw log line. Multiplied into the final score.
KIND_WEIGHTS: dict[str, float] = {
    "edit": 3.0,
    "note": 2.5,
    "recalled": 2.0,
    "file": 1.5,
    "test_result": 1.0,
    "tool_output": 0.8,
    "log": 0.5,
}
DEFAULT_KIND_WEIGHT = 1.0

_item_seq = itertools.count(1)


def new_item_id() -> str:
    return f"m{next(_item_seq):06d}"


@dataclass
class MemoryItem:
    """One piece of the agent's working memory.

    ``refs``  — identifiers merely mentioned in the text;
    ``defs``  — identifiers defined here (def/class/module-level assign);
    ``refcount`` — how many later items mentioned something defined here;
    ``last_touched`` — step of the most recent such mention;
    ``vector`` — lazily filled embedding for the semantic term.
    """

    kind: Kind
    text: str
    step: int
    id: str = field(default_factory=new_item_id)
    tokens: int = 0
    refs: frozenset[str] = frozenset()
    defs: frozenset[str] = frozenset()
    pinned: bool = False
    summary: str = ""
    refcount: int = 0
    last_touched: int = -1
    recall_count: int = 0
    #: identity of the cold-zone document family; recalled items inherit
    #: it so re-eviction REPLACES their old cold copy instead of duplicating
    lineage: str = ""
    vector: list[float] | None = None

    @property
    def kind_weight(self) -> float:
        return KIND_WEIGHTS.get(self.kind, DEFAULT_KIND_WEIGHT)

    @property
    def label(self) -> str:
        base = self.summary.strip() or self.text.strip().splitlines()[0]
        return f"{base[:72]}…"


@dataclass
class ContextBlock:
    item_id: str
    kind: Kind
    text: str
    score: float


@dataclass
class AssembledContext:
    """Result of one ``WorkingMemory.assemble`` call."""

    blocks: list[ContextBlock] = field(default_factory=list)
    used_tokens: int = 0
    budget: int = 0
    skipped_ids: list[str] = field(default_factory=list)
    manifest_lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts: list[str] = []
        for block in self.blocks:
            header = f"[{block.kind} · {block.item_id}]"
            parts.append(f"{header}\n{block.text}")
        if self.manifest_lines:
            parts.append(
                "[холодильник — доступно через recall]\n"
                + "\n".join(self.manifest_lines)
            )
        return "\n\n".join(parts)
