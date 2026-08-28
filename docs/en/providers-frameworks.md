# Providers and framework bridges

ProtoPrompt keeps provider SDKs outside the core dependency graph. Every
adapter is lazy, capability-based, and tested without cloud credentials.

## Provider conformance matrix

| Provider client | Extra | Chat | Embed | Exact input tokens | Native semantics |
|---|---|---:|---:|---:|---|
| `OpenAIClient` | `[openai]` | yes | yes | no | official SDK, custom `base_url` |
| `AnthropicClient` | `[anthropic]` | yes | no | `messages.count_tokens` | top-level system instructions, content/tool blocks |
| `GoogleGenAIClient` | `[google]` | yes | yes | `models.count_tokens` | Developer API key or Vertex AI ADC, Gemini roles/config |
| `BedrockConverseClient` | `[bedrock]` | yes | no | Bedrock `CountTokens` | boto3 credential chain, Converse/system/inference config |
| `OllamaClient` | `[ollama]` | yes | yes | no | native `/api/chat` and `/api/embed` |
| `HttpxLLMClient` | `[http]` | yes | yes | no | OpenAI-compatible HTTP only |

The compatibility range validated for 0.5 is Anthropic SDK `1.x`, Google Gen
AI `2.20.x`, and boto3 `1.43.x`. Minor versions inside those guarded ranges run
the same deterministic contract suite in CI.

Install only what one deployment needs:

```bash
pip install "protoprompt[anthropic]"
pip install "protoprompt[google]"
pip install "protoprompt[bedrock]"
```

### Native clients

```python
from protoprompt import CompositeLLMClient
from protoprompt.integrations import AnthropicClient, GoogleGenAIClient

claude = AnthropicClient(model="claude-sonnet-4-6")
gemini = GoogleGenAIClient(embed_model="gemini-embedding-001")

# Anthropic has no embedding API. Pair independent capabilities explicitly.
llm = CompositeLLMClient(chat_client=claude, embedding_client=gemini)
```

All three new adapters:

- move portable `system`/`developer` messages to the provider's native system
  field;
- retain native tool/config options instead of projecting them onto OpenAI
  parameters;
- expose `await client.count_tokens(messages)` when the provider offers an
  exact billable count;
- expose `aclose()` and never own global SDK state.

Bedrock's boto3 calls execute through `asyncio.to_thread`, keeping the async
context pipeline non-blocking. IAM roles, web identity, profiles, and the
standard credential chain remain boto3's responsibility.

## Token budgeting

`ProviderTokenCounter` is deterministic, local, and synchronous, which makes it
safe inside `TokenBudgetedContextBuilder`:

```python
from protoprompt.tokens import ProviderTokenCounter

counter = ProviderTokenCounter("anthropic", model="claude-sonnet-4-6")
```

It uses provider-specific message overhead and `tiktoken` for OpenAI when that
extra is available, otherwise the multilingual regex estimator. This is a
budget signal, not a billing value. Use the provider client's async
`count_tokens()` at request-planning boundaries for an exact count. No hidden
network request occurs while assembling context.

## PydanticAI

Install the slim package integration:

```bash
pip install "protoprompt[pydanticai]"
```

```python
from pydantic_ai import Agent
from protoprompt.integrations import create_pydantic_ai_capability

agent = Agent(
    "anthropic:claude-sonnet-4-6",
    capabilities=[create_pydantic_ai_capability(memory_service)],
)
```

The bridge is a native `ProcessHistory` capability. It searches the
host-scoped `MemoryService` using the newest user prompt and adds recall only
to the model view. Retrieved text is a `UserPromptPart`, not a system prompt,
because memory is untrusted data. The original PydanticAI history is not
mutated.

The extra intentionally depends on `pydantic-ai-slim`, not the all-provider
meta-package. This avoids forcing PydanticAI's unrelated provider/MCP versions
into an application that already selected ProtoPrompt extras.

## LlamaIndex

```bash
pip install "protoprompt[llamaindex]"
```

```python
from llama_index.core.memory import Memory
from protoprompt.integrations import ProtoPromptMemoryBlock

block = ProtoPromptMemoryBlock(memory_service, top_k=5)
memory = Memory.from_defaults(
    session_id="thread-a",
    memory_blocks=[block],
    insert_method="user",
)
```

`ProtoPromptMemoryBlock` is a native `BaseMemoryBlock`. It recalls through the
pinned service and returns content for LlamaIndex's normal memory insertion
pipeline. Automatic persistence of evicted chat messages is disabled by
default: call `block.remember(...)` for confirmed memory, or opt in with
`auto_remember=True` after reviewing that policy.

## Google ADK support decision

The 2.8 spike found a technically viable extension point:
`google.adk.memory.BaseMemoryService` exposes `search_memory()` and session or
event ingestion. We are **not shipping a native ADK adapter in 0.5**.

Reasons:

1. ADK selects `app_name` and `user_id` dynamically, while ProtoPrompt's
   security boundary is a host-created, immutable `MemoryService` scope. A
   safe bridge needs a trusted service factory and an explicit mapping policy,
   not a single global adapter.
2. `add_session_to_memory()` implies automatic extraction from raw sessions;
   ProtoPrompt distinguishes raw history from confirmed durable memory.
3. Callback/request APIs have continued to move across recent ADK releases.
   Implementing recall through `before_model_callback` would create a fragile
   second integration path when `BaseMemoryService` is the correct one.

Current recommendation: expose ProtoPrompt through the supported MCP server to
ADK agents, or build an application-local `BaseMemoryService` with a trusted
scope factory. Revisit native support when ADK's memory scope and incremental
ingestion contracts stabilize; the acceptance gate is tenant-isolation tests
plus an explicit ingestion policy.

See runnable recipes in `examples/provider_clients.py`,
`examples/pydantic_ai_memory.py`, and `examples/llamaindex_memory.py`.
