from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from protoprompt import ContextEvent, EventDispatcher
from protoprompt.integrations.otel import OpenTelemetryEventSink


def _sink():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return OpenTelemetryEventSink(provider.get_tracer("test")), exporter, provider


def test_otel_sink_exports_typed_duration_and_safe_attributes():
    sink, exporter, provider = _sink()
    sink(ContextEvent(
        action="completed",
        trace_id="a" * 32,
        scope_id="b" * 24,
        duration_ms=12.5,
        attributes={
            "token_count": 17,
            "nested": {"hit_count": 2},
            "prompt": "private exact",
            "raw_prompt": "private suffix",
            "content_blocks": [{"text": "private nested"}],
        },
    ))

    span = exporter.get_finished_spans()[0]
    assert span.name == "protoprompt.context"
    assert span.attributes["protoprompt.event.action"] == "completed"
    assert span.attributes["protoprompt.duration_ms"] == 12.5
    assert span.attributes["protoprompt.attribute.token_count"] == 17
    assert span.attributes["protoprompt.attribute.nested.hit_count"] == 2
    assert span.attributes["protoprompt.attribute.prompt"] == "[REDACTED]"
    assert span.attributes["protoprompt.attribute.raw_prompt"] == "[REDACTED]"
    rendered = str(span.attributes)
    assert "private exact" not in rendered
    assert "private suffix" not in rendered
    assert "private nested" not in rendered
    assert (span.end_time - span.start_time) / 1_000_000 == 12.5
    provider.shutdown()


def test_dispatcher_and_otel_sink_remain_deny_by_default():
    sink, exporter, provider = _sink()
    dispatcher = EventDispatcher(sink)
    dispatcher.emit(ContextEvent(
        action="completed",
        attributes={
            "request_content": "secret body",
            "profile_used": True,
        },
    ))
    attributes = exporter.get_finished_spans()[0].attributes
    assert attributes["protoprompt.attribute.request_content"] == "[REDACTED]"
    assert attributes["protoprompt.attribute.profile_used"] is True
    provider.shutdown()
