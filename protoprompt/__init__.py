"""protoprompt: layered context builder for LLM prompts.

Public top-level exports live here; per-layer subpackages re-export
their own public API as well, so both styles work:

    from protoprompt import ContextBuilder
    from protoprompt.context import ContextInput
"""

from protoprompt.cache import (
    AsyncEmbeddingCache,
    CachedLLMClient,
    EmbeddingCache,
    InMemoryEmbeddingCache,
)
from protoprompt.context import ContextInput, ContextOutput
from protoprompt.context_plan import (
    ContextBlockDecision,
    ContextDataLaneReceipt,
    ContextPlan,
    ContextRequestReceipt,
)
from protoprompt.connectivity import MemoryService
from protoprompt.exceptions import TokenBudgetExceededError
from protoprompt.events import (
    DEFAULT_REDACTION_POLICY,
    CacheEvent,
    CompressEvent,
    ContextEvent,
    EventDispatcher,
    EventSink,
    EvictEvent,
    ProfileEvent,
    ProtoPromptEvent,
    RecallEvent,
    RedactionPolicy,
    RetrieveEvent,
)
from protoprompt.hooks import ContextHooks, PipelineHooks
from protoprompt.injector import ContextBuilder
from protoprompt.injector_budgeted import (
    DEFAULT_PRIORITIES,
    BudgetReport,
    TokenBudgetedContextBuilder,
)
from protoprompt.llm import (
    ChatClientProtocol,
    CompositeLLMClient,
    EmbeddingClientProtocol,
    LLMClientProtocol,
)
from protoprompt.pipeline import Pipeline
from protoprompt.profile.builder import ProfileBuilder
from protoprompt.profile.codec import (
    DEFAULT_PROFILE,
    STRICT_PROFILE,
    CodecProfile,
)
from protoprompt.profile.manager import ProfileManager
from protoprompt.profile.source import (
    CompositeProfileSource,
    LLMProfileSource,
    ProfileProtocol,
    RuleProfileSource,
)
from protoprompt.profile.types import (
    FactOp,
    Preferences,
    ProfileDelta,
    Signal,
    Traits,
    UserProfile,
)
from protoprompt.session.compressor import Compressor
from protoprompt.session.strategy import (
    HeuristicStrategy,
    LLMSummaryStrategy,
    StrategyProtocol,
)
from protoprompt.session.types import CompressedBlock, Session
from protoprompt.scope import MemoryScope, scoped_doc_id, scoped_metadata
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

__version__ = "0.13.0"

__all__ = [
    "ContextBuilder",
    "TokenBudgetedContextBuilder",
    "ContextInput",
    "ContextOutput",
    "ContextBlockDecision",
    "ContextDataLaneReceipt",
    "ContextPlan",
    "ContextRequestReceipt",
    "MemoryService",
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
    "ProfileManager",
    "ProfileProtocol",
    "LLMProfileSource",
    "RuleProfileSource",
    "CompositeProfileSource",
    "Traits",
    "Preferences",
    "FactOp",
    "ProfileDelta",
    "Signal",
    "CodecProfile",
    "DEFAULT_PROFILE",
    "STRICT_PROFILE",
    "StoreProtocol",
    "AsyncStoreProtocol",
    "InMemStore",
    "AsyncInMemStore",
    "AsyncStoreWrapper",
    "SqliteStore",
    "as_async",
    "TokenCounter",
    "RegexTokenCounter",
    "ChatClientProtocol",
    "EmbeddingClientProtocol",
    "LLMClientProtocol",
    "CompositeLLMClient",
    "EmbeddingCache",
    "AsyncEmbeddingCache",
    "InMemoryEmbeddingCache",
    "CachedLLMClient",
    "ContextHooks",
    "PipelineHooks",
    "ProtoPromptEvent",
    "ContextEvent",
    "RetrieveEvent",
    "CompressEvent",
    "ProfileEvent",
    "RecallEvent",
    "EvictEvent",
    "CacheEvent",
    "EventSink",
    "EventDispatcher",
    "RedactionPolicy",
    "DEFAULT_REDACTION_POLICY",
    "MemoryScope",
    "scoped_doc_id",
    "scoped_metadata",
]
