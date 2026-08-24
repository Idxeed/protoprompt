"""Manifest of evicted items: the cheap index of the cold zone.

Eviction is reversible, so nothing is truly deleted — the manifest lets
the agent (and the demo log) see *what* is out there without paying for
retrieval until it is actually needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from protoprompt.agent.types import Kind


@dataclass
class ManifestEntry:
    item_id: str
    kind: Kind
    summary: str
    tokens: int
    evicted_at: int
    symbols: frozenset[str] = frozenset()
    lineage: str = ""
    important: bool = False

    def line(self) -> str:
        return f"{self.item_id} [{self.kind}] {self.summary} ({self.tokens} tok)"


@dataclass
class Manifest:
    entries: list[ManifestEntry] = field(default_factory=list)

    def record(
        self,
        item_id: str,
        kind: Kind,
        summary: str,
        tokens: int,
        evicted_at: int,
        symbols: frozenset[str] = frozenset(),
        lineage: str = "",
        important: bool = False,
    ) -> ManifestEntry:
        entry = ManifestEntry(item_id, kind, summary, tokens, evicted_at,
                              symbols=symbols, lineage=lineage,
                              important=important)
        self.entries.append(entry)
        return entry

    def restore(self, item_id: str) -> ManifestEntry | None:
        """Remove and return the entry when an item is recalled."""
        for i, entry in enumerate(self.entries):
            if entry.item_id == item_id:
                return self.entries.pop(i)
        return None

    def search(self, needle: str) -> list[ManifestEntry]:
        """Keyword fallback when no embeddings are available."""
        lowered = needle.lower()
        return [
            e for e in self.entries
            if lowered in e.summary.lower() or lowered in e.kind.lower()
        ]

    def by_symbols(self, names) -> list[ManifestEntry]:
        """Entries whose cold content defines/mentions any of ``names``,
        best (most symbol overlaps) first. Deterministic symbol channel.
        """
        wanted = set(names)
        if not wanted:
            return []
        scored: list[tuple[int, ManifestEntry]] = []
        for e in self.entries:
            overlap = len(wanted & e.symbols)
            if overlap:
                scored.append((overlap, e))
        scored.sort(key=lambda pair: (-pair[0], pair[1].evicted_at))
        return [e for _, e in scored]

    def lines(self) -> list[str]:
        return [e.line() for e in self.entries]
