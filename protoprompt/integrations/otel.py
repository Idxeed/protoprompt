"""OpenTelemetry export for content-safe protoprompt events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
import time
from typing import Any

from protoprompt.events import (
    DEFAULT_REDACTION_POLICY,
    Event,
    RedactionPolicy,
)

_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


def _otel_api():
    try:
        from opentelemetry import trace
    except ImportError as exc:
        raise ImportError(
            "OpenTelemetry export requires the OpenTelemetry SDK. "
            "Install with: pip install 'protoprompt[otel]'"
        ) from exc
    return trace


class OpenTelemetryEventSink:
    """Turn each completed typed event into one bounded OTel span.

    The sink applies redaction itself, even when it is not wrapped in an
    ``EventDispatcher``. Unknown complex values are represented by type rather
    than stringified, preventing accidental content export through ``repr``.
    """

    def __init__(
        self,
        tracer: Any | None = None,
        *,
        redaction: RedactionPolicy = DEFAULT_REDACTION_POLICY,
    ) -> None:
        trace = _otel_api()
        self._tracer = tracer or trace.get_tracer("protoprompt")
        self._redaction = redaction

    def __call__(self, event: Event) -> None:
        event_name = (
            event.event_name if _LABEL.fullmatch(event.event_name) else "event"
        )
        action = event.action if _LABEL.fullmatch(event.action) else "other"
        safe = self._redaction.sanitize(dict(event.attributes))
        attributes: dict[str, Any] = {
            "protoprompt.event.name": event_name,
            "protoprompt.event.action": action,
        }
        if event.trace_id and _LABEL.fullmatch(event.trace_id):
            attributes["protoprompt.correlation.trace_id"] = event.trace_id
        if event.scope_id and _LABEL.fullmatch(event.scope_id):
            attributes["protoprompt.correlation.scope_id"] = event.scope_id
        if event.duration_ms is not None:
            attributes["protoprompt.duration_ms"] = max(0.0, event.duration_ms)
        _flatten_attributes("protoprompt.attribute", safe, attributes)

        end_time = time.time_ns()
        duration_ns = round(max(0.0, event.duration_ms or 0.0) * 1_000_000)
        span = self._tracer.start_span(
            f"protoprompt.{event_name}",
            start_time=end_time - duration_ns,
            attributes=attributes,
        )
        span.end(end_time=end_time)


@dataclass(slots=True)
class OpenTelemetryRuntime:
    """Objects owned by a configured OTLP exporter."""

    provider: Any
    sink: OpenTelemetryEventSink

    def shutdown(self) -> None:
        self.provider.shutdown()


def create_otlp_runtime(
    *,
    service_name: str,
    service_version: str = "",
    endpoint: str | None = None,
    insecure: bool | None = None,
    headers: Mapping[str, str] | None = None,
    redaction: RedactionPolicy = DEFAULT_REDACTION_POLICY,
) -> OpenTelemetryRuntime:
    """Create an isolated SDK provider and OTLP/gRPC batch exporter.

    The helper does not mutate OpenTelemetry's global tracer provider. When no
    endpoint is supplied, the official exporter reads standard ``OTEL_*``
    environment variables.
    """
    _otel_api()
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise ImportError(
            "OTLP export requires OpenTelemetry SDK and gRPC exporter. "
            "Install with: pip install 'protoprompt[otel]'"
        ) from exc

    resource_attributes = {"service.name": service_name}
    if service_version:
        resource_attributes["service.version"] = service_version
    provider = TracerProvider(resource=Resource.create(resource_attributes))
    exporter_options: dict[str, Any] = {}
    if endpoint is not None:
        exporter_options["endpoint"] = endpoint
    if insecure is not None:
        exporter_options["insecure"] = insecure
    if headers is not None:
        exporter_options["headers"] = dict(headers)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        **exporter_options
    )))
    sink = OpenTelemetryEventSink(
        provider.get_tracer("protoprompt"),
        redaction=redaction,
    )
    return OpenTelemetryRuntime(provider=provider, sink=sink)


def _flatten_attributes(
    prefix: str,
    value: Any,
    output: dict[str, Any],
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))[:80]
            _flatten_attributes(f"{prefix}.{normalized}", child, output)
        return
    if value is None:
        return
    if isinstance(value, (str, bool, int, float)):
        output[prefix] = value
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if all(isinstance(item, (str, bool, int, float)) for item in value):
            output[prefix] = list(value)
        else:
            output[f"{prefix}.item_count"] = len(value)
        return
    output[f"{prefix}.value_type"] = type(value).__name__
