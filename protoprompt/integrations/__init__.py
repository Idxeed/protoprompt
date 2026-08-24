"""Optional integrations.

Every module here imports its third-party dependency lazily inside the
constructor, so importing ``protoprompt.integrations`` itself stays
cheap and dependency-free. Pick the extras you need:

    pip install "protoprompt[openai]"
    pip install "protoprompt[ollama]"
    pip install "protoprompt[http]"
    pip install "protoprompt[qdrant]"
    pip install "protoprompt[fastembed]"
    pip install "protoprompt[local]"

Exports are resolved lazily via PEP 562 ``__getattr__``.
"""

from typing import Any

_LAZY_EXPORTS = {
    "HttpxLLMClient": ("protoprompt.integrations.httpx_client", "HttpxLLMClient"),
    "OllamaClient": ("protoprompt.integrations.ollama_client", "OllamaClient"),
    "OpenAIClient": ("protoprompt.integrations.openai_client", "OpenAIClient"),
    "SentenceTransformersClient": (
        "protoprompt.integrations.local_embeddings",
        "SentenceTransformersClient",
    ),
    "FastEmbedClient": (
        "protoprompt.integrations.local_embeddings",
        "FastEmbedClient",
    ),
    "QdrantStore": ("protoprompt.integrations.qdrant_store", "QdrantStore"),
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None
    import importlib

    return getattr(importlib.import_module(module_name), attr)


def __dir__() -> list[str]:
    return sorted(__all__)
