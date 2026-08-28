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
    "AnthropicClient": (
        "protoprompt.integrations.anthropic_client",
        "AnthropicClient",
    ),
    "GoogleGenAIClient": (
        "protoprompt.integrations.google_genai",
        "GoogleGenAIClient",
    ),
    "BedrockConverseClient": (
        "protoprompt.integrations.bedrock",
        "BedrockConverseClient",
    ),
    "AWSSecretsManagerStore": (
        "protoprompt.integrations.aws_secrets",
        "AWSSecretsManagerStore",
    ),
    "GCPSecretManagerStore": (
        "protoprompt.integrations.gcp_secrets",
        "GCPSecretManagerStore",
    ),
    "create_fastapi_memory_app": (
        "protoprompt.integrations.fastapi_service",
        "create_fastapi_memory_app",
    ),
    "PydanticAIMemoryAdapter": (
        "protoprompt.integrations.pydantic_ai",
        "PydanticAIMemoryAdapter",
    ),
    "create_pydantic_ai_capability": (
        "protoprompt.integrations.pydantic_ai",
        "create_pydantic_ai_capability",
    ),
    "ProtoPromptMemoryBlock": (
        "protoprompt.integrations.llamaindex",
        "ProtoPromptMemoryBlock",
    ),
    "ElasticsearchStore": (
        "protoprompt.integrations.search_store",
        "ElasticsearchStore",
    ),
    "OpenSearchStore": (
        "protoprompt.integrations.search_store",
        "OpenSearchStore",
    ),
    "SentenceTransformersClient": (
        "protoprompt.integrations.local_embeddings",
        "SentenceTransformersClient",
    ),
    "FastEmbedClient": (
        "protoprompt.integrations.local_embeddings",
        "FastEmbedClient",
    ),
    "QdrantStore": ("protoprompt.integrations.qdrant_store", "QdrantStore"),
    "create_mcp_server": (
        "protoprompt.integrations.mcp_server",
        "create_mcp_server",
    ),
    "create_mcp_http_app": (
        "protoprompt.integrations.mcp_server",
        "create_mcp_http_app",
    ),
    "run_mcp_server": (
        "protoprompt.integrations.mcp_server",
        "run_mcp_server",
    ),
    "ProtoPromptSession": (
        "protoprompt.integrations.agents_sdk",
        "ProtoPromptSession",
    ),
    "create_session_input_callback": (
        "protoprompt.integrations.agents_sdk",
        "create_session_input_callback",
    ),
    "ProtoPromptStoreAdapter": (
        "protoprompt.integrations.langgraph",
        "ProtoPromptStoreAdapter",
    ),
    "create_build_context_node": (
        "protoprompt.integrations.langgraph",
        "create_build_context_node",
    ),
    "create_sync_build_context_node": (
        "protoprompt.integrations.langgraph",
        "create_sync_build_context_node",
    ),
    "TelegramMemoryBot": (
        "protoprompt.integrations.telegram",
        "TelegramMemoryBot",
    ),
    "TelegramMemoryRegistry": (
        "protoprompt.integrations.telegram",
        "TelegramMemoryRegistry",
    ),
    "TelegramMemoryStatus": (
        "protoprompt.integrations.telegram",
        "TelegramMemoryStatus",
    ),
    "create_telegram_router": (
        "protoprompt.integrations.telegram",
        "create_telegram_router",
    ),
    "PgVectorStore": (
        "protoprompt.integrations.postgres",
        "PgVectorStore",
    ),
    "PostgresProfileStore": (
        "protoprompt.integrations.postgres",
        "PostgresProfileStore",
    ),
    "OpenTelemetryEventSink": (
        "protoprompt.integrations.otel",
        "OpenTelemetryEventSink",
    ),
    "OpenTelemetryRuntime": (
        "protoprompt.integrations.otel",
        "OpenTelemetryRuntime",
    ),
    "create_otlp_runtime": (
        "protoprompt.integrations.otel",
        "create_otlp_runtime",
    ),
    "RedisEmbeddingCache": (
        "protoprompt.integrations.redis",
        "RedisEmbeddingCache",
    ),
    "RedisSession": (
        "protoprompt.integrations.redis",
        "RedisSession",
    ),
    "RedisProfileStore": (
        "protoprompt.integrations.redis",
        "RedisProfileStore",
    ),
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
