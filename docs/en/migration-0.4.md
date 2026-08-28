# Migrating from 0.3 to 0.4

The 0.4 line preserves the public 0.3 call shapes. A composite client with
`chat()` and `embed()` is still accepted everywhere, and data written without
a scope remains available under its original `doc_id`.

## Separate chat and embeddings

Existing code does not need to change:

```python
pipeline = Pipeline(store, llm)
```

When providers differ, pass the two capabilities explicitly:

```python
pipeline = Pipeline(
    store,
    chat_client=chat_client,
    embedding_client=embedding_client,
)
```

`LLMClientProtocol` remains the composite of `ChatClientProtocol` and
`EmbeddingClientProtocol`. `CompositeLLMClient` can assemble one explicitly.

## Enabling MemoryScope

Physical keys do not change when `scope` is omitted. For an incremental
migration, create scoped components only for new tenants or threads:

```python
scope = MemoryScope(tenant="acme", user="u-42", thread="chat-7")
indexer = DocumentIndexer(store, embedding_client, scope=scope)
builder = ContextBuilder(store, embedding_client, scope=scope)
```

Records created without a scope are not copied into the new namespace
automatically. To migrate existing memory, read it through an unscoped object
and re-index it through a scoped indexer. Do not pair a scoped writer with an
unscoped reader: no scope deliberately means the legacy/global namespace.

For direct `StoreProtocol` deletion, use `scoped_doc_id(logical_id, scope)`.
High-level components map IDs automatically.

## Checking a custom adapter

The contract kit does not depend on pytest:

```python
from protoprompt.testing import check_embedding_client, check_vector_store

await check_embedding_client(my_embedding_client)
await check_vector_store(my_isolated_test_store)
```

Checks use temporary keys and clean them up. A dedicated test collection or
schema is still recommended for a production backend.
