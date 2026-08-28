from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from protoprompt import AsyncStoreProtocol
from protoprompt.integrations.search_store import ElasticsearchStore, OpenSearchStore
from protoprompt.testing import check_vector_store


class FakeIndices:
    def __init__(self) -> None:
        self.created: dict | None = None

    def exists(self, *, index):
        async def wrapped_response():
            return self.created is not None

        return wrapped_response()

    async def create(self, **kwargs):
        self.created = kwargs
        return {"acknowledged": True}


class FakeSearchClient:
    """Small service fake that understands both official client call shapes."""

    def __init__(self) -> None:
        self.indices = FakeIndices()
        self.documents: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def bulk(self, **kwargs):
        self.calls.append(("bulk", kwargs))
        operations = kwargs.get("operations", kwargs.get("body", []))
        for position in range(0, len(operations), 2):
            action, source = operations[position:position + 2]
            identity = action["index"]["_id"]
            self.documents[identity] = dict(source)
        return {"errors": False, "items": []}

    async def delete_by_query(self, **kwargs):
        self.calls.append(("delete_by_query", kwargs))
        query = kwargs.get("query") or kwargs["body"]["query"]
        field, expected = next(iter(query["term"].items()))
        doomed = [
            identity for identity, source in self.documents.items()
            if _get(source, field) == expected
        ]
        for identity in doomed:
            del self.documents[identity]
        return {"deleted": len(doomed)}

    async def search(self, **kwargs):
        self.calls.append(("search", kwargs))
        if "knn" in kwargs:
            vector = kwargs["knn"]["query_vector"]
            filter_clause = kwargs["knn"].get("filter")
            size = kwargs["size"]
        else:
            payload = kwargs["body"]
            knn = payload["query"]["knn"]["embedding"]
            vector = knn["vector"]
            filter_clause = knn.get("filter")
            size = payload["size"]
        hits = []
        for identity, source in self.documents.items():
            if filter_clause and not _matches(source, filter_clause):
                continue
            score = _cosine(vector, source["embedding"])
            hits.append({"_id": identity, "_score": score, "_source": source})
        hits.sort(key=lambda hit: hit["_score"], reverse=True)
        return {"hits": {"hits": hits[:size]}}

    async def count(self, **kwargs):
        self.calls.append(("count", kwargs))
        return {"count": len(self.documents)}

    async def ping(self):
        return True

    async def close(self):
        self.closed = True


def _get(source: dict, field: str):
    value = source
    for part in field.split("."):
        value = value.get(part)
    return value


def _matches(source: dict, clause: dict) -> bool:
    if "bool" in clause:
        return all(_matches(source, child) for child in clause["bool"]["filter"])
    if "term" in clause:
        field, expected = next(iter(clause["term"].items()))
        return _get(source, field) == expected
    if "terms" in clause:
        field, expected = next(iter(clause["terms"].items()))
        return _get(source, field) in expected
    raise AssertionError(f"unsupported fake query: {clause}")


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / math.sqrt(
        sum(value * value for value in left)
        * sum(value * value for value in right)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store_type", "dialect"),
    [(ElasticsearchStore, "elasticsearch"), (OpenSearchStore, "opensearch")],
)
async def test_search_backends_pass_vector_contract(store_type, dialect):
    client = FakeSearchClient()
    store = store_type(client=client, dimensions=2, index_name="contract-search")
    assert isinstance(store, AsyncStoreProtocol)
    assert await store.setup() is True
    assert await store.setup() is False

    report = await check_vector_store(store)
    assert report.contract == "vector_store"
    assert await store.count() == 0
    assert await store.ping() is True

    create = client.indices.created
    if dialect == "elasticsearch":
        mapping = create["mappings"]
        assert mapping["properties"]["embedding"] == {
            "type": "dense_vector",
            "dims": 2,
            "index": True,
            "similarity": "cosine",
        }
        search = next(kwargs for name, kwargs in client.calls if name == "search")
        assert search["source_exclude_vectors"] is False
        assert "source_includes" in search
        assert "operations" in next(
            kwargs for name, kwargs in client.calls if name == "bulk"
        )
    else:
        mapping = create["body"]["mappings"]
        vector = mapping["properties"]["embedding"]
        assert vector["method"] == {
            "name": "hnsw",
            "space_type": "cosinesimil",
            "engine": "lucene",
        }
        assert "body" in next(
            kwargs for name, kwargs in client.calls if name == "bulk"
        )
    assert mapping["dynamic_templates"][0]["metadata_strings"]["mapping"]["type"] == "keyword"


@pytest.mark.asyncio
async def test_search_store_replace_validation_and_external_client_lifecycle():
    client = FakeSearchClient()
    store = ElasticsearchStore(client=client, dimensions=2)
    await store.add("same", ["old"], [[1.0, 0.0]])
    await store.add("same", ["new-a", "new-b"], [[1.0, 0.0], [0.5, 0.5]])
    assert await store.count() == 2
    assert {hit["document"] for hit in await store.query([1.0, 0.0], top_k=2)} == {
        "new-a", "new-b"
    }

    with pytest.raises(ValueError, match="equal lengths"):
        await store.add("bad", ["one"], [])
    with pytest.raises(ValueError, match="dimension"):
        await store.query([1.0])
    with pytest.raises(ValueError, match="finite"):
        await store.query([float("nan"), 1.0])
    with pytest.raises(ValueError, match="non-zero"):
        await store.query([0.0, 0.0])
    with pytest.raises(ValueError, match="filter key"):
        await store.query([1.0, 0.0], where={"bad field": "value"})
    with pytest.raises(ValueError, match="only equality"):
        await store.query([1.0, 0.0], where={"kind": {"$gt": 3}})
    with pytest.raises(ValueError, match="top_k"):
        await store.query([1.0, 0.0], top_k=0)

    await store.aclose()
    assert client.closed is False


@pytest.mark.asyncio
async def test_owned_clients_are_closed(monkeypatch):
    client = FakeSearchClient()
    module = SimpleNamespace(AsyncElasticsearch=lambda *args, **kwargs: client)
    monkeypatch.setitem(__import__("sys").modules, "elasticsearch", module)
    store = ElasticsearchStore("http://search.invalid", dimensions=2)
    await store.aclose()
    assert client.closed is True


@pytest.mark.parametrize("store_type", [ElasticsearchStore, OpenSearchStore])
def test_search_store_constructor_rejects_unsafe_schema_parameters(store_type):
    with pytest.raises(ValueError, match="lowercase search index"):
        store_type(client=FakeSearchClient(), dimensions=2, index_name="Bad Index")
    with pytest.raises(ValueError, match="dimensions"):
        store_type(client=FakeSearchClient(), dimensions=0)
    with pytest.raises(ValueError, match="num_candidates"):
        store_type(client=FakeSearchClient(), dimensions=2, num_candidates=0)
