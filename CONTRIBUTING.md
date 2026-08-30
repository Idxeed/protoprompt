# Contributing integrations

`protoprompt` keeps its core dependency-free. A new provider or backend belongs
in a lazy module, has an explicit extra in `pyproject.toml`, and must not be
imported while importing the top-level package.

## Adapter checklist

1. Implement the narrowest public protocol: `ChatClientProtocol`,
   `EmbeddingClientProtocol`, `StoreProtocol`, `ProfileStore`, or `SecretStore`.
2. Import third-party packages inside the adapter constructor. On a missing
   dependency, raise an `ImportError` that names the exact install command, for
   example `pip install "protoprompt[qdrant]"`.
3. Export the adapter lazily from `protoprompt.integrations` when applicable.
4. Run the matching function from `protoprompt.testing` against the adapter.
   Network clients should use a fake/recorded transport in the default suite;
   live tests must be explicitly marked `integration`.
5. Add a runnable example, RU/EN documentation, migration/removal notes, and a
   changelog entry.

```python
from protoprompt.testing import check_embedding_client, check_vector_store

await check_embedding_client(client)
await check_vector_store(store, embedding_a=vector_a, embedding_b=vector_b)
```

Use an isolated collection/schema when running a store contract. It creates
two temporary documents and deletes them in `finally`, but isolation also
protects unrelated data from backend-specific behaviour.

## Required local checks

```bash
python -m pytest -m "not integration" --disable-socket \
  --allow-unix-socket --allow-hosts=127.0.0.1,::1,localhost
python -m pytest -m integration
python -m pytest -q tests/test_ledger_property_conformance_sqlite.py \
  tests/test_ledger_recall_property_sqlite.py
python -m mkdocs build --strict -f mkdocs.ru.yml
python -m mkdocs build --strict -f mkdocs.en.yml
python -m build --sdist --wheel
```

An adapter is ready only when imports without its extra still work, missing
dependency errors are actionable, sync/async semantics are documented, scope
isolation is tested where relevant, and telemetry never exports content or
credentials by default.
