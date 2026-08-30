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

Сначала callback резервирует текущий `new_input` целиком и, при необходимости,
`output_reserve`. Затем `TokenBudgetedContextBuilder` собирает scoped
RAG/session/profile слои и заполняет оставшееся место самыми новыми history
items. Structured content, `tool_calls` и другие поля текущего turn входят в
budget; если обязательный input сам не помещается, callback выбрасывает
`TokenBudgetExceededError`, а не отправляет переполненный запрос.
Пары call/output из Responses, включая hosted MCP approval, допустимый SDK
анонимный server-side tool search, потоковые shell/tool-search outputs и
предшествующие reasoning items либо сохраняются, либо отбрасываются как
единый граф зависимостей. Несколько
programmatic tool call и их owned children могут в нём переплетаться без
перестановки элементов. Input-only controls `compaction_trigger` и
`item_reference` не попадают в recall перед новым turn, а encrypted reasoning
сохраняется только вместе с настоящим model-emitted follower.

Если `new_input` начинается с output, весь его trailing history graph обязателен:
при нехватке места callback выбрасывает ошибку, а не формирует частичный
request. Анонимные **server-side** tool-search call/output сопоставляются по
порядку SDK через границу history/new-input; client-side tool search обязан
передавать `call_id`.

Офлайн-сравнение без API-ключа:

```bash
python examples/openai_agents_session.py
```

Не совмещайте client-managed `Session` с server-managed
`conversation_id`/`previous_response_id`: у истории должен быть один владелец.
