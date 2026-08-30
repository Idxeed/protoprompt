from __future__ import annotations

import sys
import types

import pytest

from protoprompt.integrations.anthropic_client import AnthropicClient
from protoprompt.integrations.bedrock import BedrockConverseClient
from protoprompt.integrations.google_genai import GoogleGenAIClient
from protoprompt.testing import check_chat_client, check_embedding_client
from protoprompt.tokens import ProviderTokenCounter


class _AnthropicMessages:
    def __init__(self) -> None:
        self.create_request = None
        self.count_request = None

    async def create(self, **kwargs):
        self.create_request = kwargs
        return types.SimpleNamespace(
            content=[
                types.SimpleNamespace(type="text", text="native "),
                types.SimpleNamespace(type="tool_use", name="lookup"),
                types.SimpleNamespace(type="text", text="answer"),
            ]
        )

    async def count_tokens(self, **kwargs):
        self.count_request = kwargs
        return types.SimpleNamespace(input_tokens=17)


class _AnthropicSDK:
    def __init__(self) -> None:
        self.messages = _AnthropicMessages()
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_anthropic_native_system_content_and_exact_count():
    sdk = _AnthropicSDK()
    client = AnthropicClient(client=sdk, model="claude-test", max_tokens=99)

    answer = await client.chat(
        [
            {"role": "system", "content": "safe"},
            {"role": "developer", "content": "concise"},
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        ],
        temperature=0.2,
        tools=[{"name": "lookup"}],
    )

    assert answer == "native answer"
    assert sdk.messages.create_request == {
        "model": "claude-test",
        "max_tokens": 99,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        ],
        "system": "safe\n\nconcise",
        "tools": [{"name": "lookup"}],
        "extra_body": {"temperature": 0.2},
    }
    assert await client.count_tokens([{"role": "user", "content": "hello"}]) == 17
    assert "max_tokens" not in sdk.messages.count_request
    await check_chat_client(client)
    await client.aclose()
    assert sdk.closed


class _GoogleModels:
    def __init__(self) -> None:
        self.generate_request = None
        self.embed_request = None
        self.count_request = None

    async def generate_content(self, **kwargs):
        self.generate_request = kwargs
        return types.SimpleNamespace(text="gemini answer")

    async def embed_content(self, **kwargs):
        self.embed_request = kwargs
        return types.SimpleNamespace(
            embeddings=[types.SimpleNamespace(values=[float(i), 1.0]) for i, _ in enumerate(kwargs["contents"])]
        )

    async def count_tokens(self, **kwargs):
        self.count_request = kwargs
        return types.SimpleNamespace(total_tokens=23)


class _GoogleAsync:
    def __init__(self) -> None:
        self.models = _GoogleModels()
        self.closed = False

    async def aclose(self):
        self.closed = True


class _GoogleSDK:
    def __init__(self) -> None:
        self.aio = _GoogleAsync()


@pytest.mark.asyncio
async def test_google_native_roles_embeddings_and_exact_count():
    sdk = _GoogleSDK()
    client = GoogleGenAIClient(client=sdk, chat_model="gemini-test", embed_model="embed-test")

    answer = await client.chat(
        [
            {"role": "system", "content": "safe"},
            {"role": "assistant", "content": "prior"},
            {"role": "user", "content": "hello"},
        ],
        temperature=0.1,
        max_tokens=64,
        safety_settings=[{"category": "test"}],
    )
    assert answer == "gemini answer"
    request = sdk.aio.models.generate_request
    assert [item["role"] for item in request["contents"]] == ["model", "user"]
    assert request["config"]["system_instruction"] == "safe"
    assert request["config"]["max_output_tokens"] == 64

    await check_chat_client(client)
    await check_embedding_client(client)
    assert await client.count_tokens([{"role": "user", "content": "hello"}]) == 23
    await client.aclose()
    assert sdk.aio.closed


class _BedrockSDK:
    def __init__(self) -> None:
        self.converse_request = None
        self.count_request = None
        self.closed = False

    def converse(self, **kwargs):
        self.converse_request = kwargs
        return {"output": {"message": {"content": [{"text": "bedrock answer"}]}}}

    def count_tokens(self, **kwargs):
        self.count_request = kwargs
        return {"inputTokens": 31}

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_bedrock_converse_native_shape_and_exact_count():
    sdk = _BedrockSDK()
    client = BedrockConverseClient("provider.model-v1", client=sdk)

    assert await client.chat(
        [
            {"role": "system", "content": "safe"},
            {"role": "user", "content": "hello"},
        ],
        temperature=0.3,
        max_tokens=80,
        requestMetadata={"scope": "opaque"},
    ) == "bedrock answer"
    assert sdk.converse_request["messages"] == [
        {"role": "user", "content": [{"text": "hello"}]}
    ]
    assert sdk.converse_request["system"] == [{"text": "safe"}]
    assert sdk.converse_request["inferenceConfig"] == {
        "temperature": 0.3,
        "maxTokens": 80,
    }
    assert await client.count_tokens([{"role": "user", "content": "hello"}]) == 31
    assert sdk.count_request["input"]["converse"]["messages"]
    await check_chat_client(client)
    await client.aclose()
    assert sdk.closed


def test_provider_counter_is_deterministic_and_understands_aliases():
    google = ProviderTokenCounter("gemini")
    claude = ProviderTokenCounter("claude")
    messages = [{"role": "user", "content": [{"text": "hello world"}]}]

    assert google.provider == "google"
    assert claude.provider == "anthropic"
    assert google.count_messages(messages) == google.count("hello world") + 2
    assert claude.count_messages(messages) == claude.count("hello world") + 3


def test_openai_provider_counter_uses_tiktoken_when_available():
    pytest.importorskip("tiktoken")
    from protoprompt.tokens.tiktoken_adapter import TiktokenCounter

    counter = ProviderTokenCounter("openai", model="gpt-3.5-turbo")

    assert isinstance(counter._delegate, TiktokenCounter)
    assert counter.count("tokenization differs from regex estimates") == TiktokenCounter(
        model="gpt-3.5-turbo"
    ).count("tokenization differs from regex estimates")


def test_openai_provider_counter_falls_back_for_unknown_tiktoken_model():
    from protoprompt.tokens import RegexTokenCounter

    counter = ProviderTokenCounter("openai", model="unknown-model-for-test")

    assert isinstance(counter._delegate, RegexTokenCounter)


def test_unknown_portable_role_fails_before_provider_call():
    client = AnthropicClient(client=_AnthropicSDK())
    with pytest.raises(ValueError, match="Unsupported portable chat role"):
        client._request([{"role": "tool", "content": "unsafe"}], "", None, {})


@pytest.mark.parametrize(
    ("dependency", "module_name", "class_name", "extra"),
    [
        ("anthropic", "protoprompt.integrations.anthropic_client", "AnthropicClient", "anthropic"),
        ("google", "protoprompt.integrations.google_genai", "GoogleGenAIClient", "google"),
        ("boto3", "protoprompt.integrations.bedrock", "BedrockConverseClient", "bedrock"),
    ],
)
def test_provider_optional_dependency_error(monkeypatch, dependency, module_name, class_name, extra):
    monkeypatch.setitem(sys.modules, dependency, None)
    module = __import__(module_name, fromlist=[class_name])
    constructor = getattr(module, class_name)
    with pytest.raises(ImportError, match=rf"protoprompt\[{extra}\]"):
        constructor()
