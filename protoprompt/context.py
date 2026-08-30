"""Public dataclasses for context assembly."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoprompt.injector_budgeted import BudgetReport
    from protoprompt.context_plan import ContextPlan
    from protoprompt.profile.types import UserProfile
    from protoprompt.rag.types import RetrievedChunk


@dataclass
class ContextInput:
    query: str
    chat_id: str = ""
    system_prompt: str = ""
    doc_ids: list[int | str] | None = None
    score_threshold: float | None = None
    embedding_model: str = "nomic-embed-text"
    top_k_rag: int = 5
    top_k_session: int = 3
    include_rag: bool = True
    include_session: bool = True
    include_profile: bool = False
    profile_text: str = ""
    profile: "UserProfile | None" = None
    language: str = "ru"


@dataclass
class ContextOutput:
    system_prompt: str
    rag_blocks: list[str] = field(default_factory=list)
    session_blocks: list[str] = field(default_factory=list)
    profile_used: bool = False
    budget_report: "BudgetReport | None" = None
    # Appended to preserve the positional constructor used before v0.3.
    rag_chunks: "list[RetrievedChunk]" = field(default_factory=list)
    # Appended to preserve existing positional constructors.  The plan is
    # metadata for this one build, not a replacement payload or equality
    # input: it contains a unique request trace.
    plan: "ContextPlan | None" = field(default=None, compare=False, repr=False)
