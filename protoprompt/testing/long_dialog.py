"""Deterministic long-dialog retention scenario for examples and CI."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass

from protoprompt.connectivity import MemoryService
from protoprompt.scope import MemoryScope
from protoprompt.store.memory import InMemStore


@dataclass(frozen=True, slots=True)
class LongDialogResult:
    turns: int
    capacity: int
    fifo_recalled: bool
    lru_recalled: bool
    semantic_recalled: bool
    semantic_memory_id: str


class ScenarioEmbeddings:
    """Offline embeddings with an explicit, reproducible topic axis."""

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [self._one(text) for text in texts]

    @staticmethod
    def _one(text: str) -> list[float]:
        normalized = text.lower()
        if any(token in normalized for token in ("cobalt", "archive", "access code")):
            return [1.0, 0.0, 0.0]
        if "invoice" in normalized:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


async def run_long_dialog_scenario(
    *,
    turns: int = 100,
    capacity: int = 12,
) -> LongDialogResult:
    """Compare bounded FIFO/LRU retention with scoped semantic memory.

    FIFO and LRU receive the same turn identities and capacity. No old turn is
    re-accessed during ingestion, so LRU honestly evicts the early fact. The
    semantic store is then queried by meaning rather than by a known key.
    """
    if capacity < 1:
        raise ValueError("capacity must be positive")
    if turns <= capacity + 2:
        raise ValueError("turns must exceed capacity by at least three")

    fact = "My archive access code is COBALT-17."
    fact_id = "turn-2"
    messages = [
        f"Routine invoice discussion number {index}."
        for index in range(turns)
    ]
    messages[2] = fact

    fifo: deque[tuple[str, str]] = deque(maxlen=capacity)
    lru: OrderedDict[str, str] = OrderedDict()
    service = MemoryService(
        InMemStore(),
        ScenarioEmbeddings(),
        MemoryScope(tenant="benchmark", user="alice", thread="long-dialog"),
    )
    for index, message in enumerate(messages):
        memory_id = f"turn-{index}"
        fifo.append((memory_id, message))
        lru[memory_id] = message
        lru.move_to_end(memory_id)
        if len(lru) > capacity:
            lru.popitem(last=False)
        await service.remember(message, memory_id=memory_id)

    hits = await service.search("What was my archive access code?", top_k=1)
    semantic_id = hits[0]["memory_id"] if hits else ""
    return LongDialogResult(
        turns=turns,
        capacity=capacity,
        fifo_recalled=any(item_id == fact_id for item_id, _ in fifo),
        lru_recalled=fact_id in lru,
        semantic_recalled=semantic_id == fact_id,
        semantic_memory_id=semantic_id,
    )
