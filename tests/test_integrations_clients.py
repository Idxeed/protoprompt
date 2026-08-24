"""Unit tests for integration clients, fully offline.

httpx-based clients are exercised via ``httpx.MockTransport``; the
OpenAI SDK client gets a stub ``AsyncOpenAI`` injected through its
constructor hook.
"""

from __future__ import annotations

import json

import httpx
import pytest

from protoprompt.integrations.httpx_client import HttpxLLMClient
from protoprompt.integrations.ollama_client import OllamaClient


def _openai_mock_handler(calls: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append({"url": str(request.url), "body": body})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "mocked reply"}}]
            })
        if request.url.path.endswith("/embeddings"):
            input_len = len(body["input"])
            return httpx.Response(200, json={
                "data": [
                    {"index": i, "embedding": [float(i), 1.0]}
                    for i in range(input_len)
                ]
            })
        return httpx.Response(404)

    return handler


def _ollama_mock_handler(calls: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append({"url": str(request.url), "body": body})
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={
                "message": {"role": "assistant", "content": "оллама ответ"}
            })
        if request.url.path == "/api/embed":
            n = len(body["input"])
            return httpx.Response(200, json={
                "embeddings": [[0.5, 0.5]] * n
            })
        return httpx.Response(404)

    return handler


async def test_httpx_client_chat():
    calls: list[dict] = []
    client = HttpxLLMClient(
        base_url="http://fake/v1",
        transport=httpx.MockTransport(_openai_mock_handler(calls)),
    )
    answer = await client.chat(
        [{"role": "user", "content": "hi"}],
        model="test-model",
        temperature=0.3,
    )
    assert answer == "mocked reply"
    sent = calls[0]["body"]
    assert sent["model"] == "test-model"
    assert sent["temperature"] == 0.3


async def test_httpx_client_embed_ordering():
    calls: list[dict] = []
    client = HttpxLLMClient(
        base_url="http://fake/v1",
        transport=httpx.MockTransport(_openai_mock_handler(calls)),
    )
    vectors = await client.embed(["a", "b", "c"], model="emb")
    assert vectors == [[0.0, 1.0], [1.0, 1.0], [2.0, 1.0]]


async def test_httpx_client_error_raises():
    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = HttpxLLMClient(
        base_url="http://fake/v1",
        transport=httpx.MockTransport(fail_handler),
    )
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await client.chat([{"role": "user", "content": "hi"}], model="m")


async def test_httpx_api_key_header():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = HttpxLLMClient(
        base_url="http://fake/v1",
        api_key="secret-key",
        transport=httpx.MockTransport(handler),
    )
    await client.chat([{"role": "user", "content": "hi"}], model="m")
    assert captured["auth"] == "Bearer secret-key"


async def test_ollama_chat_maps_options():
    calls: list[dict] = []
    client = OllamaClient(
        host="http://fake",
        transport=httpx.MockTransport(_ollama_mock_handler(calls)),
    )
    answer = await client.chat(
        [{"role": "user", "content": "привет"}],
        model="llama3.1",
        temperature=0.7,
        max_tokens=50,
    )
    assert answer == "оллама ответ"
    body = calls[0]["body"]
    assert body["model"] == "llama3.1"
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0.7, "num_predict": 50}


async def test_ollama_embed_defaults_to_nomic():
    calls: list[dict] = []
    client = OllamaClient(
        host="http://fake",
        transport=httpx.MockTransport(_ollama_mock_handler(calls)),
    )
    vectors = await client.embed(["текст"])
    assert vectors == [[0.5, 0.5]]
    assert calls[0]["body"]["model"] == "nomic-embed-text"


class _StubCompletions:
    async def create(self, **kwargs):
        self.kwargs = kwargs

        class _Msg:
            content = "sdk reply"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()


class _StubEmbeddings:
    async def create(self, **kwargs):
        self.kwargs = kwargs

        class _Data:
            def __init__(self, index):
                self.index = index
                self.embedding = [float(index)]

        class _Resp:
            data = [_Data(1), _Data(0)]

        return _Resp()


async def test_openai_client_chat_and_embed(monkeypatch):
    import sys
    import types

    fake_openai = types.ModuleType("openai")
    created: list[dict] = []

    class _Factory:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.chat = type("C", (), {})()
            self.chat.completions = _StubCompletions()
            self.embeddings = _StubEmbeddings()

    fake_openai.AsyncOpenAI = _Factory
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    from protoprompt.integrations.openai_client import OpenAIClient

    client = OpenAIClient(api_key="k", base_url="http://x/v1")
    reply = await client.chat([{"role": "user", "content": "yo"}], temperature=0.2)
    assert reply == "sdk reply"
    assert created[0]["api_key"] == "k"

    vectors = await client.embed(["b", "a"])
    assert vectors == [[0.0], [1.0]]  # sorted by index
