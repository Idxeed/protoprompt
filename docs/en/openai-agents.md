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

`TokenBudgetedContextBuilder` first assembles scoped RAG/session/profile
layers. The callback fills the remaining budget with the newest history items;
the current turn is always retained exactly once.

Offline comparison without an API key:

```bash
python examples/openai_agents_session.py
```

Do not combine a client-managed `Session` with server-managed
`conversation_id`/`previous_response_id`; conversation state needs one owner.
