# OpenTelemetry

Установите SDK и OTLP/gRPC exporter:

```bash
pip install "protoprompt[otel]"
```

`OpenTelemetryEventSink` превращает каждое типизированное событие
context/retrieve/compress/profile/recall/evict/cache в span
`protoprompt.<event>`, сохраняя длительность и непрозрачные trace/scope id.

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
# при завершении приложения
telemetry.shutdown()
```

Helper владеет изолированным `TracerProvider` и не заменяет глобальный provider
приложения. Если хост уже настроил OpenTelemetry, передайте существующий tracer
в `OpenTelemetryEventSink(existing_tracer)`.

## Безопасные defaults

Redaction выполняется внутри sink даже без `EventDispatcher`. Атрибуты prompt,
message, document, profile, secret, credential, а также raw/content-suffix
заменяются на `[REDACTED]`. Для неизвестных сложных объектов экспортируется
только тип или число элементов. Обычные spans содержат счётчики, длительности,
решения, токен-бюджет и непрозрачные hashes, но не содержимое памяти.

## Пример с Jaeger collector

```bash
docker compose -f docker-compose.observability.yml up -d
python examples/otel_tracing.py
```

Откройте <http://localhost:16686> и выберите `protoprompt-demo`. Collector
принимает OTLP/gRPC на порту 4317. Тот же sink можно направить в Langfuse или
другой OTLP endpoint стандартными настройками endpoint/headers; auth headers
держите в environment или secret manager хоста.

## Рецепт dashboard

Подключите span-metrics connector OpenTelemetry Collector или аналогичное
преобразование backend и соберите панели:

| Панель | Группа/фильтр | Значение |
|---|---|---|
| Context latency | span `protoprompt.context` | p50/p95/p99 duration |
| Retrieval latency | `protoprompt.retrieve` + `channel` | p95 duration, hit count |
| Token pressure | context spans с `budgeted=true` | sum/avg `used_tokens`, отношение `used_tokens / budget` |
| Eviction rate | `protoprompt.evict` | spans/minute по action/kind |
| Cache effectiveness | `protoprompt.cache` | `hit_count / (hit_count + miss_count)` |

Не превращайте `scope_id` в label метрики: он полезен для связи traces, но
создаёт высокую cardinality временных рядов.
