"""Privacy-first operational tracing primitives.

The package deliberately exposes a very small manual instrumentation surface.
It never auto-instruments HTTP clients/servers, captures request data, or
records exceptions.  Application code can only emit pre-approved metadata.
"""

from el.observability.telemetry import (
    SpanRecord,
    Telemetry,
    configure_otlp_from_environment,
    extract_traceparent,
    get_telemetry,
    parse_traceparent,
)
from el.observability.events import (
    ExecutionEnvelope,
    OperationalEvent,
    StructuredEventEmitter,
    configure_structured_stderr_logging,
    execution_envelope,
    get_event_emitter,
    install_provider_log_safety,
)

__all__ = [
    "SpanRecord",
    "Telemetry",
    "configure_otlp_from_environment",
    "extract_traceparent",
    "ExecutionEnvelope",
    "OperationalEvent",
    "StructuredEventEmitter",
    "configure_structured_stderr_logging",
    "execution_envelope",
    "get_event_emitter",
    "install_provider_log_safety",
    "get_telemetry",
    "parse_traceparent",
]
