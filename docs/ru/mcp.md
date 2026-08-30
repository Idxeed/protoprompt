# Model Context Protocol

Extra `protoprompt[mcp]` поднимает официальный MCP v2 server поверх
`MemoryService`. Host создаёт `MemoryScope` один раз; ни один tool не принимает
tenant/user/thread, поэтому модель не может расширить область доступа.

## Готовый сервер

```bash
pip install "protoprompt[mcp]"

# stdio — для desktop/IDE host и Inspector
python examples/mcp_memory_server.py \
  --tenant acme --user u-42 --thread support

# Streamable HTTP — endpoint http://127.0.0.1:8000/mcp
python examples/mcp_memory_server.py \
  --transport streamable-http --host 127.0.0.1 --port 8000 \
  --tenant acme --user u-42 --thread support
```

Проверка stdio через официальный Inspector:

```bash
npx @modelcontextprotocol/inspector python examples/mcp_memory_server.py
```

Для production не оставляйте demo scope по умолчанию и не публикуйте HTTP
endpoint наружу без MCP auth/reverse-proxy policy. Demo embeddings
детерминированные и офлайн; для реального поиска передайте OpenAI, Ollama или
локальный embedding client в `MemoryService`.

## Tools

| Tool | Назначение |
|---|---|
| `memory_remember` | сохранить подтверждённое воспоминание |
| `memory_search` | вернуть scoped recall с score/provenance |
| `memory_forget` | удалить logical memory ID в текущем scope |
| `memory_profile_update` | добавить явный сигнал в профиль текущего user |
| `memory_explain` | content-free receipt последнего поиска |
| `memory_budget_report` | последний отчёт token budget |

Read-only resources: `memory://current/profile`,
`memory://current/manifest`, `memory://current/last-report`.

## Встраивание

```python
from protoprompt import MemoryScope, MemoryService, ProfileManager
from protoprompt.integrations import create_mcp_server, create_mcp_http_app
from protoprompt.profile import InMemoryProfileStore

scope = MemoryScope(tenant="acme", user="u-42", thread="support")
profiles = ProfileManager(InMemoryProfileStore(), scope=scope)
service = MemoryService(store, embeddings, scope, profile_manager=profiles)

mcp = create_mcp_server(service)          # mcp.run("stdio")
app = create_mcp_http_app(service)        # Starlette ASGI, путь /mcp
```

Один и тот же service используется обоими transport. In-process тесты можно
писать официальным `mcp.Client(mcp)` без процесса и порта.

Если включены profile endpoints, создавайте `ProfileManager` с точно тем же
host-owned scope, что и `MemoryService`: при несовпадении конструктор завершится
до первого profile I/O.
