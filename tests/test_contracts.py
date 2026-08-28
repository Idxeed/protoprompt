from __future__ import annotations

import json
import importlib
import sys
import types

import httpx
import pytest

from protoprompt.integrations.httpx_client import HttpxLLMClient
from protoprompt.integrations.ollama_client import OllamaClient
from protoprompt.profile.async_store import AsyncInMemoryProfileStore, as_async_profile
from protoprompt.profile.store import InMemoryProfileStore, SqliteProfileStore
from protoprompt.secrets.key import FileKeyProvider
from protoprompt.secrets.store import EncryptedSqliteSecretStore
from protoprompt.store.async_store import AsyncInMemStore, as_async
from protoprompt.store.memory import InMemStore
from protoprompt.store.sqlite import SqliteStore
from protoprompt.testing import (
    ContractViolation,
    check_chat_client,
    check_embedding_client,
    check_profile_store,
    check_secret_store,
    check_vector_store,
)


def _compatible_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode())
    if request.url.path.endswith("/chat/completions"):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    if request.url.path.endswith("/embeddings"):
        return httpx.Response(200, json={
            "data": [
                {"index": index, "embedding": [float(index), 1.0]}
                for index, _ in enumerate(body["input"])
            ]
        })
    raise AssertionError(request.url)


def _ollama_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode())
    if request.url.path == "/api/chat":
        return httpx.Response(200, json={"message": {"content": "ok"}})
    if request.url.path == "/api/embed":
        return httpx.Response(200, json={
            "embeddings": [[float(index), 1.0] for index, _ in enumerate(body["input"])]
        })
    raise AssertionError(request.url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client",
    [
        HttpxLLMClient(
            base_url="http://contract/v1",
            transport=httpx.MockTransport(_compatible_handler),
        ),
        OllamaClient(
            host="http://contract",
            transport=httpx.MockTransport(_ollama_handler),
        ),
    ],
)
async def test_official_http_clients_pass_chat_and_embedding_contracts(client):
    assert (await check_chat_client(client)).contract == "chat_client"
    assert (await check_embedding_client(client)).contract == "embedding_client"


@pytest.mark.asyncio
async def test_official_openai_client_passes_contracts(monkeypatch):
    fake_openai = types.ModuleType("openai")

    class _Completions:
        async def create(self, **kwargs):
            message = type("Message", (), {"content": "ok"})()
            choice = type("Choice", (), {"message": message})()
            return type("Response", (), {"choices": [choice]})()

    class _Embeddings:
        async def create(self, **kwargs):
            data = [
                type("Embedding", (), {"index": i, "embedding": [float(i), 1.0]})()
                for i, _ in enumerate(kwargs["input"])
            ]
            return type("Response", (), {"data": data})()

    class _Factory:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": _Completions()})()
            self.embeddings = _Embeddings()

    fake_openai.AsyncOpenAI = _Factory
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    from protoprompt.integrations.openai_client import OpenAIClient

    client = OpenAIClient(api_key="contract")
    await check_chat_client(client)
    await check_embedding_client(client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "store",
    [
        InMemStore(),
        SqliteStore(),
        AsyncInMemStore(),
        as_async(InMemStore()),
    ],
)
async def test_official_vector_stores_pass_contract(store):
    assert (await check_vector_store(store)).contract == "vector_store"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "store",
    [
        InMemoryProfileStore(),
        SqliteProfileStore(),
        AsyncInMemoryProfileStore(),
        as_async_profile(InMemoryProfileStore()),
    ],
)
async def test_official_profile_stores_pass_contract(store):
    assert (await check_profile_store(store)).contract == "profile_store"


@pytest.mark.asyncio
async def test_official_secret_store_passes_contract(tmp_path):
    store = EncryptedSqliteSecretStore(
        ":memory:",
        key_provider=FileKeyProvider(str(tmp_path / "contract.key")),
    )
    try:
        assert (await check_secret_store(store)).contract == "secret_store"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_contract_violation_is_actionable():
    class BrokenEmbeddingClient:
        async def embed(self, texts, model=""):
            return [[float("nan")]]

    with pytest.raises(ContractViolation, match="cardinality"):
        await check_embedding_client(BrokenEmbeddingClient())


@pytest.mark.asyncio
async def test_qdrant_store_passes_contract_when_installed():
    pytest.importorskip("qdrant_client")
    from protoprompt.integrations.qdrant_store import QdrantStore

    store = QdrantStore(collection_name="contract-kit", dim=2)
    await check_vector_store(store)
    with pytest.raises(ValueError, match="migrate data explicitly"):
        store._ensure_collection(3)


@pytest.mark.asyncio
async def test_chroma_store_passes_contract_when_installed():
    pytest.importorskip("chromadb")
    from protoprompt.store.chroma import ChromaStore

    await check_vector_store(ChromaStore(collection_name="contract-kit"))


@pytest.mark.parametrize(
    ("dependency", "module_name", "class_name", "extra"),
    [
        ("openai", "protoprompt.integrations.openai_client", "OpenAIClient", "openai"),
        ("fastembed", "protoprompt.integrations.local_embeddings", "FastEmbedClient", "fastembed"),
        (
            "sentence_transformers",
            "protoprompt.integrations.local_embeddings",
            "SentenceTransformersClient",
            "local",
        ),
        ("qdrant_client", "protoprompt.integrations.qdrant_store", "QdrantStore", "qdrant"),
        ("chromadb", "protoprompt.store.chroma", "ChromaStore", "chroma"),
        (
            "mcp.server",
            "protoprompt.integrations.mcp_server",
            "create_mcp_server",
            "mcp",
        ),
        (
            "agents",
            "protoprompt.integrations.agents_sdk",
            "ProtoPromptSession",
            "agents",
        ),
        (
            "aiogram",
            "protoprompt.integrations.telegram",
            "create_telegram_router",
            "telegram",
        ),
        (
            "psycopg",
            "protoprompt.integrations.postgres",
            "PgVectorStore",
            "postgres",
        ),
        (
            "opentelemetry",
            "protoprompt.integrations.otel",
            "OpenTelemetryEventSink",
            "otel",
        ),
        (
            "redis",
            "protoprompt.integrations.redis",
            "RedisEmbeddingCache",
            "redis",
        ),
        (
            "elasticsearch",
            "protoprompt.integrations.search_store",
            "ElasticsearchStore",
            "elasticsearch",
        ),
        (
            "opensearchpy",
            "protoprompt.integrations.search_store",
            "OpenSearchStore",
            "opensearch",
        ),
    ],
)
def test_optional_dependency_failure_names_install_extra(
    monkeypatch, dependency, module_name, class_name, extra
):
    monkeypatch.setitem(sys.modules, dependency, None)
    adapter = getattr(importlib.import_module(module_name), class_name)

    with pytest.raises(ImportError, match=rf"protoprompt\[{extra}\]"):
        if class_name == "create_mcp_server":
            adapter(None)
        elif class_name == "ProtoPromptSession":
            adapter("contract")
        elif class_name == "create_telegram_router":
            adapter(None)
        elif class_name in {"PgVectorStore", "ElasticsearchStore", "OpenSearchStore"}:
            if class_name == "PgVectorStore":
                adapter("postgresql://invalid", dimensions=3)
            else:
                adapter("http://invalid", dimensions=3)
        else:
            adapter()
