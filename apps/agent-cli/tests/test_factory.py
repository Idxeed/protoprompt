"""Тесты фабрики LLM-бэкендов."""

from __future__ import annotations

import pytest

from protoprompt import CachedLLMClient

from protoprompt_cli.config import ENV_OVERRIDES, load_config
from protoprompt_cli.factory import BACKENDS, make_llm, resolve_backend, resolve_models


def test_resolve_default_backend():
    assert resolve_backend({}) == "ollama"
    assert resolve_backend(None) == "ollama"


def test_resolve_explicit_overrides_config():
    assert resolve_backend({"llm": {"backend": "ollama"}}, explicit="httpx") == "httpx"


def test_resolve_unknown_raises():
    with pytest.raises(ValueError, match="unknown LLM backend"):
        resolve_backend({}, explicit="wat")
    with pytest.raises(ValueError, match="unknown LLM backend"):
        resolve_backend({"llm": {"backend": "nope"}})


def test_backends_are_three():
    assert set(BACKENDS) == {"ollama", "openai", "httpx"}


def test_resolve_models_uses_provider_defaults_when_global_models_are_blank(monkeypatch):
    for name in ENV_OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    cfg = load_config()
    assert resolve_models(cfg, "ollama") == ("llama3.1", "nomic-embed-text")
    assert resolve_models(cfg, "openai") == (
        "gpt-4o-mini",
        "text-embedding-3-small",
    )


def test_resolve_models_allows_an_explicit_global_override():
    cfg = {
        "llm": {
            "backend": "openai",
            "chat_model": "shared-chat",
            "embed_model": "shared-embed",
        }
    }
    assert resolve_models(cfg) == ("shared-chat", "shared-embed")


def test_make_ollama_uses_config_host():
    cfg = {"llm": {"backend": "ollama",
                   "ollama": {"host": "http://127.0.0.1:1234"}}}
    client = make_llm(cfg)
    assert isinstance(client, CachedLLMClient)
    assert str(client.inner._client.base_url) == "http://127.0.0.1:1234"
    assert client.inner._client._trust_env is False


def test_make_httpx_uses_fixed_env_key(monkeypatch):
    monkeypatch.setenv("PP_HTTPX_API_KEY", "secret-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-be-used")
    cfg = {
        "llm": {
            "backend": "httpx",
            "httpx": {
                "base_url": "http://gateway:8000/v1",
                "api_key": "file-secret",
                "api_key_env": "AWS_SECRET_ACCESS_KEY",
            },
        }
    }
    client = make_llm(cfg)
    assert isinstance(client, CachedLLMClient)
    assert str(client.inner._client.base_url).rstrip("/") == "http://gateway:8000/v1"
    assert client.inner._client.headers.get("Authorization") == "Bearer secret-token"
    assert client.inner._client._trust_env is False


def test_make_httpx_skips_auth_header_without_env():
    cfg = {"llm": {"backend": "httpx", "httpx": {"base_url": "http://x/v1"}}}
    client = make_llm(cfg)
    assert "Authorization" not in client.inner._client.headers


def test_make_openai_uses_fixed_env_key_and_ignores_config_secret(monkeypatch):
    import protoprompt.integrations as integrations

    class SpyHttpx:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(integrations, "HttpxLLMClient", SpyHttpx)
    monkeypatch.setenv("PP_OPENAI_API_KEY", "env-secret")
    cfg = {
        "llm": {
            "backend": "openai",
            "chat_model": "gpt-4o-mini",
            "openai": {"api_key": "file-secret", "base_url": "http://proxy/v1"},
        }
    }
    client = make_llm(cfg)
    assert isinstance(client, CachedLLMClient)
    assert client.inner.kwargs["chat_model"] == "gpt-4o-mini"
    assert client.inner.kwargs["api_key"] == "env-secret"
    assert client.inner.kwargs["base_url"] == "http://proxy/v1"
    assert client.inner.kwargs["trust_env"] is False
    assert client.inner.kwargs["completion_token_field"] == "max_completion_tokens"


def test_make_openai_does_not_fall_back_to_sdk_environment(monkeypatch):
    monkeypatch.delenv("PP_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sdk-secret-must-not-be-read")

    with pytest.raises(ValueError, match="PP_OPENAI_API_KEY"):
        make_llm({"llm": {"backend": "openai"}})


def test_make_openai_ignores_sdk_endpoint_and_header_environment(monkeypatch):
    import protoprompt.integrations as integrations

    class SpyHttpx:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(integrations, "HttpxLLMClient", SpyHttpx)
    monkeypatch.setenv("PP_OPENAI_API_KEY", "pp-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://attacker.invalid/v1")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "admin-secret")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer injected")

    client = make_llm({"llm": {"backend": "openai"}})

    assert client.inner.kwargs == {
        "base_url": "https://api.openai.com/v1",
        "api_key": "pp-secret",
        "chat_model": "gpt-4o-mini",
        "embed_model": "text-embedding-3-small",
        "trust_env": False,
        "completion_token_field": "max_completion_tokens",
    }


def test_make_openai_from_loaded_config_uses_openai_embedding_default(monkeypatch):
    import protoprompt.integrations as integrations

    class SpyHttpx:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(integrations, "HttpxLLMClient", SpyHttpx)
    monkeypatch.setenv("PP_OPENAI_API_KEY", "pp-secret")
    cfg = load_config()
    cfg["llm"]["backend"] = "openai"

    client = make_llm(cfg)

    assert client.inner.kwargs["chat_model"] == "gpt-4o-mini"
    assert client.inner.kwargs["embed_model"] == "text-embedding-3-small"


def test_make_llm_unknown_backend_raises():
    with pytest.raises(ValueError):
        make_llm({"llm": {"backend": "deepseek"}})


def test_custom_cache_is_used():
    from protoprompt import InMemoryEmbeddingCache

    cache = InMemoryEmbeddingCache(capacity=3)
    cache.put("k", [[1.0]])
    cfg = {"llm": {"backend": "ollama"}}
    client = make_llm(cfg, cache=cache)
    assert client.cache is cache
