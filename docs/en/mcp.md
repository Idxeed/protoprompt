# Model Context Protocol

The `protoprompt[mcp]` extra exposes an official MCP v2 server backed by
`MemoryService`. The host creates a `MemoryScope` once; no tool accepts a
tenant/user/thread argument, so the model cannot widen its access boundary.

## Ready-to-run server

```bash
pip install "protoprompt[mcp]"

# stdio — desktop/IDE hosts and Inspector
python examples/mcp_memory_server.py \
  --tenant acme --user u-42 --thread support

# Streamable HTTP — endpoint http://127.0.0.1:8000/mcp
python examples/mcp_memory_server.py \
  --transport streamable-http --host 127.0.0.1 --port 8000 \
  --tenant acme --user u-42 --thread support
```

Inspect the stdio server with the official Inspector:

```bash
npx @modelcontextprotocol/inspector python examples/mcp_memory_server.py
```

For production, replace the demo scope and do not expose the HTTP endpoint
without MCP authentication or a reverse-proxy policy. Demo embeddings are
deterministic and offline; use an OpenAI, Ollama, or local embedding client for
real retrieval.

## Tools

| Tool | Purpose |
|---|---|
| `memory_remember` | persist one confirmed memory |
| `memory_search` | scoped recall with score/provenance |
| `memory_forget` | delete a logical memory ID in the current scope |
| `memory_profile_update` | add an explicit signal to the current user profile |
| `memory_explain` | content-free receipt for the latest search |
| `memory_budget_report` | latest token-budget report |

Read-only resources: `memory://current/profile`,
`memory://current/manifest`, and `memory://current/last-report`.

## Embedding the server

```python
from protoprompt import MemoryScope, MemoryService
from protoprompt.integrations import create_mcp_server, create_mcp_http_app

scope = MemoryScope(tenant="acme", user="u-42", thread="support")
service = MemoryService(store, embeddings, scope, profile_manager=profiles)

mcp = create_mcp_server(service)          # mcp.run("stdio")
app = create_mcp_http_app(service)        # Starlette ASGI at /mcp
```

Both transports share the same service. In-process tests can use the official
`mcp.Client(mcp)` without a subprocess or port.
