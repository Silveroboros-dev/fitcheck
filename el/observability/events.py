"""Bounded JSON operational events, separate from traces and eval evidence.

Events intentionally carry only an allowlisted operation envelope. They never
accept source content, URLs, headers, prompts, model output, digests, user
metadata, exception text, or arbitrary extra fields. They are operational
observations, not review or evaluation evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Final, Literal, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


EVENT_SCHEMA_VERSION: Final = "fitcheck.operational_event.v1"
_LOGGER_NAME: Final = "fitcheck.operational"
_HANDLER_MARKER: Final = "fitcheck_structured_stderr_v1"
_DISABLED_HANDLER_MARKER: Final = "fitcheck_structured_disabled_v1"
_TRACEPARENT: Final = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)

EventName = Literal[
    "fitcheck.source.execution_envelope",
    "fitcheck.source.task_outcome",
    "fitcheck.mcp.operation",
    "fitcheck.normalization.operation",
    "fitcheck.retrieval.operation",
    "fitcheck.market_pool.operation",
    "fitcheck.retrieval.provider_failure",
    "fitcheck.retrieval.provider_cache_hit",
]
Operation = Literal[
    "source_interpretation",
    "mcp",
    "normalization",
    "retrieval",
    "pair_fit",
    "displayed_pool",
]
Stage = Literal[
    "accepted",
    "enqueued",
    "attempt",
    "model_boundary",
    "mechanical_result",
    "persisted_outcome",
    "provider",
    "operation",
]
TransportOutcome = Literal[
    "not_applicable",
    "accepted",
    "reused",
    "read_succeeded",
    "failed",
    "unknown",
]
EffectOutcome = Literal[
    "not_applicable",
    "not_started",
    "started",
    "succeeded",
    "uncertain",
    "unknown",
]
CaptureOutcome = Literal[
    "not_assessed",
    "not_attempted",
    "captured",
    "refused",
    "invalid",
    "unknown",
]
TaskOutcome = Literal[
    "not_started",
    "queued",
    "succeeded",
    "failed",
    "needs_operator",
    "stale",
    "refusal",
    "clarification",
    "no_clean_expression",
    "incomplete",
    "not_found",
    "conflict",
    "unknown",
]
ErrorCode = Literal[
    "source_pins_mismatch",
    "source_model_output_invalid",
    "source_candidate_invalid",
    "source_model_attempt_uncertain",
    "source_fixture_unavailable",
    "source_request_invalid",
    "source_interpretation_failed",
    "provider_retrieval_failed",
]
RequestedModelId = Literal[
    "fixture-source-v1",
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "unknown",
]
AdapterKind = Literal["fixture", "gemini", "unknown"]
PromptPolicyVersion = Literal[
    "fixture-projection-of-source-thesis-candidates-v1",
    "source-thesis-candidates-v1.1",
    "unknown",
]
SystemVariantId = Literal[
    "fitcheck-source-interpretation/fixture-projection-of-source-thesis-candidates-v1",
    "fitcheck-source-interpretation/source-thesis-candidates-v1.1",
    "unknown",
]
RuntimeProfileId = Literal["default", "unknown"]
RuntimeContractVersion = Literal["source-interpretation-worker-v1", "unknown"]

_ALLOWED_REQUESTED_MODEL_IDS: Final = frozenset(
    {"fixture-source-v1", "gemini-2.5-flash", "gemini-3.5-flash"}
)
_ALLOWED_ADAPTER_KINDS: Final = frozenset({"fixture", "gemini"})
_ALLOWED_PROMPT_POLICY_VERSIONS: Final = frozenset(
    {
        "fixture-projection-of-source-thesis-candidates-v1",
        "source-thesis-candidates-v1.1",
    }
)
_ALLOWED_SYSTEM_VARIANT_IDS: Final = frozenset(
    {
        "fitcheck-source-interpretation/fixture-projection-of-source-thesis-candidates-v1",
        "fitcheck-source-interpretation/source-thesis-candidates-v1.1",
    }
)
_ALLOWED_RUNTIME_PROFILE_IDS: Final = frozenset({"default"})
_ALLOWED_RUNTIME_CONTRACT_VERSIONS: Final = frozenset(
    {"source-interpretation-worker-v1"}
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GenerationSettings(_Frozen):
    """Actual reported generation settings; absent remains unknown."""

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)


class UsageObservation(_Frozen):
    """Provider-reported token counts only; missing is never coerced to zero."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    semantics: Literal["unknown", "provider_reported"] = "unknown"


class ExecutionEnvelope(_Frozen):
    """One safe reference/configuration record for a source job execution."""

    envelope_ref: str = Field(pattern=r"^source-envelope-v1:[0-9a-f]{24}$")
    requested_model_id: RequestedModelId
    observed_model_version: Literal["unknown"] = "unknown"
    adapter_kind: AdapterKind
    prompt_policy_version: PromptPolicyVersion
    system_variant_id: SystemVariantId
    response_schema_version: int = Field(ge=1, le=9)
    runtime_profile_id: RuntimeProfileId
    runtime_contract_version: RuntimeContractVersion
    generation: GenerationSettings
    usage: UsageObservation


class OperationalEvent(_Frozen):
    """One single-line, privacy-safe Cloud Logging compatible JSON event."""

    schema_version: Literal["fitcheck.operational_event.v1"] = EVENT_SCHEMA_VERSION
    event_name: EventName
    timestamp: datetime
    severity: Literal["INFO", "WARNING", "ERROR"]
    service: Literal["fitcheck"] = "fitcheck"
    service_revision: str
    operation: Operation
    job_id: UUID | None = None
    attempt_id: UUID | None = None
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    stage: Stage
    transport_outcome: TransportOutcome
    effect_outcome: EffectOutcome
    capture_outcome: CaptureOutcome
    task_outcome: TaskOutcome
    semantic_outcome: Literal["not_assessed", "unknown"] = "not_assessed"
    error_code: ErrorCode | None = None
    execution_envelope_ref: str | None = Field(
        default=None, pattern=r"^source-envelope-v1:[0-9a-f]{24}$"
    )
    execution_envelope: ExecutionEnvelope | None = None


def _allowed_config(value: object, allowed: frozenset[str]) -> str:
    """Keep only configuration values explicitly sanctioned by this schema."""

    candidate = str(value)
    return candidate if candidate in allowed else "unknown"


def _service_revision() -> str:
    # Deployment revision is not emitted as execution metadata. It is only a
    # bounded operational label, so unknown is safer than arbitrary env text.
    return _allowed_config(
        os.environ.get("FITCHECK_SERVICE_REVISION", "unknown"), frozenset()
    )


def _trace_fields(traceparent: str | None) -> tuple[str | None, str | None]:
    if traceparent is None:
        return None, None
    match = _TRACEPARENT.fullmatch(traceparent)
    return (match.group(1), match.group(2)) if match else (None, None)


def execution_envelope(
    pins: Mapping[str, object],
    *,
    generation: Mapping[str, float | None] | None = None,
) -> ExecutionEnvelope:
    """Create a reference from controlled config only, never request content."""

    settings = GenerationSettings.model_validate(generation or {})
    controlled = {
        "requested_model_id": _allowed_config(
            pins.get("model_id", "unknown"), _ALLOWED_REQUESTED_MODEL_IDS
        ),
        "adapter_kind": _allowed_config(
            pins.get("adapter_kind", "unknown"), _ALLOWED_ADAPTER_KINDS
        ),
        "prompt_policy_version": _allowed_config(
            pins.get("prompt_policy_version", "unknown"),
            _ALLOWED_PROMPT_POLICY_VERSIONS,
        ),
        "system_variant_id": _allowed_config(
            pins.get("system_variant_id", "unknown"),
            _ALLOWED_SYSTEM_VARIANT_IDS,
        ),
        "response_schema_version": int(pins.get("response_schema_version", 1)),
        "runtime_profile_id": _allowed_config(
            pins.get("runtime_profile_id", "unknown"),
            _ALLOWED_RUNTIME_PROFILE_IDS,
        ),
        "runtime_contract_version": _allowed_config(
            pins.get("runtime_contract_version", "unknown"),
            _ALLOWED_RUNTIME_CONTRACT_VERSIONS,
        ),
        "generation": settings.model_dump(mode="json"),
    }
    encoded = json.dumps(
        controlled, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return ExecutionEnvelope(
        envelope_ref="source-envelope-v1:"
        + hashlib.sha256(encoded).hexdigest()[:24],
        **controlled,
        usage=UsageObservation(),
    )


class StructuredEventEmitter:
    """Best-effort safe JSON emitter that never changes a product outcome."""

    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger(_LOGGER_NAME)
        self._logger.setLevel(logging.INFO)

    def emit(
        self,
        *,
        event_name: EventName,
        severity: Literal["INFO", "WARNING", "ERROR"],
        operation: Operation,
        stage: Stage,
        transport_outcome: TransportOutcome = "not_applicable",
        effect_outcome: EffectOutcome = "not_applicable",
        capture_outcome: CaptureOutcome = "not_assessed",
        task_outcome: TaskOutcome = "unknown",
        semantic_outcome: Literal["not_assessed", "unknown"] = "not_assessed",
        job_id: UUID | None = None,
        attempt_id: UUID | None = None,
        traceparent: str | None = None,
        error_code: ErrorCode | None = None,
        envelope: ExecutionEnvelope | None = None,
        execution_envelope_ref: str | None = None,
    ) -> None:
        if not structured_logging_enabled():
            return
        try:
            if envelope is not None and execution_envelope_ref is not None:
                return
            if envelope is not None:
                # Do not trust a caller-provided Pydantic instance: callers
                # can bypass construction with ``model_construct``.
                envelope = ExecutionEnvelope.model_validate(
                    envelope.model_dump(mode="json")
                )
            trace_id, span_id = _trace_fields(traceparent)
            event = OperationalEvent(
                event_name=event_name,
                timestamp=datetime.now(timezone.utc),
                severity=severity,
                service_revision=_service_revision(),
                operation=operation,
                job_id=job_id,
                attempt_id=attempt_id,
                trace_id=trace_id,
                span_id=span_id,
                stage=stage,
                transport_outcome=transport_outcome,
                effect_outcome=effect_outcome,
                capture_outcome=capture_outcome,
                task_outcome=task_outcome,
                semantic_outcome=semantic_outcome,
                error_code=error_code,
                execution_envelope_ref=(
                    envelope.envelope_ref
                    if envelope is not None
                    else execution_envelope_ref
                ),
                execution_envelope=envelope,
            )
            self._logger.log(
                getattr(logging, severity), event.model_dump_json()
            )
        except Exception:
            # A broken log sink must neither bypass a durable write nor alter
            # retry/effect authority.
            return


_events: StructuredEventEmitter | None = None


def get_event_emitter() -> StructuredEventEmitter:
    global _events
    if _events is None:
        _events = StructuredEventEmitter()
    return _events


def structured_logging_enabled() -> bool:
    """Return true only for the explicit JSON-event runtime opt-in."""

    return os.environ.get("FITCHECK_STRUCTURED_LOGGING_ENABLED") == "1"


class _CurrentStderrHandler(logging.Handler):
    """Resolve stderr at emit time so stdio hosts and tests cannot go stale."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
            sys.stderr.flush()
        except Exception:
            self.handleError(record)


def configure_structured_stderr_logging() -> bool:
    """Opt in to one JSON-lines stderr sink, never MCP STDIO stdout."""

    logger = logging.getLogger(_LOGGER_NAME)
    if not structured_logging_enabled():
        # A disabled emitter must also have no propagation or last-resort
        # fallback should another caller address this logger directly.
        for handler in list(logger.handlers):
            if getattr(handler, _HANDLER_MARKER, False):
                logger.removeHandler(handler)
        if not any(
            getattr(handler, _DISABLED_HANDLER_MARKER, False)
            for handler in logger.handlers
        ):
            null_handler = logging.NullHandler()
            setattr(null_handler, _DISABLED_HANDLER_MARKER, True)
            logger.addHandler(null_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        return False
    for handler in list(logger.handlers):
        if getattr(handler, _DISABLED_HANDLER_MARKER, False):
            logger.removeHandler(handler)
    if any(getattr(handler, _HANDLER_MARKER, False) for handler in logger.handlers):
        return True
    handler = _CurrentStderrHandler()
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return True


class _ProviderSafetyFilter(logging.Filter):
    """Replace legacy provider text before any handler observes it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not structured_logging_enabled():
            # Suppress the legacy provider record itself so it cannot reach a
            # root/last-resort handler while structured logging is disabled.
            return False
        event_name: EventName
        severity: Literal["INFO", "WARNING", "ERROR"]
        if record.levelno >= logging.WARNING:
            event_name = "fitcheck.retrieval.provider_failure"
            severity = "WARNING" if record.levelno < logging.ERROR else "ERROR"
            transport_outcome: TransportOutcome = "failed"
            task_outcome: TaskOutcome = "failed"
            error_code: ErrorCode | None = "provider_retrieval_failed"
        else:
            event_name = "fitcheck.retrieval.provider_cache_hit"
            severity = "INFO"
            transport_outcome = "read_succeeded"
            task_outcome = "succeeded"
            error_code = None
        event = OperationalEvent(
            event_name=event_name,
            timestamp=datetime.fromtimestamp(record.created, tz=timezone.utc),
            severity=severity,
            service_revision=_service_revision(),
            operation="retrieval",
            stage="provider",
            transport_outcome=transport_outcome,
            effect_outcome="not_applicable",
            capture_outcome="unknown",
            task_outcome=task_outcome,
            error_code=error_code,
        )
        record.msg = event.model_dump_json()
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def install_provider_log_safety() -> None:
    """Install one process-local filter before the legacy provider logs."""

    logger = logging.getLogger("el.retrieval.provider")
    if any(isinstance(value, _ProviderSafetyFilter) for value in logger.filters):
        return
    logger.addFilter(_ProviderSafetyFilter())


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "ExecutionEnvelope",
    "OperationalEvent",
    "StructuredEventEmitter",
    "UsageObservation",
    "configure_structured_stderr_logging",
    "execution_envelope",
    "get_event_emitter",
    "install_provider_log_safety",
]
