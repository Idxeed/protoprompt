# Migrating from 0.3 to 0.6

Version 0.6 keeps the 0.3 public APIs while adding capability-specific clients,
host-controlled memory scopes and opt-in integrations. Existing applications can
upgrade without enabling any new dependency or moving their data.

## Safe upgrade sequence

1. Pin and test `protoprompt==0.6.*` with the same extras used by the 0.3
   application. Core still has no mandatory third-party dependencies.
2. Keep existing composite clients working through `LLMClientProtocol`. New code
   should accept `ChatClientProtocol` or `EmbeddingClientProtocol` when only one
   capability is required.
3. Introduce `MemoryScope` at a trusted host boundary. An empty scope preserves
   the legacy physical layout; do not let model output choose tenant, user or
   thread identifiers.
4. Enable one integration at a time with its named extra and run its contract
   suite. Keep the previous implementation selectable in configuration.
5. For a persistent backend, create a versioned schema/index, backfill, compare
   reads, then switch traffic. Setup and migrations are explicit and are never
   performed by importing the package.

The `[chroma]` extra now resolves to ChromaDB `>=1.5,<2` so Python 3.13 receives
supported binary dependencies. The `ChromaStore` constructor and methods are
unchanged. Back up a persistent Chroma directory before opening it with the new
engine; rollback restores that backup and the previously pinned package.

## Data-bearing integrations

- PostgreSQL/pgvector, Elasticsearch and OpenSearch should use a new versioned
  table or index for backfill and shadow reads. Keep the old store read-only
  until the rollback window closes.
- Qdrant no longer recreates a collection on an embedding-dimension mismatch.
  Create a versioned collection and migrate explicitly; rollback selects the
  previous collection without data loss.
- Redis adapters are for cache and ephemeral state. A rollback can discard their
  keys and rebuild them from the authoritative store.
- AWS and GCP secret stores use opaque resource names and versioned envelopes.
  Copy secrets scope by scope, verify reads through `SecretAccess`, and retain the
  encrypted SQLite vault read-only during the rollback window.
- Document reader changes require reindexing into a versioned collection so the
  previous parser output remains selectable.

## Connectivity and providers

MCP, OpenAI Agents, LangGraph, Telegram, FastAPI and provider adapters are opt-in
edges around the same memory service. Deploy them behind existing authentication,
derive scope from trusted claims, canary traffic, and remove the route or adapter
to roll back. They do not mutate legacy storage during import.

## Rollback

Stop writes to the new backend, switch configuration to the previous adapter or
index, and reinstall the previously pinned package version. Do not downgrade a
shared schema destructively. Preserve old data and credentials until application
and recall metrics have remained healthy for the agreed rollback window.

## Removal and deprecation policy

Official integrations have a documented owner in the repository maintainer
group. A supported adapter is deprecated in documentation and the changelog for
at least one minor release before removal; removal normally waits for the next
major release. Security fixes may disable unsafe behavior earlier, with a
migration and rollback note in the same release.
