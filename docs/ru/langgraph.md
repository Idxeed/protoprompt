# LangGraph

Установите адаптер для LangGraph 1.2:

```bash
pip install "protoprompt[langgraph]"
```

## Store с изоляцией scope

`ProtoPromptStoreAdapter` реализует полный sync/async-контракт LangGraph
`BaseStore`. Код графа продолжает видеть исходные логические namespace, а
адаптер добавляет физический непрозрачный префикс из контролируемого хостом
`MemoryScope`:

```python
from langgraph.store.memory import InMemoryStore
from protoprompt import MemoryScope
from protoprompt.integrations import ProtoPromptStoreAdapter

store = ProtoPromptStoreAdapter(
    InMemoryStore(),
    scope=MemoryScope(tenant="acme", user="alice"),
)
store.put(("memories",), "contract", {"renewal": "May"})
```

Граница действует для `get`, `put`, `delete`, `search`, перечисления
namespace, batch-операций и всех async-аналогов. Создавайте отдельный адаптер
для каждого доверенного scope. Не собирайте `MemoryScope` из аргументов,
которые сгенерировала модель.

## Готовый узел контекста

Для `graph.ainvoke` используйте async-фабрику, для `graph.invoke` — sync:

```python
from protoprompt.integrations import create_build_context_node

graph.add_node(
    "build_context",
    create_build_context_node(builder, chat_id="case-42"),
)
```

Узел читает `state["query"]`, а при его отсутствии — последнее текстовое
сообщение. Он записывает `context` и безопасный `context_provenance` без
содержимого памяти. Поле `messages` не перезаписывается, поэтому узел корректно
работает с message reducer. Через `input_factory=` доверенный код приложения
может передать профиль и собственную политику `ContextInput`.

## Кто владеет состоянием

Разделяйте два жизненных цикла:

- checkpointer LangGraph хранит состояние выполнения и историю конкретного
  треда;
- scoped Store LangGraph или backend protoprompt хранит межтредовую память.

Офлайн-пример показывает один профиль в двух тредах с независимой историей:

```bash
python examples/langgraph_memory.py
```
