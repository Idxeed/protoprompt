"""Official MCP v2 server adapter for :class:`MemoryService`.

The SDK import is lazy, so importing ``protoprompt`` or
``protoprompt.integrations`` does not require the ``mcp`` extra.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from protoprompt.connectivity import MemoryService


def _mcp_server_class():
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise ImportError(
            "The MCP adapter requires the 'mcp' package. "
            "Install with: pip install 'protoprompt[mcp]'"
        ) from exc
    return MCPServer


def create_mcp_server(
    service: MemoryService,
    *,
    name: str = "protoprompt-memory",
):
    """Create an MCP v2 server pinned to ``service.scope`` by the host."""
    MCPServer = _mcp_server_class()
    server = MCPServer(
        name=name,
        instructions=(
            "Use protoprompt memory tools for confirmed durable facts and "
            "scoped recall. Tenant, user, and thread are fixed by the host."
        ),
    )

    @server.tool(
        name="memory_remember",
        description="Store one confirmed memory in the current host-controlled scope.",
    )
    async def memory_remember(text: str, memory_id: str | None = None) -> dict[str, Any]:
        return await service.remember(text, memory_id=memory_id)

    @server.tool(
        name="memory_search",
        description="Search memories in the current scope with provenance scores.",
    )
    async def memory_search(
        query: str,
        top_k: int = 5,
        score_threshold: float | None = None,
    ) -> dict[str, Any]:
        return {
            "results": await service.search(
                query,
                top_k=top_k,
                score_threshold=score_threshold,
            )
        }

    @server.tool(
        name="memory_forget",
        description="Delete one memory id from the current scope only.",
    )
    async def memory_forget(memory_id: str) -> dict[str, Any]:
        return await service.forget(memory_id)

    @server.tool(
        name="memory_profile_update",
        description="Update the current user's profile from one explicit signal.",
    )
    async def memory_profile_update(text: str) -> dict[str, Any]:
        return await service.profile_update(text)

    @server.tool(
        name="memory_explain",
        description="Explain the most recent recall without returning memory content.",
    )
    def memory_explain() -> dict[str, Any]:
        return service.explain()

    @server.tool(
        name="memory_budget_report",
        description="Return the latest context token-budget report when configured.",
    )
    def memory_budget_report() -> dict[str, Any]:
        return {"report": service.budget_report()}

    @server.resource(
        "memory://current/profile",
        name="current-profile",
        description="Read-only current user profile in the pinned scope.",
        mime_type="application/json",
    )
    async def current_profile() -> str:
        return _json(await service.current_profile())

    @server.resource(
        "memory://current/manifest",
        name="current-manifest",
        description="Read-only confirmed/cold memory manifest.",
        mime_type="application/json",
    )
    def current_manifest() -> str:
        return _json(service.manifest())

    @server.resource(
        "memory://current/last-report",
        name="last-budget-report",
        description="Read-only latest context budget report.",
        mime_type="application/json",
    )
    def last_report() -> str:
        return _json(service.budget_report())

    return server


def create_mcp_http_app(
    service: MemoryService,
    *,
    name: str = "protoprompt-memory",
    path: str = "/mcp",
    stateless: bool = True,
    host: str = "127.0.0.1",
):
    """Return a Starlette Streamable HTTP app for an existing ASGI host."""
    server = create_mcp_server(service, name=name)
    return server.streamable_http_app(
        streamable_http_path=path,
        stateless_http=stateless,
        host=host,
    )


def run_mcp_server(
    service: MemoryService,
    *,
    transport: Literal["stdio", "streamable-http"] = "stdio",
    name: str = "protoprompt-memory",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Run the same server over stdio or Streamable HTTP."""
    server = create_mcp_server(service, name=name)
    server.run(transport=transport, host=host, port=port)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
