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


def make_llm(
    cfg: dict | None = None,
    backend: str | None = None,
    cache: Any | None = None,
) -> LLMClientProtocol:
    """Собрать клиент нужного бэкенда из конфигурации."""
    cfg = cfg or {}
    backend = resolve_backend(cfg, backend)
    llm = cfg.get("llm", {})
    chat_model = llm.get("chat_model") or ""
    embed_model = llm.get("embed_model") or ""

    if backend == "ollama":
        ollama_cfg = llm.get("ollama", {})
        from protoprompt.integrations import OllamaClient

        kwargs: dict[str, Any] = {"host": ollama_cfg.get("host", "http://localhost:11434")}
        if chat_model:
            kwargs["chat_model"] = chat_model
        if embed_model:
            kwargs["embed_model"] = embed_model
        raw = OllamaClient(**kwargs)

    elif backend == "openai":
        openai_cfg = llm.get("openai", {})
        from protoprompt.integrations import OpenAIClient

        kwargs = {}
        if openai_cfg.get("api_key"):
            kwargs["api_key"] = openai_cfg["api_key"]
        if openai_cfg.get("base_url"):
            kwargs["base_url"] = openai_cfg["base_url"]
        kwargs["chat_model"] = chat_model or openai_cfg.get("model", "gpt-4o-mini")
        kwargs["embed_model"] = embed_model or openai_cfg.get(
            "embed_model", "text-embedding-3-small"
        )
        raw = OpenAIClient(**kwargs)

    elif backend == "httpx":
        httpx_cfg = llm.get("httpx", {})
        from protoprompt.integrations import HttpxLLMClient

        env_name = httpx_cfg.get("api_key_env")
        api_key = os.environ.get(env_name) if env_name else ""
        raw = HttpxLLMClient(
            base_url=httpx_cfg.get("base_url", "http://localhost:11434/v1"),
            api_key=api_key,
        )

    else:  # pragma: no cover — resolve_backend уже отсеял
        raise ValueError(f"unknown backend: {backend!r}")

    resolved_cache = cache if cache is not None else InMemoryEmbeddingCache()
    return CachedLLMClient(raw, resolved_cache)