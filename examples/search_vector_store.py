"""Run the same protoprompt vector-store flow on Elasticsearch or OpenSearch."""

from __future__ import annotations

import asyncio
import os

from protoprompt.integrations import ElasticsearchStore, OpenSearchStore


async def main() -> None:
    backend = os.environ.get("SEARCH_BACKEND", "elasticsearch").lower()
    if backend == "elasticsearch":
        store = ElasticsearchStore(
            os.environ.get("SEARCH_URL", "http://localhost:9200"),
            index_name="protoprompt-example",
            dimensions=2,
        )
    elif backend == "opensearch":
        store = OpenSearchStore(
            os.environ.get("SEARCH_URL", "http://localhost:9201"),
            index_name="protoprompt-example",
            dimensions=2,
        )
    else:
        raise SystemExit("SEARCH_BACKEND must be elasticsearch or opensearch")

    try:
        created = await store.setup()
        await store.add(
            "handbook",
            ["Vacation policy", "Incident response procedure"],
            [[1.0, 0.0], [0.0, 1.0]],
            {"tenant": "demo", "kind": "document"},
        )
        hits = await store.query(
            [0.9, 0.1],
            top_k=2,
            where={"tenant": "demo"},
            score_threshold=0.5,
        )
        print(f"index created: {created}")
        for hit in hits:
            print(f"{hit['score']:.3f}  {hit['document']}")
        await store.delete("handbook")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
