from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from protoprompt import UserProfile
from protoprompt.integrations.postgres import PgVectorStore, PostgresProfileStore
from protoprompt.testing import check_vector_store

pytestmark = pytest.mark.integration


@pytest.fixture
def dsn() -> str:
    value = os.environ.get("PROTOPROMPT_POSTGRES_DSN")
    if not value:
        pytest.skip("set PROTOPROMPT_POSTGRES_DSN to run PostgreSQL tests")
    return value


@pytest.fixture
def schema() -> str:
    return "pp_test_" + uuid.uuid4().hex


@pytest.mark.asyncio
async def test_pgvector_contract_and_client_restart(dsn: str, schema: str):
    first = PgVectorStore(dsn, dimensions=2, schema=schema)
    await first.setup(create_extension=True, create_hnsw_index=True)
    report = await check_vector_store(first)
    assert report.contract == "vector_store"
    await first.add("restart-marker", ["survives reconnect"], [[1.0, 0.0]])
    await first.close()

    second = PgVectorStore(dsn, dimensions=2, schema=schema)
    await second.open()
    assert await second.count() == 1
    hit = await second.get("restart-marker")
    assert hit is not None
    assert hit["document"] == "survives reconnect"
    await second.close()


@pytest.mark.asyncio
async def test_profile_tenant_isolation_and_concurrent_cas(dsn: str, schema: str):
    acme = PostgresProfileStore(dsn, tenant="acme", schema=schema)
    await acme.setup()
    other = PostgresProfileStore(
        pool=acme.pool,
        tenant="other",
        schema=schema,
    )
    initial = UserProfile(user_id="alice", version=0)
    assert await acme.compare_and_put(initial, expected_version=None)
    assert await other.get("alice") is None

    candidates = [
        UserProfile(user_id="alice", facts={"winner": str(index)}, version=1)
        for index in range(12)
    ]
    outcomes = await asyncio.gather(*[
        acme.compare_and_put(profile, expected_version=0)
        for profile in candidates
    ])
    assert outcomes.count(True) == 1
    assert (await acme.get("alice")).version == 1
    await acme.close()
