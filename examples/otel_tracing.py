"""Send content-safe context spans to an OTLP/gRPC collector."""

from __future__ import annotations

import asyncio
import os

from protoprompt import ContextBuilder, ContextInput, InMemStore, MemoryScope
from protoprompt.integrations import create_otlp_runtime
from protoprompt.rag import DocumentIndexer


class DemoEmbeddings:
    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [[1.0, float(len(text) % 7)] for text in texts]


async def main() -> None:
    runtime = create_otlp_runtime(
        service_name="protoprompt-demo",
        endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "localhost:4317"),
        insecure=True,
    )
    try:
        store = InMemStore()
        embeddings = DemoEmbeddings()
        scope = MemoryScope(tenant="demo", user="alice")
        await DocumentIndexer(
            store,
            embeddings,
            scope=scope,
            event_sink=runtime.sink,
        ).index("contract", "The contract renews on 15 May.")
        builder = ContextBuilder(
            store,
            embeddings,
            scope=scope,
            event_sink=runtime.sink,
        )
        await builder.build(ContextInput(query="When does the contract renew?"))
        print("Exported content-safe spans. Open http://localhost:16686")
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
