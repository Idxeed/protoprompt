"""Elasticsearch and OpenSearch vector-store adapters."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import re
from typing import Any

_FILTER_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")


class _SearchVectorStore:
    def __init__(
        self,
        client: Any,
        *,
        index_name: str,
        dimensions: int,
        dialect: str,
        owned_client: bool,
        num_candidates: int = 100,
    ) -> None:
        if not index_name or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", index_name):
            raise ValueError("index_name must be a non-empty lowercase search index name")
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        if num_candidates < 1:
            raise ValueError("num_candidates must be positive")
        self._client = client
        self.index_name = index_name
        self.dimensions = dimensions
        self.dialect = dialect
        self._owned_client = owned_client
        self.num_candidates = num_candidates

    @property
    def client(self) -> Any:
        """Underlying official client, primarily for health and admin operations."""

        return self._client

    async def setup(self) -> bool:
        """Create the vector index if absent; constructors never mutate schema."""

        exists = await _call(self._client.indices.exists, index=self.index_name)
        if bool(exists):
            return False
        if self.dialect == "elasticsearch":
            await _call(
                self._client.indices.create,
                index=self.index_name,
                settings={"index": {"number_of_shards": 1}},
                mappings={
                    "dynamic": True,
                    "dynamic_templates": [{
                        "metadata_strings": {
                            "path_match": "metadata.*",
                            "match_mapping_type": "string",
                            "mapping": {"type": "keyword", "ignore_above": 1024},
                        }
                    }],
                    "properties": {
                        "doc_id": {"type": "keyword"},
                        "chunk_index": {"type": "integer"},
                        "document": {"type": "text"},
                        "embedding": {
                            "type": "dense_vector",
                            "dims": self.dimensions,
                            "index": True,
                            "similarity": "cosine",
                        },
                        "metadata": {"type": "object", "dynamic": True},
                    },
                },
            )
        else:
            await _call(
                self._client.indices.create,
                index=self.index_name,
                body={
                    "settings": {"index": {"knn": True, "number_of_shards": 1}},
                    "mappings": {
                        "dynamic": True,
                        "dynamic_templates": [{
                            "metadata_strings": {
                                "path_match": "metadata.*",
                                "match_mapping_type": "string",
                                "mapping": {"type": "keyword", "ignore_above": 1024},
                            }
                        }],
                        "properties": {
                            "doc_id": {"type": "keyword"},
                            "chunk_index": {"type": "integer"},
                            "document": {"type": "text"},
                            "embedding": {
                                "type": "knn_vector",
                                "dimension": self.dimensions,
                                "method": {
                                    "name": "hnsw",
                                    "space_type": "cosinesimil",
                                    "engine": "lucene",
                                },
                            },
                            "metadata": {"type": "object", "dynamic": True},
                        },
                    },
                },
            )
        return True

    async def add(
        self,
        doc_id: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict | None = None,
    ) -> None:
        if not doc_id:
            raise ValueError("doc_id must not be empty")
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must have equal lengths")
        vectors = [self._validate_vector(vector) for vector in embeddings]
        await self.delete(doc_id)
        if not chunks:
            return
        operations: list[dict[str, Any]] = []
        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
            identity = _chunk_id(doc_id, index)
            source = {
                "doc_id": doc_id,
                "chunk_index": index,
                "document": chunk,
                "embedding": vector,
                "metadata": {**(metadata or {}), "chunk_index": index, "doc_id": doc_id},
            }
            operations.extend([
                {"index": {"_index": self.index_name, "_id": identity}},
                source,
            ])
        kwargs: dict[str, Any] = {"refresh": "wait_for"}
        if self.dialect == "elasticsearch":
            kwargs["operations"] = operations
        else:
            kwargs["body"] = operations
        response = await _call(self._client.bulk, **kwargs)
        if response.get("errors"):
            failures = [
                item for item in response.get("items", [])
                if int(next(iter(item.values())).get("status", 500)) >= 300
            ]
            raise RuntimeError(f"vector bulk indexing failed for {len(failures)} chunks")

    async def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        where: dict | None = None,
        score_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        query_vector = self._validate_vector(embedding)
        filters = _filters(where or {})
        candidates = max(top_k, self.num_candidates)
        if self.dialect == "elasticsearch":
            knn: dict[str, Any] = {
                "field": "embedding",
                "query_vector": query_vector,
                "k": top_k,
                "num_candidates": candidates,
            }
            if filters:
                knn["filter"] = filters[0] if len(filters) == 1 else {"bool": {"filter": filters}}
            response = await _call(
                self._client.search,
                index=self.index_name,
                knn=knn,
                size=top_k,
                source_includes=[
                    "doc_id", "chunk_index", "document", "embedding", "metadata"
                ],
                # Elasticsearch 9 excludes dense vectors from _source by default.
                # We need the original vector to expose one consistent cosine score
                # across Elasticsearch and OpenSearch.
                source_exclude_vectors=False,
            )
        else:
            knn_payload: dict[str, Any] = {"vector": query_vector, "k": top_k}
            if filters:
                knn_payload["filter"] = {"bool": {"filter": filters}}
            response = await _call(
                self._client.search,
                index=self.index_name,
                body={
                    "size": top_k,
                    "_source": ["doc_id", "chunk_index", "document", "embedding", "metadata"],
                    "query": {"knn": {"embedding": knn_payload}},
                },
            )

        output: list[dict[str, Any]] = []
        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            vector = source.get("embedding") or []
            score = _cosine_similarity(query_vector, [float(value) for value in vector])
            if score_threshold is not None and score < score_threshold:
                continue
            output.append({
                "id": str(hit.get("_id", "")),
                "document": str(source.get("document", "")),
                "embedding": vector,
                "metadata": dict(source.get("metadata") or {}),
                "score": score,
                "distance": 1.0 - score,
            })
        output.sort(key=lambda item: item["score"], reverse=True)
        return output[:top_k]

    async def delete(self, doc_id: str) -> None:
        query = {"term": {"doc_id": doc_id}}
        kwargs: dict[str, Any] = {
            "index": self.index_name,
            "conflicts": "proceed",
            "refresh": True,
        }
        if self.dialect == "elasticsearch":
            kwargs["query"] = query
        else:
            kwargs["body"] = {"query": query}
        await _call(self._client.delete_by_query, **kwargs)

    async def count(self) -> int:
        kwargs: dict[str, Any] = {"index": self.index_name}
        if self.dialect == "elasticsearch":
            kwargs["query"] = {"match_all": {}}
        else:
            kwargs["body"] = {"query": {"match_all": {}}}
        response = await _call(self._client.count, **kwargs)
        return int(response.get("count", 0))

    async def ping(self) -> bool:
        return bool(await _call(self._client.ping))

    async def close(self) -> None:
        """Close a client created by this adapter; injected clients stay host-owned."""

        if not self._owned_client:
            return
        close = getattr(self._client, "close", None)
        if close is not None:
            await _call(close)

    async def aclose(self) -> None:
        """Compatibility alias for async-resource naming conventions."""

        await self.close()

    def _validate_vector(self, vector: list[float]) -> list[float]:
        if len(vector) != self.dimensions:
            raise ValueError(
                f"embedding dimension {len(vector)} does not match {self.dimensions}"
            )
        normalized = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in normalized):
            raise ValueError("embedding values must be finite")
        if not any(value != 0.0 for value in normalized):
            raise ValueError("cosine embeddings must have non-zero magnitude")
        return normalized


class ElasticsearchStore(_SearchVectorStore):
    """Async Elasticsearch 9.x dense-vector store."""

    def __init__(
        self,
        hosts: str | list[str] | None = None,
        *,
        index_name: str = "protoprompt-memory",
        dimensions: int,
        client: Any | None = None,
        num_candidates: int = 100,
        **client_options: Any,
    ) -> None:
        owned = client is None
        if client is None:
            try:
                from elasticsearch import AsyncElasticsearch
            except ImportError as exc:
                raise ImportError(
                    "ElasticsearchStore requires elasticsearch[async]. "
                    "Install with: pip install 'protoprompt[elasticsearch]'"
                ) from exc
            client = AsyncElasticsearch(hosts, **client_options)
        super().__init__(
            client,
            index_name=index_name,
            dimensions=dimensions,
            dialect="elasticsearch",
            owned_client=owned,
            num_candidates=num_candidates,
        )


class OpenSearchStore(_SearchVectorStore):
    """Async OpenSearch 3.1 k-NN vector store."""

    def __init__(
        self,
        hosts: Any = None,
        *,
        index_name: str = "protoprompt-memory",
        dimensions: int,
        client: Any | None = None,
        num_candidates: int = 100,
        **client_options: Any,
    ) -> None:
        owned = client is None
        if client is None:
            try:
                from opensearchpy import AsyncOpenSearch
            except ImportError as exc:
                raise ImportError(
                    "OpenSearchStore requires opensearch-py[async]. "
                    "Install with: pip install 'protoprompt[opensearch]'"
                ) from exc
            client = AsyncOpenSearch(hosts=hosts, **client_options)
        super().__init__(
            client,
            index_name=index_name,
            dimensions=dimensions,
            dialect="opensearch",
            owned_client=owned,
            num_candidates=num_candidates,
        )


async def _call(method: Any, /, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(method):
        return await method(**kwargs)
    result = await asyncio.to_thread(method, **kwargs)
    # Official SDK methods may be decorator-wrapped: inspect sees a regular
    # callable even though invoking it returns an awaitable.
    if inspect.isawaitable(result):
        return await result
    return result


def _filters(where: dict[str, Any]) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    for key, condition in where.items():
        if not _FILTER_KEY.fullmatch(key):
            raise ValueError(f"invalid metadata filter key: {key!r}")
        field = f"metadata.{key}"
        if isinstance(condition, dict):
            if set(condition) != {"$in"} or not isinstance(condition["$in"], list):
                raise ValueError("metadata filters support only equality and {'$in': [...]} ")
            filters.append({"terms": {field: condition["$in"]}})
        else:
            filters.append({"term": {field: condition}})
    return filters


def _chunk_id(doc_id: str, index: int) -> str:
    digest = hashlib.blake2b(doc_id.encode("utf-8"), digest_size=16).hexdigest()
    return f"{digest}:{index}"


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
