"""Dependency-light protoprompt MCP server over stdio or Streamable HTTP.

Examples:
    python examples/mcp_memory_server.py
    python examples/mcp_memory_server.py --transport streamable-http --port 8000
"""

from __future__ import annotations

import argparse
import hashlib

from protoprompt import MemoryScope, MemoryService, SqliteStore
from protoprompt.integrations import run_mcp_server
from protoprompt.profile import ProfileManager, SqliteProfileStore


class DeterministicEmbeddings:
    """Small offline embedding client for transport demos, not production RAG."""

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.casefold().encode("utf-8")).digest()
            vectors.append([byte / 255.0 for byte in digest])
        return vectors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scoped protoprompt MCP memory server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database", default="protoprompt-mcp.db")
    parser.add_argument("--tenant", default="local")
    parser.add_argument("--user", default="demo-user")
    parser.add_argument("--thread", default="demo-thread")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scope = MemoryScope(
        tenant=args.tenant,
        user=args.user,
        thread=args.thread,
    )
    embeddings = DeterministicEmbeddings()
    profile_database = ":memory:" if args.database == ":memory:" else args.database + ".profiles"
    service = MemoryService(
        SqliteStore(args.database),
        embeddings,
        scope,
        profile_manager=ProfileManager(
            SqliteProfileStore(profile_database),
            scope=scope,
        ),
    )
    run_mcp_server(
        service,
        transport=args.transport,
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
