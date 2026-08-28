# protoprompt 0.6.0

The integration-platform release. It keeps the dependency-free 0.3 core and
adds host-controlled memory scopes, production backends, agent/framework bridges,
native model providers, safe document ingestion, and deployable service recipes.

## Highlights

- **One memory layer across runtimes** — MCP 2.x, OpenAI Agents, LangGraph,
  PydanticAI, LlamaIndex, aiogram, and FastAPI use the same scope-pinned
  `MemoryService` semantics.
- **Production persistence** — PostgreSQL/pgvector, Redis cache/session/profile,
  Elasticsearch 9, and OpenSearch 3 adapters with explicit setup, contracts,
  concurrency/reconnect coverage, and opt-in live tests.
- **Native providers** — Anthropic Messages, Google GenAI/Vertex, and Amazon
  Bedrock Converse with capability-specific clients and provider token counting.
- **Safe data ingestion** — bounded text/source/HTML/PDF/DOCX readers, trusted
  provenance, and dependency-free LlamaIndex/Unstructured converters.
- **Enterprise credentials** — AWS and GCP managed secret stores preserve scope,
  TTL, and deletion semantics while keeping plaintext identifiers out of cloud
  resource names.
- **Observable by default, private by default** — typed OpenTelemetry-ready events
  carry trace/scope metrics while recursively redacting content.
- **Runnable delivery path** — Telegram demo, FastAPI service, Dockerfile,
  Kubernetes/minikube manifest, and RU/EN operational guides.

## Upgrade

```bash
pip install --upgrade protoprompt

# Install only the integrations you use, for example:
pip install "protoprompt[mcp,postgres,otel]"
pip install "protoprompt[anthropic,pydanticai]"
pip install "protoprompt[documents,elasticsearch,fastapi]"
pip install "protoprompt[aws-secrets]"  # or gcp-secrets
```

Existing `LLMClientProtocol` implementations and unscoped 0.3 stores keep working.
New code can provide separate chat/embedding clients and migrate one tenant at a
time with `MemoryScope`. Constructors never create remote schemas implicitly;
run the documented `setup()`/migration step before switching traffic.

The optional ChromaDB range is now `>=1.5,<2`; the `ChromaStore` API and its
persistent-directory constructor remain unchanged. Back up an existing Chroma
directory before opening it with the newer engine.

## Verification status

- The socket-blocked deterministic suite passes 394 tests on each of Python
  3.11, 3.12, and 3.13; Python 3.12 coverage is 87.8%.
- Live PostgreSQL/pgvector, Redis, Elasticsearch 9, OpenSearch 3, and local
  Chroma contracts pass 10 tests, including reconnect, TTL, filtering and
  concurrency. AWS/GCP live contracts remain opt-in because they create billable
  cloud resources and require dedicated test credentials.
- The agent CLI passes 188 tests; eight offline examples and the authenticated
  FastAPI lifecycle were executed successfully.
- Wheel and sdist contents, 28 extras, zero-dependency isolated import, strict
  RU/EN docs, and the minikube server-side deployment validation pass.

This file prepares the release only. No tag, PyPI upload, GitHub Release, cloud
resource, or documentation deployment is created by the preparation itself.
