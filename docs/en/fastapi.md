# FastAPI memory service

The `[fastapi]` extra exposes the scoped `MemoryService` as a small HTTP API.
Authentication, scope resolution, and service construction are mandatory host
callbacks; tenant, user, and thread never appear in request bodies.

```bash
pip install "protoprompt[fastapi]"
export PROTOPROMPT_API_KEY='replace-with-a-random-test-key'
python examples/fastapi_memory_service.py
```

```bash
curl -H "Authorization: Bearer $PROTOPROMPT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"memory_id":"renewal","text":"The contract renews in May"}' \
  http://localhost:8000/v1/memories

curl -H "Authorization: Bearer $PROTOPROMPT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"When does the contract renew?"}' \
  http://localhost:8000/v1/memories/search
```

The example is intentionally offline and uses deterministic demo embeddings.
Replace them and SQLite with provider embeddings and an async production store.
The SQLite instance is wrapped with `as_async`, so disk I/O does not block the
event loop.

## Security boundary

`create_fastapi_memory_app(service_factory, scope_resolver, authorize)` has no
default that trusts `X-Tenant` or user input. In production, `authorize` should
verify a JWT/session/mTLS identity and place trusted claims on `request.state`;
`scope_resolver` maps only those claims to `MemoryScope`. The adapter verifies
that the returned `MemoryService` is pinned to exactly that scope.

Requests reject unknown fields, bound text, identifiers, `top_k`, metadata size,
and score thresholds. Set an HTTP body-size limit and request timeout in the
reverse proxy too. `/healthz` contains no backend or tenant details and is the
only unauthenticated route. Explain responses omit recalled text.

The API includes remember/search/forget, manifest/explain/budget report, and
optional profile endpoints. A profile signal returns `409` when the host did not
configure a `ProfileManager`. When supplied, its host-owned scope must exactly
match the `MemoryService` scope; construction fails before profile I/O when it
does not.

## Lifespan and deployment

Pass a FastAPI lifespan context to open connection pools before traffic and
close only host-owned resources on shutdown. The included local example closes
SQLite in lifespan.

The Kubernetes recipe is deliberately single-replica because it uses SQLite:

```bash
docker build -f examples/fastapi/Dockerfile -t protoprompt-fastapi:local .
minikube image load protoprompt-fastapi:local
kubectl create secret generic protoprompt-api \
  --from-literal=api-key='replace-with-a-random-test-key'
kubectl apply -f examples/fastapi/k8s.yaml
kubectl port-forward service/protoprompt-memory 8000:80
```

For multiple replicas, replace SQLite with PostgreSQL/pgvector and use a shared
profile/session backend. Never scale this manifest horizontally over a
ReadWriteOnce SQLite volume.

## Migration and rollback

Mount the HTTP app beside an existing process first and route a small authorized
cohort to it. Scope mapping must be compared against the current auth claims
before any memory is copied. Rollback removes the route and returns traffic to
the in-process adapter; the backing store remains unchanged.

FastAPI stays an optional recipe rather than a required core server. Updating its
version range requires the ASGI isolation, validation, lifespan, and missing-extra
tests. Breaking route changes require a new `/vN` prefix.
