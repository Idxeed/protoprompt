# Провайдеры и framework-мосты

SDK провайдеров не входят в граф зависимостей ядра ProtoPrompt. Каждый адаптер
загружается лениво, реализует только свои capabilities и тестируется без
облачных credentials.

## Матрица совместимости провайдеров

| Клиент | Экстра | Chat | Embed | Точные входные токены | Нативная семантика |
|---|---|---:|---:|---:|---|
| `OpenAIClient` | `[openai]` | да | да | нет | официальный SDK, свой `base_url` |
| `AnthropicClient` | `[anthropic]` | да | нет | `messages.count_tokens` | system-инструкции верхнего уровня, content/tool blocks |
| `GoogleGenAIClient` | `[google]` | да | да | `models.count_tokens` | Developer API key или Vertex AI ADC, роли/config Gemini |
| `BedrockConverseClient` | `[bedrock]` | да | нет | Bedrock `CountTokens` | credential chain boto3, Converse/system/inference config |
| `OllamaClient` | `[ollama]` | да | да | нет | нативные `/api/chat` и `/api/embed` |
| `HttpxLLMClient` | `[http]` | да | да | нет | только OpenAI-compatible HTTP |

Для 0.5 проверены Anthropic SDK `1.x`, Google Gen AI `2.20.x` и boto3
`1.43.x`. Minor-версии внутри этих ограниченных диапазонов проходят один и тот
же детерминированный contract suite в CI.

Ставьте только нужный конкретному deployment набор:

```bash
pip install "protoprompt[anthropic]"
pip install "protoprompt[google]"
pip install "protoprompt[bedrock]"
```

### Нативные клиенты

```python
from protoprompt import CompositeLLMClient
from protoprompt.integrations import AnthropicClient, GoogleGenAIClient

claude = AnthropicClient(model="claude-sonnet-4-6")
gemini = GoogleGenAIClient(embed_model="gemini-embedding-001")

# У Anthropic нет embedding API: capabilities объединяются явно.
llm = CompositeLLMClient(chat_client=claude, embedding_client=gemini)
```

Все три новых адаптера:

- переносят portable-роли `system`/`developer` в нативное system-поле;
- сохраняют native tool/config options, а не проецируют всё на OpenAI;
- дают `await client.count_tokens(messages)`, если провайдер умеет считать
  точное тарифицируемое число;
- имеют `aclose()` и не изменяют глобальное состояние SDK.

Синхронные вызовы boto3 в Bedrock выполняются через `asyncio.to_thread`, поэтому
async context pipeline не блокируется. IAM roles, web identity, profiles и
обычный credential chain остаются ответственностью boto3.

## Токен-бюджет

`ProviderTokenCounter` детерминированный, локальный и синхронный — его безопасно
использовать в `TokenBudgetedContextBuilder`:

```python
from protoprompt.tokens import ProviderTokenCounter

counter = ProviderTokenCounter("anthropic", model="claude-sonnet-4-6")
```

Он учитывает overhead сообщений конкретного провайдера, использует `tiktoken`
для OpenAI при наличии экстра, иначе — мультиязычную regex-оценку. Это сигнал
для бюджета, а не биллинга. Для точного числа вызывайте асинхронный
`count_tokens()` клиента на границе планирования запроса. Во время сборки
контекста скрытого сетевого вызова нет.

## PydanticAI

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

Мост — нативная capability `ProcessHistory`. Она ищет по закреплённому
`MemoryService` с последним пользовательским prompt и добавляет recall только в
model view. Найденный текст становится `UserPromptPart`, а не system prompt:
память — недоверенные данные. Исходная история PydanticAI не изменяется.

Экстра намеренно зависит от `pydantic-ai-slim`, а не all-provider
метапакета. Так приложение не получает чужие версии provider/MCP-зависимостей,
когда уже выбрало extras ProtoPrompt.

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

`ProtoPromptMemoryBlock` — нативный `BaseMemoryBlock`. Он читает через
закреплённый service и возвращает content в штатный memory pipeline
LlamaIndex. Автозапись вытесненных сообщений по умолчанию выключена: для
подтверждённой памяти вызывайте `block.remember(...)` или после проверки
политики явно включите `auto_remember=True`.

## Решение по Google ADK

Spike на ADK 2.8 нашёл технически подходящую точку расширения:
`google.adk.memory.BaseMemoryService` с `search_memory()` и ingestion сессий или
events. **Нативный ADK-адаптер в 0.5 не выпускаем.**

Причины:

1. ADK динамически передаёт `app_name` и `user_id`, а security boundary
   ProtoPrompt — созданный host-приложением неизменяемый scope `MemoryService`.
   Безопасному мосту нужна доверенная service factory и явная mapping policy.
2. `add_session_to_memory()` подразумевает автоматическое извлечение из сырой
   сессии; ProtoPrompt разделяет историю и подтверждённую долговременную память.
3. Callback/request API продолжали меняться в последних версиях ADK. Recall
   через `before_model_callback` дал бы хрупкий второй путь, хотя правильная
   абстракция уже существует в `BaseMemoryService`.

Сейчас рекомендуем подключать ProtoPrompt к ADK через поддержанный MCP server
или написать локальный `BaseMemoryService` с доверенной scope factory. Нативную
поддержку пересмотрим после стабилизации scope и incremental ingestion;
обязательные гейты — tenant-isolation tests и явная ingestion policy.

Запускаемые рецепты: `examples/provider_clients.py`,
`examples/pydantic_ai_memory.py`, `examples/llamaindex_memory.py`.
