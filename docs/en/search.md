# Elasticsearch and OpenSearch

`ElasticsearchStore` and `OpenSearchStore` implement the same async vector-store
contract as the core stores. They keep protoprompt's replace-on-add behavior,
metadata filters, cosine score threshold, and source `doc_id` metadata.

This adapter is dense-vector retrieval only. Sparse+dense hybrid retrieval stays
in the research backlog until it has a stable cross-backend contract.

## Install and create the index

```bash
pip install "protoprompt[elasticsearch]"  # Elasticsearch 9.x client
# or
pip install "protoprompt[opensearch]"     # OpenSearch 3.1 async client
```

Constructors do not mutate server schema. Call `setup()` explicitly during a
deployment or migration:

```python
from protoprompt.integrations import ElasticsearchStore

store = ElasticsearchStore(
    "https://search.example.com:9200",
    index_name="protoprompt-memory-v1",
    dimensions=1536,
    api_key="...",  # forwarded to the official client
)
await store.setup()
```

OpenSearch accepts its official client options in the same way. For AWS request
signing, build an `AsyncOpenSearch` client with the approved signer and inject it
with `client=`. The host owns injected clients; `close()` only closes clients the
adapter created.

String metadata is mapped to `keyword`, so equality and `$in` keep exact values.
OpenSearch uses Lucene HNSW because inline k-NN filtering is supported by that
engine. Both adapters recalculate cosine similarity from the stored vector so
`score_threshold` means the same thing on both servers.

## Local live test

The compose file disables authentication and is **test-only**:

```bash
docker compose -f docker-compose.search.yml up -d --wait
PROTOPROMPT_ELASTICSEARCH_URL=http://localhost:9200 \
PROTOPROMPT_OPENSEARCH_URL=http://localhost:9201 \
pytest -m integration tests/integration/test_search_live.py
docker compose -f docker-compose.search.yml down
```

Run the example with `python examples/search_vector_store.py`. Set
`SEARCH_BACKEND=opensearch` to use port `9201`.

## Migration and rollback

Create a versioned index, populate it through `DocumentIndexer`, compare counts
and sampled queries, then switch the application configuration. Do not point the
adapter at an existing index with an incompatible vector dimension.

Rollback is a configuration switch to the previous index. Keep the old index
read-only until the observation window ends; deleting it is a separate operator
decision. `setup()` never modifies an existing mapping.

Supported client lines are declared by the extras. Dependency updates require
the contract suite plus both opt-in live tests. An incompatible server line gets
a new major extra constraint or adapter dialect instead of silent behavior
changes.
