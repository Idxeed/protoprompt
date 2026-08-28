from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from protoprompt import MemoryScope, MemoryService
from protoprompt.integrations.mcp_server import create_mcp_http_app, create_mcp_server
from protoprompt.profile.manager import ProfileManager
from protoprompt.profile.store import InMemoryProfileStore
from protoprompt.store.memory import InMemStore

from _mocks import MockLLM

mcp = pytest.importorskip("mcp")
from mcp import Client  # noqa: E402
from mcp.client.stdio import StdioServerParameters  # noqa: E402


def _server():
    scope = MemoryScope(tenant="acme", user="alice", thread="mcp")
    manager = ProfileManager(InMemoryProfileStore(), scope=scope)
    service = MemoryService(
        InMemStore(),
        MockLLM(embed_dim=8),
        scope,
        profile_manager=manager,
    )
    return create_mcp_server(service), service


@pytest.mark.asyncio
async def test_mcp_in_process_tools_and_resources():
    server, service = _server()
    async with Client(server, raise_exceptions=True) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "memory_remember",
            "memory_search",
            "memory_forget",
            "memory_profile_update",
            "memory_explain",
            "memory_budget_report",
        }
        resources = await client.list_resources()
        assert {str(resource.uri) for resource in resources.resources} == {
            "memory://current/profile",
            "memory://current/manifest",
            "memory://current/last-report",
        }

        remembered = await client.call_tool(
            "memory_remember",
            {"text": "The contract renews in May", "memory_id": "renewal"},
        )
        assert not remembered.is_error
        assert remembered.structured_content["memory_id"] == "renewal"

        searched = await client.call_tool(
            "memory_search",
            {"query": "contract", "top_k": 3},
        )
        assert searched.structured_content["results"][0]["memory_id"] == "renewal"
        assert searched.structured_content["results"][0]["text"] == "The contract renews in May"

        explained = await client.call_tool("memory_explain", {})
        assert "text" not in json.dumps(explained.structured_content)

        manifest = await client.read_resource("memory://current/manifest")
        payload = json.loads(manifest.contents[0].text)
        assert payload["confirmed_memory_ids"] == ["renewal"]

        profile = await client.call_tool(
            "memory_profile_update",
            {"text": "Пожалуйста, отвечай по-русски"},
        )
        assert profile.structured_content["user_id"] == "alice"
        profile_resource = await client.read_resource("memory://current/profile")
        assert json.loads(profile_resource.contents[0].text)["user_id"] == "alice"

        forgotten = await client.call_tool("memory_forget", {"memory_id": "renewal"})
        assert forgotten.structured_content["forgotten"] is True
        assert await service.search("contract") == []


def test_mcp_streamable_http_app_is_available():
    server, service = _server()
    app = create_mcp_http_app(service)
    assert app is not None
    assert any(getattr(route, "path", None) == "/mcp" for route in app.routes)


@pytest.mark.asyncio
async def test_mcp_stdio_subprocess_transport():
    script = Path(__file__).resolve().parents[1] / "examples" / "mcp_memory_server.py"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            str(script),
            "--database",
            ":memory:",
            "--tenant",
            "contract",
            "--user",
            "stdio-user",
        ],
    )
    async with Client(parameters) as client:
        tools = await client.list_tools()
        assert "memory_search" in {tool.name for tool in tools.tools}
        result = await client.call_tool(
            "memory_remember",
            {"text": "stdio transport works", "memory_id": "transport"},
        )
        assert result.structured_content["stored"] is True
