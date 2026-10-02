"""Small, safe OpenTelemetry facade for FitCheck operational traces.

The facade is intentionally manual and metadata-only.  It uses W3C
``traceparent`` for propagation, rejects baggage, and refuses arbitrary span
names, attributes, events, exception details, or status descriptions.  The
default singleton is disabled unless both telemetry and an OTLP exporter are
explicitly configured.  Disabled telemetry neither initializes an exporter
nor discovers Google credentials.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final

from opentelemetry import trace


_TRACEPARENT_RE: Final = re.compile(
    r"^(?P<version>[0-9a-f]{2})-"
    r"(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<parent_id>[0-9a-f]{16})-"
    r"(?P<flags>[0-9a-f]{2})$"
)
_ALLOWED_SPANS: Final = frozenset(
    {
        "fitcheck.source.accept",
        "fitcheck.source.enqueue",
        "fitcheck.source.worker_attempt",
        "fitcheck.source.model_boundary",
        "fitcheck.source.mechanical_result",
        "fitcheck.source.persisted_outcome",
        "fitcheck.mcp.invoke",
        "fitcheck.normalization.propose",
        "fitcheck.normalization.decision",
        "fitcheck.retrieval.fetch",
        "fitcheck.market_pool.pair_fit",
        "fitcheck.market_pool.displayed_pool",
    }
)
_ALLOWED_ATTRIBUTES: Final = {
    "fitcheck.source.context": frozenset({"absent", "accepted", "invalid"}),
    "fitcheck.source.stage": frozenset(
        {
            "accepted",
            "enqueued",
            "attempt",
            "model_boundary",
            "mechanical_result",
            "persisted_outcome",
        }
    ),
    "fitcheck.source.result": frozenset(
        {
            "queued",
            "reused",
            "succeeded",
            "failed",
            "needs_operator",
            "stale",
            "refusal",
            "candidates",
            "invalid",
        }
    ),
    "fitcheck.source.external_effect": frozenset({True, False}),
    "fitcheck.operation": frozenset(
        {
            "mcp",
            "normalization",
            "retrieval",
            "pair_fit",
            "displayed_pool",
        }
    ),
    "fitcheck.outcome": frozenset(
        {
            "succeeded",
            "failed",
            "conflict",
            "not_found",
            "refusal",
            "clarification",
            "no_clean_expression",
            "incomplete",
        }
    ),
}
_CURRENT_TRACEPARENT: ContextVar[str | None] = ContextVar(
    "fitcheck_safe_traceparent", default=None
)


@dataclass(frozen=True)
class SpanRecord:
    """A test-only observation with no content-bearing fields."""

    name: str
    traceparent: str
    parent_traceparent: str | None
    attributes: dict[str, str | bool]


def parse_traceparent(value: str | None) -> str | None:
    """Return a canonical W3C traceparent or ``None`` without logging input."""

    if value is None:
        return None
    candidate = value.strip().lower()
    match = _TRACEPARENT_RE.fullmatch(candidate)
    if match is None:
        return None
    parts = match.groupdict()
    if (
        parts["version"] != "00"
        or set(parts["trace_id"]) == {"0"}
        or set(parts["parent_id"]) == {"0"}
    ):
        return None
    return candidate


def extract_traceparent(headers: Mapping[str, str]) -> str | None:
    """Extract only the W3C traceparent header; baggage is intentionally ignored."""

    return parse_traceparent(headers.get("traceparent"))


class _DisabledSpan:
    def __enter__(self) -> "_DisabledSpan":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def set_attributes(self, attributes: Mapping[str, str | bool]) -> None:
        return None


class _SafeSpan:
    def __init__(
        self,
        telemetry: "Telemetry",
        name: str,
        parent_traceparent: str | None,
    ):
        self._telemetry = telemetry
        self._name = name
        self._parent_traceparent = parent_traceparent
        self._traceparent: str | None = None
        self._attributes: dict[str, str | bool] = {}
        self._context_token = None
        self._otel_span_cm = None

    def __enter__(self) -> "_SafeSpan":
        parent = self._parent_traceparent or _CURRENT_TRACEPARENT.get()
        self._parent_traceparent = parent
        parsed_parent = parse_traceparent(parent)
        if parsed_parent is None:
            trace_id = secrets.token_hex(16)
            flags = "01"
        else:
            _, trace_id, _, flags = parsed_parent.split("-")
        self._traceparent = f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"

        # Use only this facade's explicitly-owned provider. Falling back to a
        # process-global provider could silently duplicate external export or
        # make our bounded shutdown target a different provider.
        if self._telemetry._tracer is not None:
            try:
                parent_context = self._otel_parent_context(parsed_parent)
                self._otel_span_cm = (
                    self._telemetry._tracer.start_as_current_span(
                        self._name, context=parent_context
                    )
                )
                self._otel_span_cm.__enter__()
                span_context = trace.get_current_span().get_span_context()
                if span_context.is_valid:
                    self._traceparent = (
                        f"00-{span_context.trace_id:032x}-{span_context.span_id:016x}-"
                        f"{int(span_context.trace_flags):02x}"
                    )
            except Exception:
                # Telemetry must not affect acceptance, durable persistence, or
                # external-effect decisions. Do not record a possibly sensitive
                # exporter/configuration error.
                self._otel_span_cm = None
        self._context_token = _CURRENT_TRACEPARENT.set(self._traceparent)
        return self

    @staticmethod
    def _otel_parent_context(parent: str | None):
        if parent is None:
            return None
        _, trace_id, parent_id, flags = parent.split("-")
        parent_span_context = trace.SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(parent_id, 16),
            is_remote=True,
            trace_flags=trace.TraceFlags(int(flags, 16)),
            trace_state=trace.TraceState(),
        )
        return trace.set_span_in_context(trace.NonRecordingSpan(parent_span_context))

    def set_attributes(self, attributes: Mapping[str, str | bool]) -> None:
        approved = {
            key: value
            for key, value in attributes.items()
            if key in _ALLOWED_ATTRIBUTES and value in _ALLOWED_ATTRIBUTES[key]
        }
        self._attributes.update(approved)
        if approved and self._otel_span_cm is not None:
            try:
                trace.get_current_span().set_attributes(approved)
            except Exception:
                # As above, instrumentation failure remains non-interfering.
                pass

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if self._otel_span_cm is not None:
                # Never pass exception details to OTel; callers represent only
                # a bounded, safe outcome attribute.
                self._otel_span_cm.__exit__(None, None, None)
        except Exception:
            pass
        finally:
            if self._context_token is not None:
                _CURRENT_TRACEPARENT.reset(self._context_token)
            if self._telemetry._record_span is not None and self._traceparent:
                try:
                    self._telemetry._record_span(
                        SpanRecord(
                            name=self._name,
                            traceparent=self._traceparent,
                            parent_traceparent=self._parent_traceparent,
                            attributes=dict(self._attributes),
                        )
                    )
                except Exception:
                    # A full local recorder/export queue cannot change a
                    # product result or provoke another provider attempt.
                    pass
        return False


class Telemetry:
    """Safe manual tracing; no exporter or credential initialization here."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        record_span: Callable[[SpanRecord], None] | None = None,
        provider: object | None = None,
        tracer: Any | None = None,
    ):
        self._enabled = enabled
        self._record_span = record_span
        self._provider = provider
        self._tracer = tracer
        self._shutdown_started = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def span(
        self, name: str, *, parent_traceparent: str | None = None
    ) -> _SafeSpan | _DisabledSpan:
        if not self._enabled or name not in _ALLOWED_SPANS:
            return _DisabledSpan()
        return _SafeSpan(self, name, parse_traceparent(parent_traceparent))

    def shutdown(self, *, timeout_seconds: float = 2.5) -> bool:
        """Best-effort bounded exporter teardown without product interference.

        Product requests and workers never wait for this method.  A daemon
        thread prevents an exporter bug or blocked network from holding
        process shutdown open; the direct OTLP exporter is additionally
        configured with a two-second export timeout.
        """

        if self._provider is None or self._shutdown_started:
            return True
        if timeout_seconds <= 0:
            return False
        self._shutdown_started = True

        def _close_provider() -> None:
            try:
                self._provider.force_flush(timeout_millis=2_000)
            except Exception:
                pass
            try:
                self._provider.shutdown()
            except Exception:
                pass

        thread = threading.Thread(target=_close_provider, daemon=True)
        try:
            thread.start()
            thread.join(timeout_seconds)
            return not thread.is_alive()
        except Exception:
            return False

    @staticmethod
    def current_traceparent() -> str | None:
        return _CURRENT_TRACEPARENT.get()


_telemetry: Telemetry | None = None


def configure_otlp_from_environment() -> Telemetry:
    """Configure bounded direct OTLP export only after explicit opt-in.

    This is intentionally a direct OTLP endpoint, not a cloud-specific
    exporter, and never performs ADC discovery.  It is never called while
    telemetry is disabled or unconfigured.  Failure returns disabled
    telemetry without exposing endpoint or credential details.
    """

    if (
        os.environ.get("FITCHECK_TELEMETRY_ENABLED") != "1"
        or os.environ.get("FITCHECK_TELEMETRY_EXPORTER") != "otlp"
    ):
        return Telemetry()
    endpoint = os.environ.get("FITCHECK_TELEMETRY_OTLP_ENDPOINT")
    if not endpoint or len(endpoint) > 2_048:
        return Telemetry()
    # The upstream exporter would otherwise consult these standard variables
    # when no headers argument is provided. FitCheck has no approved generic
    # authorization-header channel, so any such configuration fails closed.
    if any(
        os.environ.get(name)
        for name in (
            "OTEL_EXPORTER_OTLP_HEADERS",
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        )
    ):
        return Telemetry()
    # Never replace or share an externally installed provider. That would
    # create uncoordinated exporters and make shutdown ownership unclear.
    if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        return Telemetry()
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": "fitcheck"})
        )
        exporter = OTLPSpanExporter(endpoint=endpoint, timeout=2)
        provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=2_048,
                max_export_batch_size=256,
                schedule_delay_millis=5_000,
                export_timeout_millis=2_000,
            )
        )
    except Exception:
        return Telemetry()
    return Telemetry(
        enabled=True,
        provider=provider,
        tracer=provider.get_tracer("fitcheck.observability"),
    )


def get_telemetry() -> Telemetry:
    """Return the process-local telemetry singleton, disabled by default."""

    global _telemetry
    if _telemetry is None:
        _telemetry = configure_otlp_from_environment()
    return _telemetry
