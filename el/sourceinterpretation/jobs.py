"""Durable submission and worker boundary for source interpretation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from el.domain.tables import Job, SourceInterpretationRequest
from el.extraction.gate_v2 import insider_screen
from el.jobs import (
    FailureKind,
    IdempotencyConflict,
    JobClaim,
    JobNotFound,
    JobStatus,
    JobStore,
)
from el.sourceinterpretation.service import (
    SourceCandidateValidationError,
    SourceInterpretationOutcome,
    SourceInterpretationService,
)
from el.models.source_adapter import (
    SourceModelOutputInvalid,
    SourceProviderOutcomeUncertain,
)

SOURCE_INTERPRETATION_JOB_TYPE = "source_interpretation_v1"
PRIVACY_REFUSAL_CODE = "input_matched_nonpublic_information_screen"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceInterpretationPins(_Frozen):
    prompt_policy_version: str
    system_variant_id: str
    response_schema_version: Literal[1]
    adapter_kind: str
    model_id: str
    has_external_effect: bool
    runtime_contract_version: Literal["source-interpretation-worker-v1"]


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
    ):
        self._jobs = jobs
        self._source = source_interpretation
        self._sessions = session_factory

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
            or dict(row.pinned_manifest) != pins.model_dump(mode="json")
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
    ) -> SourceInterpretationJobSubmission:
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency key is required")
        pins = self._pins()
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
            max_attempts=2,
        )
        job = self._jobs.get_owned(
            submitted.job_id,
            owner_client_type=owner_client_type,
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
    ) -> SourceInterpretationJobStatus:
        with self._sessions() as session:
            job_id = session.scalar(
                select(Job.id).where(
                    Job.job_type == SOURCE_INTERPRETATION_JOB_TYPE,
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
    ):
        if not worker_id.strip():
            raise ValueError("worker id is required")
        if lease_seconds < 1:
            raise ValueError("lease seconds must be positive")
        self._jobs = jobs
        self._source = source_interpretation
        self._sessions = session_factory
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    def _fail(
        self,
        claim: JobClaim,
        *,
        kind: FailureKind,
        code: str,
        message: str,
        now: datetime | None,
    ) -> SourceInterpretationWorkerResult:
        transitioned = self._jobs.fail(
            claim,
            kind=kind,
            code=code,
            safe_message=message,
            now=now,
        )
        return SourceInterpretationWorkerResult(
            claimed=True,
            status=(
                "needs_operator"
                if transitioned and kind is FailureKind.NEEDS_OPERATOR
                else "failed" if transitioned else "stale"
            ),
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
        )

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
                or dict(request.pinned_manifest) != pins.model_dump(mode="json")
            ):
                raise SourceInterpretationPinMismatch()
            session.expunge(request)
            return request

    def run_once(
        self, *, now: datetime | None = None
    ) -> SourceInterpretationWorkerResult:
        claim = self._jobs.claim_due(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            job_types=[SOURCE_INTERPRETATION_JOB_TYPE],
            now=now,
        )
        if claim is None:
            return SourceInterpretationWorkerResult(claimed=False, status="idle")

        external_effect_started = False
        try:
            payload = SourceInterpretationJobPayload.model_validate(claim.payload)
            pins = SourceInterpretationPins.model_validate(claim.pinned_manifest)
            current_pins = SourceInterpretationPins.model_validate(
                self._source.execution_pins()
            )
            if current_pins != pins:
                raise SourceInterpretationPinMismatch()
            self._checkpoint(claim, stage="validating_request", now=now)
            request = self._load_request(claim, payload, pins)

            if request.privacy_refusal_code is not None:
                computation = self._source.privacy_refusal_computation()
            else:
                if request.input_text is None:
                    raise SourceInterpretationPinMismatch()
                if pins.has_external_effect:
                    if not self._jobs.begin_external_effect(claim, now=now):
                        raise SourceInterpretationLeaseLost()
                    external_effect_started = True
                else:
                    self._checkpoint(claim, stage="computing_fixture", now=now)
                computation = self._source.compute_allowed(
                    request.input_text,
                    source_url=request.source_url,
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
                return {
                    "source_interpretation_id": str(
                        persisted.source_interpretation_id
                    ),
                    "source_interpretation_request_id": str(request.id),
                    "input_digest": persisted.input_digest,
                    "outcome": persisted.outcome,
                }

            if not self._jobs.succeed_with(claim, commit_action, now=now):
                raise SourceInterpretationLeaseLost()
            assert persisted is not None
            return SourceInterpretationWorkerResult(
                claimed=True,
                status="succeeded",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                source_interpretation_id=persisted.source_interpretation_id,
            )
        except SourceInterpretationLeaseLost:
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
            )
        except SourceModelOutputInvalid:
            return self._fail(
                claim,
                kind=FailureKind.PERMANENT,
                code="source_model_output_invalid",
                message="source model output failed schema validation",
                now=now,
            )
        except SourceCandidateValidationError:
            return self._fail(
                claim,
                kind=FailureKind.PERMANENT,
                code="source_candidate_invalid",
                message="source candidate evidence failed validation",
                now=now,
            )
        except SourceProviderOutcomeUncertain:
            return self._fail(
                claim,
                kind=FailureKind.NEEDS_OPERATOR,
                code="source_model_attempt_uncertain",
                message="source model attempt outcome requires operator review",
                now=now,
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
                )
            if isinstance(error, KeyError):
                return self._fail(
                    claim,
                    kind=FailureKind.PERMANENT,
                    code="source_fixture_unavailable",
                    message="source fixture is unavailable",
                    now=now,
                )
            return self._fail(
                claim,
                kind=FailureKind.PERMANENT,
                code="source_request_invalid",
                message="source interpretation request failed validation",
                now=now,
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
            )
