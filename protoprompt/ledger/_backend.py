"""Private nominal boundary for durable Ledger storage implementations.

``MemoryWriter`` is deliberately coupled only to this internal command
boundary, not to a concrete SQLite class.  The marker is intentionally not a
public plugin protocol: backends remain trusted host infrastructure and must
implement the complete command surface used by the writer, admission gate,
and strict recall planner.
"""

from __future__ import annotations

from abc import ABC


class _LedgerCommandBackend(ABC):
    """Nominal marker for trusted synchronous Ledger command backends."""
