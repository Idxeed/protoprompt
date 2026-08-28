# LangGraph

Install the adapter for LangGraph 1.2:

```bash
pip install "protoprompt[langgraph]"
```

## Scope-safe Store

`ProtoPromptStoreAdapter` is a complete sync/async LangGraph `BaseStore`.
It preserves logical namespaces for graph code while adding an opaque physical
prefix derived from a host-owned `MemoryScope`:

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

The boundary applies to `get`, `put`, `delete`, `search`, namespace listing,
batch operations, and all async counterparts. Use one adapter instance per
trusted scope. Do not build `MemoryScope` from model-generated tool arguments.

## Ready context node

Use the async factory with `graph.ainvoke` or the sync factory with
`graph.invoke`:

```python
from protoprompt.integrations import create_build_context_node

graph.add_node(
    "build_context",
    create_build_context_node(builder, chat_id="case-42"),
)
```

The node reads `state["query"]`, falling back to the newest text message. It
writes `context` and content-free `context_provenance`. It never rewrites the
`messages` field, so it is safe with LangGraph message reducers. Customize
`ContextInput` with `input_factory=` when profiles or per-graph policy need to
be supplied by trusted application code.

## State ownership

Keep the two lifecycles distinct:

- a LangGraph checkpointer owns per-thread execution state and message history;
- a scoped LangGraph Store or a protoprompt backend owns cross-thread memory.

The offline example stores one profile across two threads while each thread's
history remains independent:

```bash
python examples/langgraph_memory.py
```
