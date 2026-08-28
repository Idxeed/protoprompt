"""Run the public adapter contracts without a test framework."""

from __future__ import annotations

import asyncio

from protoprompt import InMemStore
from protoprompt.testing import check_embedding_client, check_vector_store


class DemoEmbeddings:
    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [[float(index), 1.0] for index, _ in enumerate(texts)]


async def main() -> None:
    embedding_report = await check_embedding_client(DemoEmbeddings())
    store_report = await check_vector_store(InMemStore())
    print(embedding_report)
    print(store_report)


if __name__ == "__main__":
    asyncio.run(main())
