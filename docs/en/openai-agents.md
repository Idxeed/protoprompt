# OpenAI Agents SDK

The `protoprompt[agents]` extra targets current `openai-agents 0.22` and can be
installed alongside `protoprompt[mcp]`.

`ProtoPromptSession` structurally implements the official `Session` protocol
and delegates persistence to the upstream `SQLiteSession`. Correction
workflows retain their original semantics:

```python
from agents import Agent, RunConfig, Runner
from protoprompt.integrations import ProtoPromptSession

session = ProtoPromptSession("legal-chat", "agents.db")
assistant_item = await session.pop_item()
user_item = await session.pop_item()
await session.clear_session()  # long-term scoped memory is not deleted
```

## Budgeted recall callback

The callback changes only model-facing input. The Agents SDK still persists
only the original new-turn items, so recalled context and old history are not
duplicated into the session:

```python
session = ProtoPromptSession("legal-chat", "agents.db")
callback = session.input_callback(
    budgeted_builder,
    system_prompt="Use recalled contract facts.",
)

result = await Runner.run(
    agent,
    "When does the contract renew?",
    session=session,
    run_config=RunConfig(session_input_callback=callback),
)
```

The callback first reserves the complete current `new_input` and, when needed,
an `output_reserve`. `TokenBudgetedContextBuilder` then assembles scoped
RAG/session/profile layers and fills what remains with the newest history
items. Structured content, `tool_calls`, and other current-turn fields are
budgeted; if the mandatory input itself cannot fit, the callback raises
`TokenBudgetExceededError` instead of sending an overflowing request.
Responses call/output pairs, including hosted MCP approvals, SDK-valid
anonymous server-side tool searches, streamed shell/tool-search outputs, and
preceding reasoning items, are retained or dropped as one dependency graph.
Several programmatic tool calls
and their owned children may interleave in that graph without being reordered.
Input-only `compaction_trigger` and `item_reference` controls are never
recalled before a new turn, and encrypted reasoning is retained only with its
actual model-emitted follower.

If `new_input` begins with an output, its complete trailing history graph is
mandatory and the callback raises rather than emitting a partial request when
it cannot fit. Anonymous **server-side** tool-search calls and outputs are
paired by their SDK order across the history/new-input boundary; client-side
tool searches must supply a `call_id`.

Offline comparison without an API key:

```bash
python examples/openai_agents_session.py
```

Do not combine a client-managed `Session` with server-managed
`conversation_id`/`previous_response_id`; conversation state needs one owner.
