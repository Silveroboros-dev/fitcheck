"""Durable submission and worker boundary for source interpretation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from el.domain.tables import Job, SourceInterpretationRequest
from el.product.demo_admission import (
    DemoAdmission, DemoAdmissionDenied, MAX_ACTIVE_SOURCE_JOBS,
    MAX_LIFETIME_SOURCE_JOBS,
)
from el.mcp.contracts import PermissionDenied
from el.mcp.source_pilot import SourcePilotPolicy
from el.extraction.gate_v2 import insider_screen
from el.jobs import (
    FailureKind,
    IdempotencyConflict,
    JobClaim,
    JobNotFound,
    JobStatus,
    JobStore,
)
from el.jobs.store import ExternalEffectDenied
from el.sourceinterpretation.service import (
    SourceCandidateValidationError,
    SourceInterpretationOutcome,
    SourceInterpretationService,
)
from el.models.source_adapter import (
    SourceModelOutputInvalid,
    SourceProviderOutcomeUncertain,
)
from el.observability import (
    ExecutionEnvelope,
    StructuredEventEmitter,
    Telemetry,
    execution_envelope,
    get_event_emitter,
    get_telemetry,
)
from el.sourceinterpretation.binding import (
    SOURCE_INTERPRETATION_JOB_TYPE,
)

PRIVACY_REFUSAL_CODE = "input_matched_nonpublic_information_screen"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceInterpretationPins(_Frozen):
    prompt_policy_version: str
    system_variant_id: str
    response_schema_version: Literal[1]
    adapter_kind: str
    model_id: str
    runtime_profile_id: str = "default"
    has_external_effect: bool
    runtime_contract_version: Literal["source-interpretation-worker-v1"]


def _source_pins_from_job_manifest(manifest: object) -> SourceInterpretationPins:
    if not isinstance(manifest, dict):
        raise SourceInterpretationPinMismatch()
    source_fields = set(SourceInterpretationPins.model_fields)
    if set(manifest) - source_fields - {"budget"}:
        raise SourceInterpretationPinMismatch()
    return SourceInterpretationPins.model_validate(
        {name: manifest[name] for name in source_fields if name in manifest}
    )


class SourceInterpretationJobPayload(_Frozen):
    source_interpretation_request_id: uuid.UUID
    input_digest: str | None


class SourceInterpretationJobSubmission(_Frozen):
    job_id: uuid.UUID
    source_interpretation_request_id: uuid.UUID
    input_digest: str | None
    status: str
    stage: str | None
    created: bool
    created_at: datetime
    updated_at: datetime


class SourceJobAdmissionError(RuntimeError):
    """Bounded transport error emitted by an injected admission strategy."""

    def __init__(
        self,
        error: str,
        status_code: int,
        *,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail or error)
        self.error = error
        self.status_code = status_code
        self.detail = detail


class SourceJobAdmission(Protocol):
    """Server-owned admission strategy for a durable source-job namespace."""

    job_type: str
    request_max_bytes: int

    def submit(
        self,
        service: "SourceInterpretationJobService",
        input_text: str,
        *,
        source_url: str | None,
        idempotency_key: str,
        owner_client_type: str,
        owner_actor_id: str,
        agent_client_id: str,
        owner_user_id: uuid.UUID,
    ) -> SourceInterpretationJobSubmission: ...


class SourceJobBudgetGuard(Protocol):
    """Budget transitions required by an explicitly targeted source worker."""

    policy_version: str

    def validate(self, session: Session, job: Job, now: datetime) -> bool: ...

    def record_dispatch(
        self, session: Session, job: Job, now: datetime
    ) -> bool: ...

    def reconcile_terminal(
        self,
        job_id: uuid.UUID,
        *,
        account_id: uuid.UUID,
        owner_client_type: str,
        owner_actor_id: str,
    ) -> bool: ...

    def release_in_fenced_success(
        self, session: Session, job: Job, now: datetime
    ) -> bool: ...


class SourceInterpretationJobStatus(_Frozen):
    job_id: uuid.UUID
    source_interpretation_request_id: uuid.UUID
    input_digest: str | None
    status: str
    stage: str | None
    error_code: str | None
    safe_error_message: str | None
    interpretation: SourceInterpretationOutcome | None
    created_at: datetime
    updated_at: datetime


class SourceInterpretationWorkerResult(_Frozen):
    claimed: bool
    status: str
    job_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    source_interpretation_id: uuid.UUID | None = None


class SourceInterpretationLeaseLost(RuntimeError):
    pass


class SourceInterpretationPinMismatch(RuntimeError):
    pass


class SourceInterpretationJobService:
    def __init__(
        self,
        *,
        jobs: JobStore,
        source_interpretation: SourceInterpretationService,
        session_factory: sessionmaker[Session],
        telemetry: Telemetry | None = None,
        events: StructuredEventEmitter | None = None,
    ):
        self._jobs = jobs
        self._source = source_interpretation
        self._sessions = session_factory
        self._telemetry = telemetry or get_telemetry()
        self._events = events or get_event_emitter()

    @property
    def telemetry(self) -> Telemetry:
        return self._telemetry

    @property
    def events(self) -> StructuredEventEmitter:
        return self._events

    def _execution_envelope(
        self, pins: SourceInterpretationPins
    ) -> ExecutionEnvelope:
        return execution_envelope(
            pins.model_dump(mode="json"),
            generation=self._source.observability_generation_settings(),
        )

    def _pins(self) -> SourceInterpretationPins:
        return SourceInterpretationPins.model_validate(
            self._source.execution_pins()
        )

    @staticmethod
    def _validate_existing_request(
        row: SourceInterpretationRequest,
        *,
        input_text: str,
        input_digest: str | None,
        source_url: str | None,
        privacy_refused: bool,
        pins: SourceInterpretationPins,
        agent_client_id: str,
    ) -> None:
        expected_refusal = PRIVACY_REFUSAL_CODE if privacy_refused else None
        if (
            row.agent_client_id != agent_client_id
            or row.privacy_refusal_code != expected_refusal
            or row.input_digest != input_digest
            or row.source_url != (None if privacy_refused else source_url)
            or SourceInterpretationPins.model_validate(row.pinned_manifest) != pins
            or (not privacy_refused and row.input_text != input_text)
        ):
            raise IdempotencyConflict(
                "idempotency key reused with different source or execution pins"
            )

    def _request_for_submission(
        self,
        *,
        input_text: str,
        source_url: str | None,
        owner_client_type: str,
        owner_actor_id: str,
        agent_client_id: str,
        idempotency_key: str,
        pins: SourceInterpretationPins,
    ) -> SourceInterpretationRequest:
        privacy_refused = insider_screen(input_text)
        input_digest = (
            None if privacy_refused else self._source.digest_input(input_text)
        )
        with self._sessions() as session:
            row = session.scalar(
                select(SourceInterpretationRequest).where(
                    SourceInterpretationRequest.owner_client_type
                    == owner_client_type,
                    SourceInterpretationRequest.owner_actor_id == owner_actor_id,
                    SourceInterpretationRequest.idempotency_key == idempotency_key,
                )
            )
            if row is None:
                row = SourceInterpretationRequest(
                    owner_client_type=owner_client_type,
                    owner_actor_id=owner_actor_id,
                    agent_client_id=agent_client_id,
                    idempotency_key=idempotency_key,
                    input_text=None if privacy_refused else input_text,
                    input_digest=input_digest,
                    source_url=None if privacy_refused else source_url,
                    privacy_refusal_code=(
                        PRIVACY_REFUSAL_CODE if privacy_refused else None
                    ),
                    pinned_manifest=pins.model_dump(mode="json"),
                )
                session.add(row)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    row = session.scalar(
                        select(SourceInterpretationRequest).where(
                            SourceInterpretationRequest.owner_client_type
                            == owner_client_type,
                            SourceInterpretationRequest.owner_actor_id
                            == owner_actor_id,
                            SourceInterpretationRequest.idempotency_key
                            == idempotency_key,
                        )
                    )
                    if row is None:
                        raise
            self._validate_existing_request(
                row,
                input_text=input_text,
                input_digest=input_digest,
                source_url=source_url,
                privacy_refused=privacy_refused,
                pins=pins,
                agent_client_id=agent_client_id,
            )
            session.expunge(row)
            return row

    def submit(
        self,
        input_text: str,
        *,
        source_url: str | None,
        idempotency_key: str,
        owner_client_type: str,
        owner_actor_id: str,
        agent_client_id: str,
        owner_user_id: uuid.UUID | None = None,
        demo_admission: DemoAdmission | None = None,
    ) -> SourceInterpretationJobSubmission:
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency key is required")
        if demo_admission is not None:
            demo_admission.require_source(input_text, source_url)
        # This manual span begins after the transport has parsed the payload.
        # It has no content attributes, headers, events, or exception capture.
        with self._telemetry.span("fitcheck.source.enqueue") as span:
            span.set_attributes({"fitcheck.source.stage": "enqueued"})
            pins = self._pins()
            if demo_admission is None:
                request = self._request_for_submission(
                    input_text=input_text,
                    source_url=source_url,
                    owner_client_type=owner_client_type,
                    owner_actor_id=owner_actor_id,
                    agent_client_id=agent_client_id,
                    idempotency_key=idempotency_key,
                    pins=pins,
                )
                payload = SourceInterpretationJobPayload(
                    source_interpretation_request_id=request.id,
                    input_digest=request.input_digest,
                )
                submitted = self._jobs.submit_or_get(
                    job_type=SOURCE_INTERPRETATION_JOB_TYPE,
                    owner_client_type=owner_client_type,
                    owner_actor_id=owner_actor_id,
                    owner_user_id=owner_user_id,
                    idempotency_key=idempotency_key,
                    payload=payload.model_dump(mode="json"),
                    pinned_manifest=pins.model_dump(mode="json"),
                    accepted_traceparent=self._telemetry.current_traceparent(),
                    max_attempts=2,
                )
            else:
                request, submitted = self._submit_demo_admitted(
                    admission=demo_admission, input_text=input_text,
                    source_url=source_url, owner_client_type=owner_client_type,
                    owner_actor_id=owner_actor_id, agent_client_id=agent_client_id,
                    idempotency_key=idempotency_key, owner_user_id=owner_user_id,
                    pins=pins,
                )
            job = self._jobs.get_owned(
                submitted.job_id,
                owner_client_type=owner_client_type,
                owner_actor_id=owner_actor_id,
            )
            span.set_attributes(
                {
                    "fitcheck.source.result": (
                        "queued" if submitted.created else "reused"
                    ),
                    "fitcheck.source.external_effect": pins.has_external_effect,
                }
            )
            envelope = self._execution_envelope(pins)
            traceparent = self._telemetry.current_traceparent()
            self._events.emit(
                event_name="fitcheck.source.execution_envelope",
                severity="INFO",
                operation="source_interpretation",
                stage="enqueued",
                transport_outcome=(
                    "accepted" if submitted.created else "reused"
                ),
                effect_outcome=(
                    "not_started" if pins.has_external_effect else "not_applicable"
                ),
                capture_outcome="not_attempted",
                task_outcome="queued",
                job_id=job.id,
                traceparent=traceparent,
                envelope=envelope,
            )
            self._events.emit(
                event_name="fitcheck.source.task_outcome",
                severity="INFO",
                operation="source_interpretation",
                stage="enqueued",
                transport_outcome=(
                    "accepted" if submitted.created else "reused"
                ),
                effect_outcome=(
                    "not_started" if pins.has_external_effect else "not_applicable"
                ),
                capture_outcome="not_attempted",
                task_outcome="queued",
                job_id=job.id,
                traceparent=traceparent,
                execution_envelope_ref=envelope.envelope_ref,
            )
            return SourceInterpretationJobSubmission(
                job_id=job.id,
                source_interpretation_request_id=request.id,
                input_digest=request.input_digest,
                status=job.status,
                stage=job.stage,
                created=submitted.created,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )

    def _submit_demo_admitted(
        self, *, admission: DemoAdmission, input_text: str,
        source_url: str | None, owner_client_type: str, owner_actor_id: str,
        agent_client_id: str, idempotency_key: str,
        owner_user_id: uuid.UUID | None, pins: SourceInterpretationPins,
    ):
        refused = insider_screen(input_text)
        digest = None if refused else self._source.digest_input(input_text)
        with self._sessions() as session:
            admission.lock(session)
            admission.require_transaction_writable(session)
            request = session.scalar(select(SourceInterpretationRequest).where(
                SourceInterpretationRequest.owner_client_type == owner_client_type,
                SourceInterpretationRequest.owner_actor_id == owner_actor_id,
                SourceInterpretationRequest.idempotency_key == idempotency_key,
            ))
            existing = session.scalar(select(Job).where(
                Job.job_type == SOURCE_INTERPRETATION_JOB_TYPE,
                Job.owner_client_type == owner_client_type,
                Job.owner_actor_id == owner_actor_id,
                Job.idempotency_key == idempotency_key,
            ))
            if (request is None) != (existing is None):
                raise RuntimeError("source request and job integrity mismatch")
            if request is not None:
                self._validate_existing_request(
                    request, input_text=input_text, input_digest=digest,
                    source_url=source_url, privacy_refused=refused,
                    pins=pins, agent_client_id=agent_client_id,
                )
            else:
                lifetime = session.scalar(select(func.count()).select_from(Job).where(
                    Job.job_type == SOURCE_INTERPRETATION_JOB_TYPE,
                )) or 0
                active = session.scalar(select(func.count()).select_from(Job).where(
                    Job.job_type == SOURCE_INTERPRETATION_JOB_TYPE,
                    Job.status.in_((JobStatus.QUEUED.value,
                                    JobStatus.RUNNING.value,
                                    JobStatus.RETRY_WAIT.value)),
                )) or 0
                if lifetime >= MAX_LIFETIME_SOURCE_JOBS:
                    raise DemoAdmissionDenied("source_job_lifetime_limit")
                if active >= MAX_ACTIVE_SOURCE_JOBS:
                    raise DemoAdmissionDenied("source_job_active_limit")
                request = SourceInterpretationRequest(
                    owner_client_type=owner_client_type,
                    owner_actor_id=owner_actor_id,
                    agent_client_id=agent_client_id,
                    idempotency_key=idempotency_key,
                    input_text=None if refused else input_text,
                    input_digest=digest,
                    source_url=None if refused else source_url,
                    privacy_refusal_code=PRIVACY_REFUSAL_CODE if refused else None,
                    pinned_manifest=pins.model_dump(mode="json"),
                )
                session.add(request)
                session.flush()
            payload = SourceInterpretationJobPayload(
                source_interpretation_request_id=request.id,
                input_digest=request.input_digest,
            )
            submitted = self._jobs.submit_or_get(
                job_type=SOURCE_INTERPRETATION_JOB_TYPE,
                owner_client_type=owner_client_type,
                owner_actor_id=owner_actor_id,
                owner_user_id=owner_user_id,
                idempotency_key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                pinned_manifest=pins.model_dump(mode="json"),
                accepted_traceparent=self._telemetry.current_traceparent(),
                max_attempts=2, deadline_at=admission.write_deadline,
                session=session,
            )
            session.commit()
            return request, submitted

    def submit_pilot(
        self,
        input_text: str,
        *,
        source_url: str | None,
        idempotency_key: str,
        owner_client_type: str,
        owner_actor_id: str,
        agent_client_id: str,
        owner_user_id: uuid.UUID,
        policy: SourcePilotPolicy,
    ) -> SourceInterpretationJobSubmission:
        """Lock one pilot client and commit the request and Job together."""
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency key is required")
        pins = self._pins()
        refused = insider_screen(input_text)
        digest = None if refused else self._source.digest_input(input_text)
        with self._sessions() as session:
            policy.lock_client(
                session, actor_id=owner_actor_id,
                client_type=owner_client_type, user_id=owner_user_id,
            )
            request = session.scalar(
                select(SourceInterpretationRequest).where(
                    SourceInterpretationRequest.owner_client_type == owner_client_type,
                    SourceInterpretationRequest.owner_actor_id == owner_actor_id,
                    SourceInterpretationRequest.idempotency_key == idempotency_key,
                )
            )
            existing = session.scalar(
                select(Job).where(
                    Job.job_type == SOURCE_INTERPRETATION_JOB_TYPE,
                    Job.owner_client_type == owner_client_type,
                    Job.owner_actor_id == owner_actor_id,
                    Job.idempotency_key == idempotency_key,
                )
            )
            if (request is None) != (existing is None):
                raise RuntimeError("source request and job integrity mismatch")
            if existing is not None and (
                existing.owner_user_id != owner_user_id
                or existing.submitted_by_api_client_id not in (None, policy.actor_id)
            ):
                raise RuntimeError("source job owner binding is invalid")
            if request is not None:
                self._validate_existing_request(
                    request, input_text=input_text, input_digest=digest,
                    source_url=source_url, privacy_refused=refused,
                    pins=pins, agent_client_id=agent_client_id,
                )
            else:
                now = self._jobs._database_now(session)
                if not policy.is_current(now):
                    raise PermissionDenied("source pilot has expired")
                count = session.scalar(
                    select(func.count()).select_from(Job).where(
                        Job.job_type == SOURCE_INTERPRETATION_JOB_TYPE,
                        Job.owner_actor_id == owner_actor_id,
                    )
                ) or 0
                if count >= policy.max_jobs:
                    raise PermissionDenied("source pilot job limit reached")
                request = SourceInterpretationRequest(
                    owner_client_type=owner_client_type,
                    owner_actor_id=owner_actor_id,
                    agent_client_id=agent_client_id,
                    idempotency_key=idempotency_key,
                    input_text=None if refused else input_text,
                    input_digest=digest,
                    source_url=None if refused else source_url,
                    privacy_refusal_code=PRIVACY_REFUSAL_CODE if refused else None,
                    pinned_manifest=pins.model_dump(mode="json"),
                )
                session.add(request)
                session.flush()
            payload = SourceInterpretationJobPayload(
                source_interpretation_request_id=request.id,
                input_digest=request.input_digest,
            )
            submitted = self._jobs.submit_or_get(
                job_type=SOURCE_INTERPRETATION_JOB_TYPE,
                owner_client_type=owner_client_type,
                owner_actor_id=owner_actor_id,
                owner_user_id=owner_user_id,
                submitted_by_api_client_id=policy.actor_id,
                idempotency_key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                pinned_manifest=pins.model_dump(mode="json"),
                accepted_traceparent=self._telemetry.current_traceparent(),
                max_attempts=2,
                session=session,
            )
            session.commit()
        job = self._jobs.get_owned(
            submitted.job_id, owner_client_type=owner_client_type,
            owner_actor_id=owner_actor_id,
        )
        return SourceInterpretationJobSubmission(
            job_id=job.id,
            source_interpretation_request_id=request.id,
            input_digest=request.input_digest,
            status=job.status,
            stage=job.stage,
            created=submitted.created,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    def get_owned(
        self,
        job_id: uuid.UUID,
        *,
        owner_client_type: str,
        owner_actor_id: str,
        agent_client_id: str,
    ) -> SourceInterpretationJobStatus:
        job = self._jobs.get_owned(
            job_id,
            owner_client_type=owner_client_type,
            owner_actor_id=owner_actor_id,
        )
        payload = SourceInterpretationJobPayload.model_validate(job.payload)
        interpretation = None
        if job.status == JobStatus.SUCCEEDED.value:
            result = job.result or {}
            try:
                interpretation_id = uuid.UUID(result["source_interpretation_id"])
                result_request_id = uuid.UUID(
                    result["source_interpretation_request_id"]
                )
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError("completed source job result is invalid") from error
            if result_request_id != payload.source_interpretation_request_id:
                raise RuntimeError("completed source job request binding is invalid")
            interpretation = self._source.get_owned(
                interpretation_id,
                client_type=owner_client_type,
                agent_client_id=agent_client_id,
            )
            if (
                interpretation.job_id != job.id
                or interpretation.source_interpretation_request_id
                != payload.source_interpretation_request_id
                or interpretation.input_digest != payload.input_digest
            ):
                raise RuntimeError("completed source interpretation binding is invalid")
        return SourceInterpretationJobStatus(
            job_id=job.id,
            source_interpretation_request_id=(
                payload.source_interpretation_request_id
            ),
            input_digest=payload.input_digest,
            status=job.status,
            stage=job.stage,
            error_code=job.error_code,
            safe_error_message=job.safe_error_message,
            interpretation=interpretation,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    def get_by_idempotency_owned(
        self,
        idempotency_key: str,
        *,
        owner_client_type: str,
        owner_actor_id: str,
        agent_client_id: str,
        job_type: str = SOURCE_INTERPRETATION_JOB_TYPE,
    ) -> SourceInterpretationJobStatus:
        if not isinstance(job_type, str) or not job_type.strip() or len(job_type) > 64:
            raise ValueError("bounded source interpretation job type is required")
        with self._sessions() as session:
            job_id = session.scalar(
                select(Job.id).where(
                    Job.job_type == job_type,
                    Job.owner_client_type == owner_client_type,
                    Job.owner_actor_id == owner_actor_id,
                    Job.idempotency_key == idempotency_key,
                )
            )
        if job_id is None:
            raise JobNotFound("job not found")
        return self.get_owned(
            job_id,
            owner_client_type=owner_client_type,
            owner_actor_id=owner_actor_id,
            agent_client_id=agent_client_id,
        )


class SourceInterpretationWorker:
    def __init__(
        self,
        *,
        jobs: JobStore,
        source_interpretation: SourceInterpretationService,
        session_factory: sessionmaker[Session],
        worker_id: str,
        lease_seconds: int = 90,
        telemetry: Telemetry | None = None,
        events: StructuredEventEmitter | None = None,
        pilot_policy: SourcePilotPolicy | None = None,
        budget_guard: SourceJobBudgetGuard | None = None,
        job_type: str = SOURCE_INTERPRETATION_JOB_TYPE,
        untargeted_pinned_manifest: dict[str, Any] | None = None,
    ):
        if not worker_id.strip():
            raise ValueError("worker id is required")
        if lease_seconds < 1:
            raise ValueError("lease seconds must be positive")
        if not isinstance(job_type, str) or not job_type.strip() or len(job_type) > 64:
            raise ValueError("bounded source interpretation job type is required")
        self._jobs = jobs
        self._source = source_interpretation
        self._sessions = session_factory
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._telemetry = telemetry or get_telemetry()
        self._events = events or get_event_emitter()
        self._pilot_policy = pilot_policy
        self._budget_guard = budget_guard
        self._job_type = job_type
        if pilot_policy is not None and budget_guard is not None:
            raise ValueError("source worker accepts one pilot authority")
        if untargeted_pinned_manifest is not None:
            expected = SourceInterpretationPins.model_validate(
                untargeted_pinned_manifest
            ).model_dump(mode="json")
            if self._source.execution_pins() != expected:
                raise ValueError("worker source pins differ from untargeted claim scope")
            self._untargeted_pinned_manifest = expected
        else:
            self._untargeted_pinned_manifest = None

    def _fail(
        self,
        claim: JobClaim,
        *,
        kind: FailureKind,
        code: str,
        message: str,
        now: datetime | None,
        effect_outcome: str = "unknown",
        capture_outcome: str = "unknown",
        execution_envelope_ref: str | None = None,
    ) -> SourceInterpretationWorkerResult:
        with self._telemetry.span("fitcheck.source.persisted_outcome") as span:
            transitioned = self._jobs.fail(
                claim,
                kind=kind,
                code=code,
                safe_message=message,
                now=now,
            )
            self._reconcile_terminal_budget(claim.job_id)
            status = (
                "needs_operator"
                if transitioned and kind is FailureKind.NEEDS_OPERATOR
                else "failed" if transitioned else "stale"
            )
            span.set_attributes(
                {
                    "fitcheck.source.stage": "persisted_outcome",
                    "fitcheck.source.result": status,
                }
            )
            self._events.emit(
                event_name="fitcheck.source.task_outcome",
                severity=("WARNING" if status != "failed" else "ERROR"),
                operation="source_interpretation",
                stage="persisted_outcome",
                effect_outcome=effect_outcome,  # type: ignore[arg-type]
                capture_outcome=capture_outcome,  # type: ignore[arg-type]
                task_outcome=status,  # type: ignore[arg-type]
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                traceparent=self._telemetry.current_traceparent(),
                error_code=code,  # type: ignore[arg-type]
                execution_envelope_ref=execution_envelope_ref,
            )
            return SourceInterpretationWorkerResult(
                claimed=True,
                status=status,
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
            )

    def _reconcile_terminal_budget(self, job_id: uuid.UUID) -> None:
        """Finish the safe reservation transition after JobStore commits.

        JobStore can terminalize a Job while claiming, heartbeating or fencing
        an effect. Those transitions intentionally do not know the injected
        reservation store. A targeted worker reconciles it on the same
        invocation and on later retries after a process crash.
        """
        if self._budget_guard is None:
            return
        job = self._jobs.get(job_id)
        if "budget" not in job.pinned_manifest:
            return
        if job.owner_user_id is None:
            raise RuntimeError("budget-pinned source job has no account owner")
        owner = {
            "account_id": job.owner_user_id,
            "owner_client_type": job.owner_client_type,
            "owner_actor_id": job.owner_actor_id,
        }
        if (
            (
                job.external_effect_started_at is None
                and job.status in {
                    JobStatus.SUCCEEDED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                }
            )
            or (
                job.external_effect_started_at is not None
                and job.status == JobStatus.NEEDS_OPERATOR.value
            )
        ):
            self._budget_guard.reconcile_terminal(job.id, **owner)

    def _checkpoint(
        self, claim: JobClaim, *, stage: str, now: datetime | None
    ) -> None:
        if not self._jobs.heartbeat(
            claim,
            lease_seconds=self._lease_seconds,
            stage=stage,
            now=now,
        ):
            raise SourceInterpretationLeaseLost()

    def _load_request(
        self,
        claim: JobClaim,
        payload: SourceInterpretationJobPayload,
        pins: SourceInterpretationPins,
    ) -> SourceInterpretationRequest:
        job = self._jobs.get(claim.job_id)
        with self._sessions() as session:
            request = session.get(
                SourceInterpretationRequest,
                payload.source_interpretation_request_id,
            )
            if request is None:
                raise SourceInterpretationPinMismatch()
            if (
                request.owner_client_type != job.owner_client_type
                or request.owner_actor_id != job.owner_actor_id
                or request.input_digest != payload.input_digest
                or _source_pins_from_job_manifest(request.pinned_manifest) != pins
            ):
                raise SourceInterpretationPinMismatch()
            session.expunge(request)
            return request

    def run_once(
        self,
        *,
        job_id: uuid.UUID | None = None,
        expected_owner_client_type: str | None = None,
        expected_owner_actor_id: str | None = None,
        now: datetime | None = None,
    ) -> SourceInterpretationWorkerResult:
        target = {}
        if job_id is not None:
            if not expected_owner_client_type or not expected_owner_actor_id:
                raise ValueError("target owner is required with job_id")
            # Pilot Jobs require a policy-bearing explicit processor. The
            # immutable submitter FK marks them without a new schema field.
            try:
                owned = self._jobs.get_owned(
                    job_id, owner_client_type=expected_owner_client_type,
                    owner_actor_id=expected_owner_actor_id,
                )
            except JobNotFound:
                return SourceInterpretationWorkerResult(claimed=False, status="idle")
            if owned.job_type != self._job_type:
                return SourceInterpretationWorkerResult(claimed=False, status="idle")
            owned = self._jobs.reconcile_owned_terminal(
                job_id,
                job_type=owned.job_type,
                owner_client_type=expected_owner_client_type,
                owner_actor_id=expected_owner_actor_id,
                now=now,
            )
            if owned.status in {
                JobStatus.SUCCEEDED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
                JobStatus.NEEDS_OPERATOR.value,
            }:
                self._reconcile_terminal_budget(job_id)
                return SourceInterpretationWorkerResult(
                    claimed=False, status="idle"
                )
            if owned.submitted_by_api_client_id is not None and self._pilot_policy is None:
                return SourceInterpretationWorkerResult(claimed=False, status="idle")
            try:
                source_pins = _source_pins_from_job_manifest(
                    owned.pinned_manifest
                )
            except (SourceInterpretationPinMismatch, ValidationError):
                return SourceInterpretationWorkerResult(claimed=False, status="idle")
            if source_pins.model_dump(mode="json") != self._source.execution_pins():
                return SourceInterpretationWorkerResult(claimed=False, status="idle")
            budget_pin = owned.pinned_manifest.get("budget")
            if self._budget_guard is not None:
                if (
                    not isinstance(budget_pin, dict)
                    or budget_pin.get("policy_version")
                    != self._budget_guard.policy_version
                ):
                    return SourceInterpretationWorkerResult(
                        claimed=False, status="idle"
                    )
            elif budget_pin is not None:
                return SourceInterpretationWorkerResult(claimed=False, status="idle")
            target = {
                "job_id": job_id,
                "expected_owner_client_type": expected_owner_client_type,
                "expected_owner_actor_id": expected_owner_actor_id,
                "expected_pinned_manifest": owned.pinned_manifest,
            }
        elif (
            expected_owner_client_type is not None
            or expected_owner_actor_id is not None
        ):
            raise ValueError("target owner requires job_id")
        else:
            if self._job_type != SOURCE_INTERPRETATION_JOB_TYPE:
                return SourceInterpretationWorkerResult(
                    claimed=False, status="idle"
                )
            target["expected_pinned_manifest"] = self._untargeted_pinned_manifest
        claim = self._jobs.claim_due(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            job_types=[self._job_type],
            now=now,
            exclude_api_submitted=job_id is None,
            **target,
        )
        if claim is None:
            if job_id is not None:
                self._reconcile_terminal_budget(job_id)
            return SourceInterpretationWorkerResult(claimed=False, status="idle")

        # A durable attempt starts a new bounded span.  Its parent is the
        # first accepted W3C context, not the long-finished HTTP request span.
        with self._telemetry.span(
            "fitcheck.source.worker_attempt",
            parent_traceparent=claim.accepted_traceparent,
        ) as span:
            span.set_attributes({"fitcheck.source.stage": "attempt"})
            result = self._run_claim(claim, now=now)
            span.set_attributes({"fitcheck.source.result": result.status})
            return result

    def _run_claim(
        self, claim: JobClaim, *, now: datetime | None
    ) -> SourceInterpretationWorkerResult:
        external_effect_started = False
        envelope_ref: str | None = None
        try:
            payload = SourceInterpretationJobPayload.model_validate(claim.payload)
            pins = _source_pins_from_job_manifest(claim.pinned_manifest)
            current_pins = SourceInterpretationPins.model_validate(
                self._source.execution_pins()
            )
            if current_pins != pins:
                raise SourceInterpretationPinMismatch()
            envelope_ref = execution_envelope(
                pins.model_dump(mode="json"),
                generation=self._source.observability_generation_settings(),
            ).envelope_ref
            self._checkpoint(claim, stage="validating_request", now=now)
            request = self._load_request(claim, payload, pins)

            if request.privacy_refusal_code is not None:
                with self._telemetry.span(
                    "fitcheck.source.mechanical_result"
                ) as span:
                    computation = self._source.privacy_refusal_computation()
                    span.set_attributes(
                        {
                            "fitcheck.source.stage": "mechanical_result",
                            "fitcheck.source.result": "refusal",
                        }
                    )
            else:
                if request.input_text is None:
                    raise SourceInterpretationPinMismatch()
                if pins.has_external_effect:
                    if not self._jobs.begin_external_effect(
                        claim, now=now,
                        allow_effect=(self._pilot_policy.allow_effect
                                      if self._pilot_policy is not None else None),
                        budget_guard=self._budget_guard,
                    ):
                        raise SourceInterpretationLeaseLost()
                    external_effect_started = True
                else:
                    self._checkpoint(claim, stage="computing_fixture", now=now)
                with self._telemetry.span(
                    "fitcheck.source.model_boundary"
                ) as span:
                    span.set_attributes(
                        {
                            "fitcheck.source.stage": "model_boundary",
                            "fitcheck.source.external_effect": (
                                pins.has_external_effect
                            ),
                        }
                    )
                    computation = self._source.compute_allowed(
                        request.input_text,
                        source_url=request.source_url,
                        reservation_identity={
                            "input_sha256": request.input_digest,
                            "actor_scope": {
                                "owner_client_type": request.owner_client_type,
                                "owner_actor_id": request.owner_actor_id,
                                "agent_client_id": request.agent_client_id,
                            },
                            "source_interpretation_request_id": str(request.id),
                            "job_id": str(claim.job_id),
                        },
                    )
                # The source service performs deterministic validation before
                # returning.  This separate result span records only its
                # bounded product result, never candidates or source bytes.
                with self._telemetry.span(
                    "fitcheck.source.mechanical_result"
                ) as span:
                    span.set_attributes(
                        {
                            "fitcheck.source.stage": "mechanical_result",
                            "fitcheck.source.result": computation.outcome,
                        }
                    )

            if external_effect_started:
                if not self._jobs.heartbeat(
                    claim,
                    lease_seconds=self._lease_seconds,
                    now=now,
                ):
                    raise SourceInterpretationLeaseLost()
            else:
                self._checkpoint(claim, stage="persisting_result", now=now)
            persisted: SourceInterpretationOutcome | None = None

            def commit_action(session, job: Job) -> dict:
                nonlocal persisted
                persisted = self._source.persist_in_session(
                    session,
                    computation,
                    client_type=request.owner_client_type,
                    agent_client_id=request.agent_client_id,
                    source_interpretation_request_id=request.id,
                    job_id=job.id,
                )
                if (
                    not external_effect_started
                    and self._budget_guard is not None
                    and "budget" in job.pinned_manifest
                ):
                    self._budget_guard.release_in_fenced_success(
                        session, job, now or datetime.now(timezone.utc)
                    )
                return {
                    "source_interpretation_id": str(
                        persisted.source_interpretation_id
                    ),
                    "source_interpretation_request_id": str(request.id),
                    "input_digest": persisted.input_digest,
                    "outcome": persisted.outcome,
                }

            with self._telemetry.span(
                "fitcheck.source.persisted_outcome"
            ) as span:
                if not self._jobs.succeed_with(claim, commit_action, now=now):
                    raise SourceInterpretationLeaseLost()
                span.set_attributes(
                    {
                        "fitcheck.source.stage": "persisted_outcome",
                        "fitcheck.source.result": "succeeded",
                    }
                )
            self._events.emit(
                event_name="fitcheck.source.task_outcome",
                severity="INFO",
                operation="source_interpretation",
                stage="persisted_outcome",
                effect_outcome=(
                    "not_started"
                    if computation.outcome == "refusal"
                    else "succeeded"
                    if pins.has_external_effect
                    else "not_applicable"
                ),
                capture_outcome=(
                    "refused" if computation.outcome == "refusal" else "captured"
                ),
                task_outcome=(
                    "refusal" if computation.outcome == "refusal" else "succeeded"
                ),
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                traceparent=self._telemetry.current_traceparent(),
                execution_envelope_ref=envelope_ref,
            )
            assert persisted is not None
            return SourceInterpretationWorkerResult(
                claimed=True,
                status="succeeded",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                source_interpretation_id=persisted.source_interpretation_id,
            )
        except ExternalEffectDenied:
            self._reconcile_terminal_budget(claim.job_id)
            return SourceInterpretationWorkerResult(
                claimed=True, status="failed", job_id=claim.job_id,
                attempt_id=claim.attempt_id,
            )
        except SourceInterpretationLeaseLost:
            self._reconcile_terminal_budget(claim.job_id)
            return SourceInterpretationWorkerResult(
                claimed=True,
                status="stale",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
            )
        except SourceInterpretationPinMismatch:
            return self._fail(
                claim,
                kind=FailureKind.PERMANENT,
                code="source_pins_mismatch",
                message="source interpretation request conflicts with pinned runtime",
                now=now,
                execution_envelope_ref=envelope_ref,
            )
        except SourceModelOutputInvalid:
            return self._fail(
                claim,
                kind=FailureKind.PERMANENT,
                code="source_model_output_invalid",
                message="source model output failed schema validation",
                now=now,
                effect_outcome=(
                    "succeeded" if external_effect_started else "not_applicable"
                ),
                capture_outcome="invalid",
                execution_envelope_ref=envelope_ref,
            )
        except SourceCandidateValidationError:
            return self._fail(
                claim,
                kind=FailureKind.PERMANENT,
                code="source_candidate_invalid",
                message="source candidate evidence failed validation",
                now=now,
                effect_outcome=(
                    "succeeded" if external_effect_started else "not_applicable"
                ),
                capture_outcome="invalid",
                execution_envelope_ref=envelope_ref,
            )
        except SourceProviderOutcomeUncertain:
            return self._fail(
                claim,
                kind=FailureKind.NEEDS_OPERATOR,
                code="source_model_attempt_uncertain",
                message="source model attempt outcome requires operator review",
                now=now,
                effect_outcome="uncertain",
                capture_outcome="unknown",
                execution_envelope_ref=envelope_ref,
            )
        except (ValidationError, KeyError, ValueError) as error:
            if external_effect_started:
                return self._fail(
                    claim,
                    kind=FailureKind.NEEDS_OPERATOR,
                    code="source_model_attempt_uncertain",
                    message=(
                        "source model attempt outcome requires operator review"
                    ),
                    now=now,
                    effect_outcome="uncertain",
                    capture_outcome="unknown",
                    execution_envelope_ref=envelope_ref,
                )
            if isinstance(error, KeyError):
                return self._fail(
                    claim,
                    kind=FailureKind.PERMANENT,
                code="source_fixture_unavailable",
                message="source fixture is unavailable",
                now=now,
                effect_outcome="not_applicable",
                capture_outcome="unknown",
                execution_envelope_ref=envelope_ref,
                )
            return self._fail(
                claim,
                kind=FailureKind.PERMANENT,
                code="source_request_invalid",
                message="source interpretation request failed validation",
                now=now,
                effect_outcome="not_applicable",
                capture_outcome="invalid",
                execution_envelope_ref=envelope_ref,
            )
        except Exception:
            return self._fail(
                claim,
                kind=(
                    FailureKind.NEEDS_OPERATOR
                    if external_effect_started
                    else FailureKind.PERMANENT
                ),
                code=(
                    "source_model_attempt_uncertain"
                    if external_effect_started
                    else "source_interpretation_failed"
                ),
                message=(
                    "source model attempt outcome requires operator review"
                    if external_effect_started
                    else "source interpretation failed"
                ),
                now=now,
                effect_outcome=(
                    "uncertain" if external_effect_started else "not_applicable"
                ),
                capture_outcome="unknown",
                execution_envelope_ref=envelope_ref,
            )
