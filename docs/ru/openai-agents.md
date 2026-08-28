# OpenAI Agents SDK

Extra `protoprompt[agents]` интегрируется с актуальным `openai-agents 0.22` и
совместим с `protoprompt[mcp]` в одном окружении.

`ProtoPromptSession` структурно реализует официальный `Session` protocol и
делегирует хранение upstream `SQLiteSession`. Поэтому correction workflow не
меняется:

```python
from agents import Agent, RunConfig, Runner
from protoprompt.integrations import ProtoPromptSession

session = ProtoPromptSession("legal-chat", "agents.db")
assistant_item = await session.pop_item()
user_item = await session.pop_item()
await session.clear_session()  # long-term scoped memory не удаляется
```

## Budgeted recall callback

Callback меняет только model-facing input. Agents SDK по-прежнему сохраняет
только оригинальные items нового turn, поэтому recall и старая история не
дублируются в session:

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

Сначала `TokenBudgetedContextBuilder` собирает scoped RAG/session/profile
слои. В оставшийся бюджет callback кладёт самые новые history items; текущий
turn сохраняется всегда и ровно один раз.

Офлайн-сравнение без API-ключа:

```bash
python examples/openai_agents_session.py
```

Не совмещайте client-managed `Session` с server-managed
`conversation_id`/`previous_response_id`: у истории должен быть один владелец.
