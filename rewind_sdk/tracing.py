"""OpenTelemetry wiring for Rewind.

Everything OTel is optional at import time. If the SDK is missing, or
REWIND_DISABLE_OTEL=1, the rest of rewind_sdk keeps working and falls back to
the local JSONL mirror. That keeps tests hermetic and the first-run experience
friendly on a fresh Codespace.
"""

from __future__ import annotations

import os

OTEL_AVAILABLE = True
try:  # pragma: no cover - depends on the environment
    from opentelemetry import metrics as _metrics
    from opentelemetry import trace as _trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
except Exception:  # pragma: no cover
    OTEL_AVAILABLE = False

_STATE = {"ready": False, "tracer": None, "counters": {}, "providers": {}}

# Loggers that carry replay envelopes and guardrail failures into SigNoz logs.
ENVELOPE_LOGGER = "rewind.envelope"
GUARDRAIL_LOGGER = "rewind.guardrail"


def disabled() -> bool:
    return os.getenv("REWIND_DISABLE_OTEL", "0") == "1" or not OTEL_AVAILABLE


def endpoint() -> str:
    return os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")


def setup(service_name: str = "rewind-demo-agent"):
    """Idempotently install trace, log and metric pipelines pointed at SigNoz."""
    if _STATE["ready"] or disabled():
        return _STATE

    base = endpoint()
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": os.getenv("REWIND_VERSION", "0.1.0"),
            "deployment.environment": os.getenv("REWIND_ENV", "dev"),
        }
    )

    # -- traces --------------------------------------------------------
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=base + "/v1/traces"))
    )
    _trace.set_tracer_provider(tracer_provider)

    # -- logs (this is where replay envelopes live) ---------------------
    import logging

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=base + "/v1/logs"))
    )
    set_logger_provider(logger_provider)
    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    for name in (ENVELOPE_LOGGER, GUARDRAIL_LOGGER):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False

    # -- metrics -------------------------------------------------------
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=base + "/v1/metrics"),
        export_interval_millis=int(os.getenv("REWIND_METRIC_INTERVAL_MS", "10000")),
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    _metrics.set_meter_provider(meter_provider)
    meter = meter_provider.get_meter("rewind")

    _STATE["counters"] = {
        "tokens": meter.create_counter(
            "gen_ai.client.token.usage", unit="token", description="LLM tokens used"
        ),
        "duration": meter.create_histogram(
            "agent.run.duration", unit="ms", description="End to end agent run duration"
        ),
        "cost": meter.create_counter(
            "agent.run.cost.usd", unit="USD", description="Estimated model spend"
        ),
        "tool_calls": meter.create_counter(
            "agent.tool.calls", unit="1", description="Tool invocations"
        ),
        "errors": meter.create_counter(
            "agent.run.errors", unit="1", description="Failed or guardrailed runs"
        ),
        "replays": meter.create_counter(
            "rewind.replays", unit="1", description="Replays executed, by override type"
        ),
    }
    _STATE["providers"] = {
        "tracer": tracer_provider,
        "logger": logger_provider,
        "meter": meter_provider,
    }
    _STATE["tracer"] = _trace.get_tracer("rewind")
    _STATE["ready"] = True
    return _STATE


def tracer():
    return _STATE["tracer"]


def counters() -> dict:
    return _STATE["counters"]


def record(name: str, value, attributes: dict = None) -> None:
    """Safe metric write - a broken metric must never break an agent run."""
    counter = _STATE["counters"].get(name)
    if counter is None:
        return
    try:
        if hasattr(counter, "add"):
            counter.add(value, attributes or {})
        else:
            counter.record(value, attributes or {})
    except Exception:
        pass


def flush() -> None:
    """Force-export everything. Short-lived CLI runs need this."""
    for provider in _STATE.get("providers", {}).values():
        try:
            provider.force_flush()
        except Exception:
            pass
