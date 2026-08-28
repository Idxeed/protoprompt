"""Тесты фабрики LLM-бэкендов."""

from __future__ import annotations

import pytest

from protoprompt import CachedLLMClient

from protoprompt_cli.factory import BACKENDS, make_llm, resolve_backend


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


def test_make_ollama_uses_config_host():
    cfg = {"llm": {"backend": "ollama",
                   "ollama": {"host": "http://127.0.0.1:1234"}}}
    client = make_llm(cfg)
    assert isinstance(client, CachedLLMClient)
    assert str(client.inner._client.base_url) == "http://127.0.0.1:1234"


def test_make_httpx_uses_base_url_and_env_key(monkeypatch):
    monkeypatch.setenv("PP_HTTPX_KEY", "secret-token")
    cfg = {
        "llm": {
            "backend": "httpx",
            "httpx": {"base_url": "http://gateway:8000/v1",
                      "api_key_env": "PP_HTTPX_KEY"},
        }
    }
    client = make_llm(cfg)
    assert isinstance(client, CachedLLMClient)
    assert str(client.inner._client.base_url).rstrip("/") == "http://gateway:8000/v1"
    assert client.inner._client.headers.get("Authorization") == "Bearer secret-token"


def test_make_httpx_skips_auth_header_without_env():
    cfg = {"llm": {"backend": "httpx", "httpx": {"base_url": "http://x/v1"}}}
    client = make_llm(cfg)
    assert "Authorization" not in client.inner._client.headers


def test_make_openai_uses_config(monkeypatch):
    pytest.importorskip("openai")
    cfg = {
        "llm": {
            "backend": "openai",
            "chat_model": "gpt-4o-mini",
            "openai": {"api_key": "sk-test", "base_url": "http://proxy/v1"},
        }
    }
    client = make_llm(cfg)
    assert isinstance(client, CachedLLMClient)
    assert client.inner._chat_model == "gpt-4o-mini"


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