from __future__ import annotations

import os
import uuid

import pytest

from protoprompt.integrations.search_store import ElasticsearchStore, OpenSearchStore
from protoprompt.testing import check_vector_store

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment", "store_type"),
    [
        ("PROTOPROMPT_ELASTICSEARCH_URL", ElasticsearchStore),
        ("PROTOPROMPT_OPENSEARCH_URL", OpenSearchStore),
    ],
)
async def test_search_contract_and_restart(environment, store_type):
    url = os.environ.get(environment)
    if not url:
        pytest.skip(f"set {environment} to run this live search test")
    index_name = "pp-test-" + uuid.uuid4().hex
    first = store_type(url, index_name=index_name, dimensions=2)
    try:
        assert await first.setup() is True
        report = await check_vector_store(first)
        assert report.contract == "vector_store"
        await first.add("restart-marker", ["survives reconnect"], [[1.0, 0.0]])
    finally:
        await first.aclose()

    second = store_type(url, index_name=index_name, dimensions=2)
    try:
        assert await second.setup() is False
        assert await second.count() == 1
        hits = await second.query([1.0, 0.0], top_k=1)
        assert hits[0]["document"] == "survives reconnect"
        assert hits[0]["score"] == pytest.approx(1.0)
    finally:
        await second.client.indices.delete(index=index_name)
        await second.close()
