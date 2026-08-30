"""Фабрика LLM-бэкендов: ollama | openai | httpx → LLMClientProtocol.

Все три равноправны: любые расхождения только в настройках. Результат
оборачивается в ``CachedLLMClient``, чтобы эмбеддинги не били по API
повторно.
"""

from __future__ import annotations

import os
from typing import Any

from protoprompt import CachedLLMClient, InMemoryEmbeddingCache
from protoprompt.llm import LLMClientProtocol

BACKENDS = ("ollama", "openai", "httpx")


def resolve_backend(cfg: dict | None, explicit: str | None = None) -> str:
    """Валидировать и вернуть имя бэкенда."""
    backend = explicit or (cfg or {}).get("llm", {}).get("backend", "ollama")
    if backend not in BACKENDS:
        raise ValueError(
            f"unknown LLM backend: {backend!r} (expected one of {BACKENDS})"
        )
    return backend


def resolve_models(
    cfg: dict | None, backend: str | None = None
) -> tuple[str, str]:
    """Resolve concrete chat and embedding defaults for one provider.

    ``llm.chat_model`` and ``llm.embed_model`` are explicit user-wide
    overrides.  When either is blank, fall back to the selected backend rather
    than allowing Ollama's embedding default to leak into an OpenAI request.
    """
    cfg = cfg or {}
    resolved_backend = resolve_backend(cfg, backend)
    llm = cfg.get("llm", {})
    chat_model = llm.get("chat_model") or ""
    embed_model = llm.get("embed_model") or ""

    if resolved_backend == "ollama":
        provider = llm.get("ollama", {})
        return (
            chat_model or provider.get("model", "llama3.1"),
            embed_model or provider.get("embed_model", "nomic-embed-text"),
        )
    if resolved_backend == "openai":
        provider = llm.get("openai", {})
        return (
            chat_model or provider.get("model", "gpt-4o-mini"),
            embed_model or provider.get("embed_model", "text-embedding-3-small"),
        )
    provider = llm.get("httpx", {})
    return (
        chat_model or provider.get("model", ""),
        embed_model or provider.get("embed_model", ""),
    )


def make_llm(
    cfg: dict | None = None,
    backend: str | None = None,
    cache: Any | None = None,
) -> LLMClientProtocol:
    """Собрать клиент нужного бэкенда из конфигурации."""
    cfg = cfg or {}
    backend = resolve_backend(cfg, backend)
    llm = cfg.get("llm", {})
    chat_model, embed_model = resolve_models(cfg, backend)

    if backend == "ollama":
        ollama_cfg = llm.get("ollama", {})
        from protoprompt.integrations import OllamaClient

        kwargs: dict[str, Any] = {
            "host": ollama_cfg.get("host", "http://localhost:11434"),
            # The agent's endpoint policy is explicit.  Ambient proxy and CA
            # settings must not silently redirect a loopback provider.
            "trust_env": False,
        }
        kwargs["chat_model"] = chat_model
        kwargs["embed_model"] = embed_model
        raw = OllamaClient(**kwargs)

    elif backend == "openai":
        openai_cfg = llm.get("openai", {})
        from protoprompt.integrations import HttpxLLMClient

        # The official SDK deliberately accepts several OPENAI_* ambient
        # settings (including custom headers).  The agent must have one
        # auditable credential and endpoint source, so use the stable
        # OpenAI-compatible REST surface directly instead of inheriting SDK
        # process-environment behaviour.
        api_key = os.environ.get("PP_OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OpenAI backend requires PP_OPENAI_API_KEY")
        raw = HttpxLLMClient(
            base_url=openai_cfg.get("base_url") or "https://api.openai.com/v1",
            api_key=api_key,
            chat_model=chat_model,
            embed_model=embed_model,
            trust_env=False,
            completion_token_field="max_completion_tokens",
        )

    elif backend == "httpx":
        httpx_cfg = llm.get("httpx", {})
        from protoprompt.integrations import HttpxLLMClient

        # Keep the credential source fixed rather than accepting api_key_env
        # from configuration.  PP_OPENAI_API_KEY is retained as a compatible
        # fallback for OpenAI-compatible gateways.
        api_key = os.environ.get("PP_HTTPX_API_KEY") or os.environ.get(
            "PP_OPENAI_API_KEY"
        )
        raw = HttpxLLMClient(
            base_url=httpx_cfg.get("base_url", "http://localhost:11434/v1"),
            api_key=api_key,
            chat_model=chat_model,
            embed_model=embed_model,
            trust_env=False,
        )

    else:  # pragma: no cover — resolve_backend уже отсеял
        raise ValueError(f"unknown backend: {backend!r}")

    resolved_cache = cache if cache is not None else InMemoryEmbeddingCache()
    return CachedLLMClient(raw, resolved_cache)
