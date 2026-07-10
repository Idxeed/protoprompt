"""protoprompt: layered context builder for LLM prompts.

Public top-level exports live here; per-layer subpackages re-export
their own public API as well, so both styles work:

    from protoprompt import ContextBuilder
    from protoprompt.context import ContextInput
"""

from protoprompt.context import ContextInput, ContextOutput
from protoprompt.exceptions import TokenBudgetExceededError
from protoprompt.injector import ContextBuilder
from protoprompt.injector_budgeted import (
    DEFAULT_PRIORITIES,
    BudgetReport,
    TokenBudgetedContextBuilder,
)
from protoprompt.llm import LLMClientProtocol
from protoprompt.pipeline import Pipeline
from protoprompt.profile.builder import ProfileBuilder
from protoprompt.profile.types import UserProfile
from protoprompt.session.compressor import Compressor
from protoprompt.session.strategy import (
    HeuristicStrategy,
    LLMSummaryStrategy,
    StrategyProtocol,
)
from protoprompt.session.types import CompressedBlock, Session
from protoprompt.store.memory import InMemStore
from protoprompt.store.protocol import StoreProtocol
from protoprompt.tokens.protocol import TokenCounter
from protoprompt.tokens.regex_counter import RegexTokenCounter

__version__ = "0.1.0"

__all__ = [
    "ContextBuilder",
    "TokenBudgetedContextBuilder",
    "ContextInput",
    "ContextOutput",
    "BudgetReport",
    "DEFAULT_PRIORITIES",
    "TokenBudgetExceededError",
    "Pipeline",
    "Compressor",
    "StrategyProtocol",
    "HeuristicStrategy",
    "LLMSummaryStrategy",
    "Session",
    "CompressedBlock",
    "UserProfile",
    "ProfileBuilder",
    "StoreProtocol",
    "InMemStore",
    "TokenCounter",
    "RegexTokenCounter",
    "LLMClientProtocol",
]
