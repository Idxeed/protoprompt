from __future__ import annotations

from contextlib import AbstractAsyncContextManager
import json

import pytest

from protoprompt import AsyncStoreProtocol, UserProfile
from protoprompt.integrations.postgres import PgVectorStore, PostgresProfileStore

pytest.importorskip("psycopg")


class FakeCursor:
    def __init__(self, rows=None, *, rowcount=0, many=None):
        self._rows = list(rows or [])
        self.rowcount = rowcount
        self._many = many

    async def fetchall(self):
        return list(self._rows)

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def executemany(self, statement, rows):
        if self._many is None:
            raise AssertionError("cursor was not configured for executemany")
        self._many.append((statement.as_string(), list(rows)))


class _Context(AbstractAsyncContextManager):
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeConnection:
    def __init__(self):
        self.calls: list[tuple[str, tuple | None]] = []
        self.many: list[tuple[str, list[tuple]]] = []
        self.responses: list[FakeCursor] = []

    def transaction(self):
        return _Context(self)

    def cursor(self):
        return _Context(FakeCursor(many=self.many))

    async def execute(self, statement, params=None):
        self.calls.append((statement.as_string(), params))
        if self.responses:
            return self.responses.pop(0)
        return FakeCursor(rowcount=1)

class FakePool:
    closed = False

    def __init__(self):
        self.connection_object = FakeConnection()

    def connection(self):
        return _Context(self.connection_object)


@pytest.mark.asyncio
async def test_pgvector_setup_is_explicit_and_add_is_atomic():
    pool = FakePool()
    store = PgVectorStore(pool=pool, dimensions=3)
    assert isinstance(store, AsyncStoreProtocol)
    assert pool.connection_object.calls == []

    await store.setup(create_extension=True)
    statements = "\n".join(sql for sql, _ in pool.connection_object.calls)
    assert "CREATE EXTENSION IF NOT EXISTS vector" in statements
    assert "vector(3)" in statements
    assert "USING hnsw" in statements

    pool.connection_object.calls.clear()
    await store.add(
        "contract",
        ["first", "second"],
        [[1, 0, 0], [0, 1, 0]],
        {"tenant": "acme"},
    )
    assert "DELETE FROM" in pool.connection_object.calls[0][0]
    statement, rows = pool.connection_object.many[0]
    assert "%s::vector" in statement
    assert len(rows) == 2
    assert json.loads(rows[0][3]) == {
        "tenant": "acme",
        "chunk_index": 0,
        "doc_id": "contract",
    }
    assert rows[0][4] == "[1,0,0]"


@pytest.mark.asyncio
async def test_pgvector_query_parameterizes_filters_and_score():
    pool = FakePool()
    store = PgVectorStore(pool=pool, dimensions=3)
    pool.connection_object.responses.append(FakeCursor([
        (7, "contract text", {"tenant": "acme"}, 0.91),
    ]))

    hits = await store.query(
        [1, 0, 0],
        top_k=4,
        where={"tenant": "acme", "kind": {"$in": ["document", "memory"]}},
        score_threshold=0.8,
    )

    statement, params = pool.connection_object.calls[-1]
    assert "metadata -> %s = %s::jsonb" in statement
    assert "1 - (embedding <=> %s::vector) >= %s" in statement
    assert params[-1] == 4
    assert hits == [{
        "id": 7,
        "document": "contract text",
        "metadata": {"tenant": "acme"},
        "score": 0.91,
    }]


@pytest.mark.asyncio
async def test_pgvector_validates_dimensions_and_filter_shape():
    store = PgVectorStore(pool=FakePool(), dimensions=3)
    with pytest.raises(ValueError, match="expected 3"):
        await store.query([1, 2])
    with pytest.raises(ValueError, match="finite"):
        await store.query([1, float("nan"), 3])
    with pytest.raises(TypeError, match="sequence"):
        await store.query([1, 2, 3], where={"kind": {"$in": "bad"}})
    assert await store.query([1, 2, 3], where={"kind": {"$in": []}}) == []
    with pytest.raises(ValueError, match="SQL identifier"):
        PgVectorStore(pool=FakePool(), dimensions=3, table="chunks; DROP TABLE")


@pytest.mark.asyncio
async def test_postgres_profile_store_uses_tenant_and_optimistic_locking():
    pool = FakePool()
    store = PostgresProfileStore(pool=pool, tenant="acme")
    profile = UserProfile(user_id="alice", facts={"role": "lawyer"}, version=2)

    await store.setup()
    pool.connection_object.calls.clear()
    await store.put(profile)
    _, put_params = pool.connection_object.calls[-1]
    assert put_params[0:2] == ("acme", "alice")

    pool.connection_object.responses.append(FakeCursor(rowcount=1))
    assert await store.compare_and_put(profile, expected_version=1) is True
    _, cas_params = pool.connection_object.calls[-1]
    assert cas_params[-3:] == ("acme", "alice", 1)

    pool.connection_object.responses.append(FakeCursor(rowcount=0))
    assert await store.compare_and_put(profile, expected_version=1) is False


@pytest.mark.asyncio
async def test_profile_get_round_trips_jsonb_mapping():
    pool = FakePool()
    store = PostgresProfileStore(pool=pool, tenant="acme")
    pool.connection_object.responses.append(FakeCursor([({
        "user_id": "alice",
        "facts": {"role": "lawyer"},
        "version": 3,
    },)]))
    profile = await store.get("alice")
    assert profile is not None
    assert profile.facts == {"role": "lawyer"}
    assert profile.version == 3
