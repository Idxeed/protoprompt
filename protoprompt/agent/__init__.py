"""protoprompt.agent — working memory for autonomous coding agents.

The chat-oriented builders live at the top level; this package is the
experimental lineage for agents that run many tool-call steps on their
own and need significance-based (not recency-based) context eviction.

    from protoprompt.agent import WorkingMemory

    memory = WorkingMemory(store=SqliteStore("agent.db"), llm=my_client)
    await memory.set_goal("fix the failing test in retry.py")
    await memory.add("log", huge_test_output)      # dies young
    await memory.note("retry_it lives in retry.py:42")  # pinned
    context = await memory.assemble()              # fits the budget
"""

from protoprompt.agent.goal import GoalTracker
from protoprompt.agent.manifest import Manifest, ManifestEntry
from protoprompt.agent.references import (
    ReferenceIndex,
    extract_definitions,
    extract_identifiers,
)
from protoprompt.agent.scorer import MemoryScorer, ScorerWeights
from protoprompt.agent.types import (
    KIND_WEIGHTS,
    AssembledContext,
    ContextBlock,
    MemoryItem,
)
from protoprompt.agent.working import WorkingMemory

__all__ = [
    "WorkingMemory",
    "MemoryItem",
    "AssembledContext",
    "ContextBlock",
    "KIND_WEIGHTS",
    "MemoryScorer",
    "ScorerWeights",
    "GoalTracker",
    "Manifest",
    "ManifestEntry",
    "ReferenceIndex",
    "extract_definitions",
    "extract_identifiers",
]
