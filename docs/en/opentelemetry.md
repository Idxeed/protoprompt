# OpenTelemetry

Install the SDK and OTLP/gRPC exporter:

```bash
pip install "protoprompt[otel]"
```

`OpenTelemetryEventSink` maps every typed context/retrieve/compress/profile/
recall/evict/cache event to a `protoprompt.<event>` span. It preserves the
event duration and opaque trace/scope correlation ids.

```python
from protoprompt.integrations import create_otlp_runtime

telemetry = create_otlp_runtime(
    service_name="legal-memory",
    endpoint="collector:4317",
    insecure=True,
)
builder = TokenBudgetedContextBuilder(
    store,
    embeddings,
    event_sink=telemetry.sink,
)
# on shutdown
telemetry.shutdown()
```

The helper owns an isolated `TracerProvider`; it does not replace the
application's global provider. If the host already configures OpenTelemetry,
construct `OpenTelemetryEventSink(existing_tracer)` instead.

## Safe defaults

Redaction runs inside the sink even when no `EventDispatcher` is used. Prompt,
message, document, profile, secret, credential, and raw/content-suffixed
attributes become `[REDACTED]`. Unknown complex objects are exported only as a
type or item count. Standard spans contain counts, timings, decisions, budget
numbers, and opaque hashes—not memory content.

## Jaeger collector example

```bash
docker compose -f docker-compose.observability.yml up -d
python examples/otel_tracing.py
```

Open <http://localhost:16686> and choose `protoprompt-demo`. The collector
accepts OTLP/gRPC on port 4317. The same sink can target Langfuse or another
OTLP endpoint through the standard endpoint and header settings; keep
authentication headers in environment variables or the host secret manager.

## Dashboard recipe

Use an OpenTelemetry Collector span-metrics connector (or equivalent backend
transform) and build these panels:

| Panel | Group/filter | Value |
|---|---|---|
| Context latency | span name `protoprompt.context` | p50/p95/p99 duration |
| Retrieval latency | `protoprompt.retrieve` + `channel` | p95 duration, hit count |
| Token pressure | context spans where `budgeted=true` | sum/avg `used_tokens`, ratio `used_tokens / budget` |
| Eviction rate | `protoprompt.evict` | spans/minute, grouped by action/kind |
| Cache effectiveness | `protoprompt.cache` | `hit_count / (hit_count + miss_count)` |

Do not promote `scope_id` to a metric label: it is useful for trace
correlation but creates high-cardinality time series.
