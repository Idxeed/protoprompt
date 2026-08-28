from __future__ import annotations

from contextlib import asynccontextmanager
import builtins

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from protoprompt import InMemStore, MemoryScope, MemoryService
from protoprompt.integrations.fastapi_service import create_fastapi_memory_app

from _mocks import MockLLM


def _app(*, max_metadata_bytes=32_768, lifespan=None):
    store = InMemStore()
    embeddings = MockLLM(embed_dim=8)
    services: dict[MemoryScope, MemoryService] = {}
    tokens = {
        "alice-token": MemoryScope(tenant="acme", user="alice", thread="api"),
        "bob-token": MemoryScope(tenant="acme", user="bob", thread="api"),
    }

    async def authorize(request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in tokens:
            raise HTTPException(status_code=401, detail="invalid bearer token")
        request.state.memory_scope = tokens[token]

    def resolve_scope(request):
        return request.state.memory_scope

    async def service_factory(scope):
        return services.setdefault(scope, MemoryService(store, embeddings, scope))

    return create_fastapi_memory_app(
        service_factory,
        resolve_scope,
        authorize,
        max_metadata_bytes=max_metadata_bytes,
        lifespan=lifespan,
    )


@pytest.mark.asyncio
async def test_fastapi_recipe_auth_scope_and_memory_lifecycle():
    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        denied = await client.post("/v1/memories", json={"text": "private"})
        assert denied.status_code == 401

        alice = {"Authorization": "Bearer alice-token"}
        bob = {"Authorization": "Bearer bob-token"}
        stored = await client.post(
            "/v1/memories",
            headers=alice,
            json={
                "text": "The contract renews in May",
                "memory_id": "renewal",
                "metadata": {"source": "user-confirmed"},
            },
        )
        assert stored.status_code == 201
        assert stored.json() == {"memory_id": "renewal", "stored": True}

        bob_hits = await client.post(
            "/v1/memories/search", headers=bob, json={"query": "contract"}
        )
        assert bob_hits.json() == {"results": []}
        alice_hits = await client.post(
            "/v1/memories/search", headers=alice, json={"query": "contract"}
        )
        assert alice_hits.json()["results"][0]["memory_id"] == "renewal"

        explanation = (await client.get(
            "/v1/memory/explain", headers=alice
        )).json()
        assert explanation["result_count"] == 1
        assert "text" not in explanation["results"][0]

        forgotten = await client.delete("/v1/memories/renewal", headers=alice)
        assert forgotten.json()["forgotten"] is True


@pytest.mark.asyncio
async def test_fastapi_recipe_bounds_and_openapi_do_not_expose_scope():
    app = _app(max_metadata_bytes=20)
    headers = {"Authorization": "Bearer alice-token"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        extra = await client.post(
            "/v1/memories", headers=headers, json={"text": "x", "tenant": "other"}
        )
        assert extra.status_code == 422
        large = await client.post(
            "/v1/memories",
            headers=headers,
            json={"text": "x", "metadata": {"payload": "x" * 100}},
        )
        assert large.status_code == 413
        invalid_k = await client.post(
            "/v1/memories/search",
            headers=headers,
            json={"query": "x", "top_k": 101},
        )
        assert invalid_k.status_code == 422

        schema = (await client.get("/openapi.json")).json()
        serialized = str(schema["components"]["schemas"])
        assert "tenant" not in serialized
        assert "user_id" not in serialized
        assert "thread_id" not in serialized


@pytest.mark.asyncio
async def test_fastapi_recipe_reports_unconfigured_profile_without_crashing():
    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/profile/signals",
            headers={"Authorization": "Bearer alice-token"},
            json={"text": "Reply concisely"},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "profile operations are not configured by the host"


def test_fastapi_recipe_forwards_lifespan():
    events = []

    @asynccontextmanager
    async def lifespan(app):
        events.append("started")
        yield
        events.append("stopped")

    with TestClient(_app(lifespan=lifespan)) as client:
        assert client.get("/healthz").status_code == 200
        assert events == ["started"]
    assert events == ["started", "stopped"]


def test_fastapi_recipe_missing_extra_error(monkeypatch):
    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "fastapi" or name.startswith("fastapi."):
            raise ImportError("blocked for contract")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(ImportError, match=r"protoprompt\[fastapi\]"):
        create_fastapi_memory_app(lambda scope: None, lambda request: None, lambda request: None)
