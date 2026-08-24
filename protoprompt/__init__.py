"""protoprompt: layered context builder for LLM prompts.

Public top-level exports live here; per-layer subpackages re-export
their own public API as well, so both styles work:

    from protoprompt import ContextBuilder
    from protoprompt.context import ContextInput
"""

from protoprompt.cache import (
    CachedLLMClient,
    EmbeddingCache,
    InMemoryEmbeddingCache,
)
from protoprompt.context import ContextInput, ContextOutput
from protoprompt.exceptions import TokenBudgetExceededError
from protoprompt.hooks import ContextHooks, PipelineHooks
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
from protoprompt.store.async_store import (
    AsyncInMemStore,
    AsyncStoreWrapper,
    as_async,
)
from protoprompt.store.memory import InMemStore
from protoprompt.store.protocol import (
    AsyncStoreProtocol,
    StoreProtocol,
)
from protoprompt.store.sqlite import SqliteStore
from protoprompt.tokens.protocol import TokenCounter
from protoprompt.tokens.regex_counter import RegexTokenCounter

__version__ = "0.2.0"

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
    "AsyncStoreProtocol",
    "InMemStore",
    "AsyncInMemStore",
    "AsyncStoreWrapper",
    "SqliteStore",
    "as_async",
    "TokenCounter",
    "RegexTokenCounter",
    "LLMClientProtocol",
    "EmbeddingCache",
    "InMemoryEmbeddingCache",
    "CachedLLMClient",
    "ContextHooks",
    "PipelineHooks",
]
