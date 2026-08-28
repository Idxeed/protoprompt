"""Reproducible FIFO/LRU versus semantic-memory comparison."""

from __future__ import annotations

import asyncio

from protoprompt.testing import run_long_dialog_scenario


async def main() -> None:
    result = await run_long_dialog_scenario(turns=100, capacity=12)
    print(f"dialog turns: {result.turns}; bounded capacity: {result.capacity}")
    print("FIFO recalled early fact:", result.fifo_recalled)
    print("LRU recalled early fact:", result.lru_recalled)
    print("protoprompt semantic recall:", result.semantic_recalled)
    print("recalled memory id:", result.semantic_memory_id)


if __name__ == "__main__":
    asyncio.run(main())
