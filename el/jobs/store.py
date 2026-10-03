"""Retry-safe job ledger with idempotent submission and fenced leases.

The implementation intentionally does not know about Cloud Tasks, HTTP, MCP,
or any concrete worker. PostgreSQL is the production authority; SQLite keeps
the state machine and migrations offline-testable. Claiming uses a
compare-and-swap update so stale readers cannot create two active attempts.
PostgreSQL contention remains a separate deployment gate.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, TypeVar

from sqlalchemy import Interval, and_, func, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select

from el.domain.tables import Job, JobAttempt
from el.observability import parse_traceparent

PAYLOAD_HASH_VERSION = 1
MAX_LEASE_SECONDS = 86_400
MAX_BACKOFF_SECONDS = 86_400
REAPER_BATCH_SIZE = 100
EXTERNAL_EFFECT_STARTED_STAGE = "external_effect_started"
NON_PILOT_SOURCE = or_(
    Job.job_type != "source_interpretation_v1",
    Job.submitted_by_api_client_id.is_(None),
)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_OPERATOR = "needs_operator"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    ABANDONED = "abandoned"
    CANCELLED = "cancelled"


class FailureKind(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    RETRY_EXHAUSTED = "retry_exhausted"
    NEEDS_OPERATOR = "needs_operator"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {
        JobStatus.SUCCEEDED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
        JobStatus.NEEDS_OPERATOR.value,
    }
)


class IdempotencyConflict(RuntimeError):
    """The same logical key was reused with different semantic input."""


class JobNotFound(LookupError):
    """Used for both missing and unauthorized jobs to avoid existence leaks."""


class ExternalEffectDenied(RuntimeError):
    """The current attempt was safely ended before an external operation."""


def _is_job_idempotency_collision(exc: IntegrityError) -> bool:
    if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == "uq_job_idempotency":
        return True
    return (
        getattr(exc.orig, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_UNIQUE"
        and str(exc.orig)
        == "UNIQUE constraint failed: jobs.owner_client_type, "
        "jobs.owner_actor_id, jobs.job_type, jobs.idempotency_key"
    )


@dataclass(frozen=True)
class SubmissionResult:
    job_id: uuid.UUID
    created: bool


@dataclass(frozen=True)
class JobClaim:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_number: int
    job_type: str
    payload: dict[str, Any]
    pinned_manifest: dict[str, Any]
    accepted_traceparent: str | None
    job_correlation_id: str
    attempt_trace_id: str
    deadline_at: datetime | None


_T = TypeVar("_T")


class FencedWriteSession:
    """Flush-only SQLAlchemy session facade for fenced domain writes.

    The job store owns transaction completion. Domain callbacks can read,
    stage, and flush ORM rows, but cannot accidentally commit, roll back,
    close, or replace the transaction that holds the attempt fence.
    """

    __slots__ = ("__session",)

    def __init__(self, session: Session):
        self.__session = session

    def add(self, instance: object) -> None:
        self.__session.add(instance)

    def add_all(self, instances: Iterable[object]) -> None:
        self.__session.add_all(instances)

    def delete(self, instance: object) -> None:
        self.__session.delete(instance)

    def scalar(self, statement, params=None, **kwargs):
        self._require_select(statement)
        return self.__session.scalar(statement, params=params, **kwargs)

    def scalars(self, statement, params=None, **kwargs):
        self._require_select(statement)
        return self.__session.scalars(statement, params=params, **kwargs)

    def get(self, entity: type[_T], ident, **kwargs) -> _T | None:
        return self.__session.get(entity, ident, **kwargs)

    def flush(self, objects=None) -> None:
        self.__session.flush(objects)

    @staticmethod
    def _require_select(statement: object) -> None:
        if not isinstance(statement, Select):
            raise TypeError("fenced callbacks may execute typed SELECT statements only")


def canonical_payload_hash(
    payload: dict[str, Any],
    pinned_manifest: dict[str, Any],
    *,
    execution_guarantees: dict[str, Any] | None = None,
) -> str:
    """Hash semantic input and immutable execution guarantees."""

    semantic_input: dict[str, Any] = {
        "payload": payload,
        "pinned_manifest": pinned_manifest,
    }
    if execution_guarantees is not None:
        semantic_input["execution_guarantees"] = execution_guarantees
    canonical = json.dumps(
        semantic_input,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _execution_guarantees(
    *,
    max_attempts: int,
    priority: int,
    available_at: datetime | None,
    deadline_at: datetime | None,
) -> dict[str, Any]:
    # Omitted availability means "at the original submission time". Keep that
    # semantic sentinel in the digest instead of hashing the newly observed
    # clock on every idempotent retry.
    availability: dict[str, str] = (
        {"mode": "explicit", "value": _aware(available_at).isoformat()}
        if available_at is not None
        else {"mode": "submission_time"}
    )
    return {
        "max_attempts": max_attempts,
        "priority": priority,
        "available_at": availability,
        "deadline_at": (
            _aware(deadline_at).isoformat() if deadline_at is not None else None
        ),
    }


class JobStore:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._sessions = session_factory

    def submit_or_get(
        self,
        *,
        job_type: str,
        owner_client_type: str,
        owner_actor_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
        pinned_manifest: dict[str, Any] | None = None,
        owner_user_id: uuid.UUID | None = None,
        submitted_by_api_client_id: uuid.UUID | None = None,
        max_attempts: int = 3,
        priority: int = 100,
        available_at: datetime | None = None,
        deadline_at: datetime | None = None,
        correlation_id: str | None = None,
        accepted_traceparent: str | None = None,
        now: datetime | None = None,
        session: Session | None = None,
    ) -> SubmissionResult:
        if not all(
            value and value.strip()
            for value in (
                job_type,
                owner_client_type,
                owner_actor_id,
                idempotency_key,
            )
        ):
            raise ValueError("job type, owner, and idempotency key are required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if max_attempts >= 2**31:
            raise ValueError("max_attempts exceeds the database integer range")
        for field, value, maximum in (
            ("job_type", job_type, 64),
            ("owner_client_type", owner_client_type, 32),
            ("owner_actor_id", owner_actor_id, 160),
            ("idempotency_key", idempotency_key, 160),
        ):
            if len(value) > maximum:
                raise ValueError(f"{field} exceeds {maximum} characters")
        if correlation_id is not None:
            if not correlation_id.strip():
                raise ValueError("correlation_id must be nonblank")
            if len(correlation_id) > 64:
                raise ValueError("correlation_id exceeds 64 characters")
        if (
            accepted_traceparent is not None
            and parse_traceparent(accepted_traceparent) != accepted_traceparent
        ):
            raise ValueError("accepted_traceparent must be a W3C version 00 traceparent")
        if not -(2**31) <= priority < 2**31:
            raise ValueError("priority is outside the database integer range")
        pins = pinned_manifest or {}
        normalized_available_at = (
            _aware(available_at) if available_at is not None else None
        )
        normalized_deadline_at = (
            _aware(deadline_at) if deadline_at is not None else None
        )
        guarantees = _execution_guarantees(
            max_attempts=max_attempts,
            priority=priority,
            available_at=normalized_available_at,
            deadline_at=normalized_deadline_at,
        )
        digest = canonical_payload_hash(
            payload,
            pins,
            execution_guarantees=guarantees,
        )
        job_id = uuid.uuid4()

        with (nullcontext(session) if session is not None else self._sessions()) as active:
            if session is not None:
                # begin_nested() flushes all pending objects before opening its
                # savepoint. Flush caller work deliberately outside the Job
                # insert savepoint so a duplicate Job cannot roll it back.
                active.flush()
                existing = active.scalar(
                    select(Job).where(
                        Job.owner_client_type == owner_client_type,
                        Job.owner_actor_id == owner_actor_id,
                        Job.job_type == job_type,
                        Job.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if (
                        existing.payload_hash_version != PAYLOAD_HASH_VERSION
                        or existing.payload_hash != digest
                    ):
                        raise IdempotencyConflict(
                            "idempotency key reused with different input or "
                            "execution guarantees/hash version"
                        )
                    return SubmissionResult(job_id=existing.id, created=False)
            timestamp = _aware(now) if now else self._database_now(active)
            deadline_expired = (
                normalized_deadline_at is not None
                and normalized_deadline_at <= timestamp
            )
            row = Job(
                id=job_id,
                job_type=job_type,
                owner_client_type=owner_client_type,
                owner_actor_id=owner_actor_id,
                owner_user_id=owner_user_id,
                submitted_by_api_client_id=submitted_by_api_client_id,
                idempotency_key=idempotency_key,
                payload_hash=digest,
                payload_hash_version=PAYLOAD_HASH_VERSION,
                payload=payload,
                pinned_manifest=pins,
                status=(
                    JobStatus.FAILED.value
                    if deadline_expired
                    else JobStatus.QUEUED.value
                ),
                stage="completed" if deadline_expired else None,
                priority=priority,
                attempt_count=0,
                max_attempts=max_attempts,
                available_at=normalized_available_at or timestamp,
                deadline_at=normalized_deadline_at,
                error_kind=(
                    FailureKind.PERMANENT.value if deadline_expired else None
                ),
                error_code="deadline_exceeded" if deadline_expired else None,
                safe_error_message=(
                    "job deadline exceeded" if deadline_expired else None
                ),
                accepted_traceparent=accepted_traceparent,
                correlation_id=(
                    correlation_id or f"job_{uuid.uuid4().hex}"
                ),
                completed_at=timestamp if deadline_expired else None,
                created_at=timestamp,
                updated_at=timestamp,
            )
            if session is not None:
                try:
                    with active.begin_nested():
                        active.add(row)
                        active.flush([row])
                    return SubmissionResult(job_id=job_id, created=True)
                except IntegrityError as exc:
                    if not _is_job_idempotency_collision(exc):
                        raise
                    existing = active.scalar(
                        select(Job).where(
                            Job.owner_client_type == owner_client_type,
                            Job.owner_actor_id == owner_actor_id,
                            Job.job_type == job_type,
                            Job.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is None:
                        raise
                    if (
                        existing.payload_hash_version != PAYLOAD_HASH_VERSION
                        or existing.payload_hash != digest
                    ):
                        raise IdempotencyConflict(
                            "idempotency key reused with different input or "
                            "execution guarantees/hash version"
                        ) from exc
                    return SubmissionResult(job_id=existing.id, created=False)
            active.add(row)
            try:
                active.commit()
                return SubmissionResult(job_id=job_id, created=True)
            except IntegrityError:
                active.rollback()
                existing = active.scalar(
                    select(Job).where(
                        Job.owner_client_type == owner_client_type,
                        Job.owner_actor_id == owner_actor_id,
                        Job.job_type == job_type,
                        Job.idempotency_key == idempotency_key,
                    )
                )
                if existing is None:
                    raise
                if (
                    existing.payload_hash_version != PAYLOAD_HASH_VERSION
                    or existing.payload_hash != digest
                ):
                    raise IdempotencyConflict(
                        "idempotency key reused with different input or "
                        "execution guarantees/hash version"
                    )
                return SubmissionResult(job_id=existing.id, created=False)

    def get(self, job_id: uuid.UUID) -> Job:
        with self._sessions() as session:
            row = session.get(Job, job_id)
            if row is None:
                raise JobNotFound("job not found")
            self._normalize_job_datetimes(row)
            session.expunge(row)
            return row

    def get_owned(
        self, job_id: uuid.UUID, *, owner_client_type: str, owner_actor_id: str
    ) -> Job:
        with self._sessions() as session:
            row = session.scalar(
                select(Job).where(
                    Job.id == job_id,
                    Job.owner_client_type == owner_client_type,
                    Job.owner_actor_id == owner_actor_id,
                )
            )
            if row is None:
                raise JobNotFound("job not found")
            self._normalize_job_datetimes(row)
            session.expunge(row)
            return row

    def cancel_owned(
        self,
        job_id: uuid.UUID,
        *,
        owner_client_type: str,
        owner_actor_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Cancel an owned nonterminal job and revoke any active attempt.

        Cancellation and completion serialize on the job row lock. Whichever
        transition commits first wins; a worker whose attempt was cancelled
        cannot heartbeat or commit domain outputs afterward.
        """

        with self._sessions() as session:
            timestamp = self._time(session, now)
            job = session.scalar(
                select(Job)
                .where(
                    Job.id == job_id,
                    Job.owner_client_type == owner_client_type,
                    Job.owner_actor_id == owner_actor_id,
                )
                .with_for_update()
            )
            if job is None:
                raise JobNotFound("job not found")
            if job.status in {
                JobStatus.SUCCEEDED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
                JobStatus.NEEDS_OPERATOR.value,
            }:
                return False

            # Re-read the database clock after acquiring the row lock so a
            # cancellation delayed behind another transition is timestamped
            # when it actually gains authority.
            timestamp = self._time(session, now)
            active_attempt_id = job.active_attempt_id
            expected_status = job.status
            transitioned = session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == expected_status,
                    Job.active_attempt_id == active_attempt_id,
                )
                .values(
                    status=JobStatus.CANCELLED.value,
                    stage="completed",
                    result=None,
                    error_kind=FailureKind.CANCELLED.value,
                    error_code="cancelled_by_owner",
                    safe_error_message="job cancelled",
                    error_details=None,
                    active_attempt_id=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=timestamp,
                    cancel_requested_at=timestamp,
                    completed_at=timestamp,
                    updated_at=timestamp,
                )
                .execution_options(synchronize_session=False)
            )
            if transitioned.rowcount != 1:
                session.rollback()
                return False
            if active_attempt_id is not None:
                attempt = session.execute(
                    update(JobAttempt)
                    .where(
                        JobAttempt.id == active_attempt_id,
                        JobAttempt.status == AttemptStatus.RUNNING.value,
                    )
                    .values(
                        status=AttemptStatus.CANCELLED.value,
                        finished_at=timestamp,
                        error_kind=FailureKind.CANCELLED.value,
                        error_code="cancelled_by_owner",
                        safe_error_message="job cancelled",
                    )
                )
                if attempt.rowcount != 1:
                    session.rollback()
                    return False
            session.commit()
            return True

    def claim_due(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        job_types: Iterable[str] | None = None,
        job_id: uuid.UUID | None = None,
        expected_owner_client_type: str | None = None,
        expected_owner_actor_id: str | None = None,
        expected_pinned_manifest: dict[str, Any] | None = None,
        exclude_api_submitted: bool = False,
        now: datetime | None = None,
        contention_retries: int = 8,
    ) -> JobClaim | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if len(worker_id) > 160:
            raise ValueError("worker_id exceeds 160 characters")
        self._validate_lease_seconds(lease_seconds)
        if contention_retries < 1:
            raise ValueError("contention_retries must be positive")
        allowed_types = tuple(job_types or ())
        targeted = job_id is not None
        if targeted:
            if not isinstance(job_id, uuid.UUID):
                raise ValueError("job_id must be a UUID")
            if (
                len(allowed_types) != 1
                or not expected_owner_client_type
                or not expected_owner_actor_id
                or expected_pinned_manifest is None
            ):
                raise ValueError("targeted claim requires kind, owner, and pins")
            target_scope = (
                Job.id == job_id,
                Job.job_type == allowed_types[0],
                Job.owner_client_type == expected_owner_client_type,
                Job.owner_actor_id == expected_owner_actor_id,
            )
        else:
            if any(
                value is not None
                for value in (
                    expected_owner_client_type,
                    expected_owner_actor_id,
                    expected_pinned_manifest,
                )
            ):
                raise ValueError("target guards require job_id")
            target_scope = ()

        for _ in range(contention_retries):
            with self._sessions() as session:
                timestamp = _aware(now) if now else self._database_now(session)
                if targeted:
                    # Check immutable identity and runtime pins before any
                    # reconciliation write. A mismatched canary must leave
                    # every job and attempt untouched.
                    target = session.scalar(select(Job).where(*target_scope))
                    if (
                        target is None
                        or target.pinned_manifest != expected_pinned_manifest
                    ):
                        session.rollback()
                        return None
                self._mark_deadline_exceeded(
                    session, timestamp, job_id=job_id,
                    exclude_api_submitted=exclude_api_submitted,
                )
                self._mark_exhausted(
                    session, timestamp, job_id=job_id,
                    exclude_api_submitted=exclude_api_submitted,
                )
                # Reconciliation is independent of this worker's subsequent
                # claim CAS. Preserve terminal transitions even if contention
                # makes the claim lose and retry.
                session.commit()
                timestamp = _aware(now) if now else self._database_now(session)
                predicate = self._claimable(timestamp)
                query = select(Job).where(predicate, *target_scope)
                if exclude_api_submitted:
                    query = query.where(NON_PILOT_SOURCE)
                if allowed_types:
                    query = query.where(Job.job_type.in_(allowed_types))
                candidate = session.scalar(
                    query.order_by(
                        Job.priority,
                        Job.available_at,
                        Job.created_at,
                        Job.id,
                    ).limit(1)
                )
                if candidate is None:
                    session.commit()
                    return None

                attempt_id = uuid.uuid4()
                attempt_number = candidate.attempt_count + 1
                previous_attempt_id = candidate.active_attempt_id
                lease_expires_at = timestamp + timedelta(seconds=lease_seconds)
                attempt_trace_id = (
                    f"{candidate.correlation_id[:36]}:a{attempt_number}:"
                    f"{attempt_id.hex[:8]}"
                )
                target_cas = (
                    (Job.payload_hash == candidate.payload_hash,) if targeted else ()
                )
                updated = session.execute(
                    update(Job)
                    .where(
                        Job.id == candidate.id,
                        Job.attempt_count == candidate.attempt_count,
                        self._claimable(timestamp),
                        *target_cas,
                        *target_scope,
                    )
                    .values(
                        status=JobStatus.RUNNING.value,
                        stage="claimed",
                        attempt_count=attempt_number,
                        active_attempt_id=attempt_id,
                        lease_owner=worker_id,
                        lease_expires_at=lease_expires_at,
                        heartbeat_at=timestamp,
                        started_at=candidate.started_at or timestamp,
                        updated_at=timestamp,
                    )
                    .execution_options(synchronize_session=False)
                )
                if updated.rowcount != 1:
                    session.rollback()
                    continue

                if previous_attempt_id is not None:
                    session.execute(
                        update(JobAttempt)
                        .where(
                            JobAttempt.id == previous_attempt_id,
                            JobAttempt.status == AttemptStatus.RUNNING.value,
                        )
                        .values(
                            status=AttemptStatus.ABANDONED.value,
                            finished_at=timestamp,
                            error_kind=FailureKind.TRANSIENT.value,
                            error_code="lease_expired",
                            safe_error_message="worker lease expired",
                        )
                    )
                session.add(
                    JobAttempt(
                        id=attempt_id,
                        job_id=candidate.id,
                        attempt_number=attempt_number,
                        lease_owner=worker_id,
                        status=AttemptStatus.RUNNING.value,
                        attempt_trace_id=attempt_trace_id,
                        started_at=timestamp,
                        lease_expires_at=lease_expires_at,
                        heartbeat_at=timestamp,
                    )
                )
                session.commit()
                return JobClaim(
                    job_id=candidate.id,
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    job_type=candidate.job_type,
                    payload=dict(candidate.payload),
                    pinned_manifest=dict(candidate.pinned_manifest),
                    accepted_traceparent=candidate.accepted_traceparent,
                    job_correlation_id=candidate.correlation_id,
                    attempt_trace_id=attempt_trace_id,
                    deadline_at=(
                        _aware(candidate.deadline_at)
                        if candidate.deadline_at is not None
                        else None
                    ),
                )
        return None

    def heartbeat(
        self,
        claim: JobClaim,
        *,
        lease_seconds: int = 60,
        stage: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        self._validate_lease_seconds(lease_seconds)
        if stage is not None:
            if not stage.strip():
                raise ValueError("stage must be nonblank")
            if len(stage) > 64:
                raise ValueError("stage exceeds 64 characters")
        with self._sessions() as session:
            timestamp = self._time(session, now)
            self._mark_deadline_exceeded(
                session, timestamp, job_id=claim.job_id
            )
            self._mark_exhausted(session, timestamp, job_id=claim.job_id)
            job = self._lock_claim(session, claim)
            if job is None:
                session.commit()
                return False
            # PostgreSQL ``now()`` is transaction-start time. Read a fresh
            # wall clock only after the row lock so a delayed heartbeat cannot
            # revive a lease that expired while waiting for the lock.
            timestamp = self._time(session, now)
            if not self._authority_is_current(job, timestamp):
                self._mark_deadline_exceeded(
                    session, timestamp, job_id=claim.job_id
                )
                self._mark_exhausted(
                    session, timestamp, job_id=claim.job_id
                )
                session.commit()
                return False
            heartbeat_clock = self._completion_clock(session, now)
            lease_expires_at = self._add_seconds(
                session,
                heartbeat_clock,
                lease_seconds,
                now=now,
            )
            values: dict[str, Any] = {
                "heartbeat_at": heartbeat_clock,
                "lease_expires_at": lease_expires_at,
                "updated_at": heartbeat_clock,
            }
            if stage is not None:
                values["stage"] = stage
            renewed = session.execute(
                update(Job)
                .where(
                    Job.id == claim.job_id,
                    Job.status == JobStatus.RUNNING.value,
                    Job.active_attempt_id == claim.attempt_id,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at > heartbeat_clock,
                    self._before_deadline(heartbeat_clock),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if renewed.rowcount != 1:
                session.rollback()
                self._reap_lost_authority(claim, now=now)
                return False
            attempt = session.execute(
                update(JobAttempt)
                .where(
                    JobAttempt.id == claim.attempt_id,
                    JobAttempt.status == AttemptStatus.RUNNING.value,
                )
                .values(
                    heartbeat_at=heartbeat_clock,
                    lease_expires_at=lease_expires_at,
                )
            )
            if attempt.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True

    def begin_external_effect(
        self,
        claim: JobClaim,
        *,
        now: datetime | None = None,
        allow_effect: Callable[[Session, Job, datetime], bool] | None = None,
    ) -> bool:
        """Seal automatic retries before a non-idempotent external effect.

        A worker calls this immediately before a provider/model operation whose
        outcome may be ambiguous if the process dies.  Expiry after this point
        becomes ``needs_operator`` instead of creating another attempt.  A
        crash before this call remains reclaimable under the submitted attempt
        budget.
        """

        with self._sessions() as session:
            timestamp = self._time(session, now)
            self._mark_deadline_exceeded(
                session, timestamp, job_id=claim.job_id
            )
            self._mark_exhausted(session, timestamp, job_id=claim.job_id)
            job = self._lock_claim(session, claim)
            if job is None:
                session.commit()
                return False
            effect_clock = self._time(session, now)
            if not self._authority_is_current(job, effect_clock):
                self._mark_deadline_exceeded(
                    session, effect_clock, job_id=claim.job_id
                )
                self._mark_exhausted(
                    session, effect_clock, job_id=claim.job_id
                )
                session.commit()
                return False
            # A pilot guard may wait on the actor row. Refresh DB time after
            # that lock before judging expiry or lease authority.
            guard_allowed = (
                allow_effect(session, job, effect_clock)
                if allow_effect is not None else True
            )
            effect_clock = self._time(session, now)
            if not self._authority_is_current(job, effect_clock):
                self._mark_deadline_exceeded(
                    session, effect_clock, job_id=claim.job_id
                )
                self._mark_exhausted(session, effect_clock, job_id=claim.job_id)
                session.commit()
                return False
            if guard_allowed and allow_effect is not None:
                guard_allowed = allow_effect(session, job, effect_clock)
            marked_source_without_policy = (
                job.job_type == "source_interpretation_v1"
                and job.submitted_by_api_client_id is not None
                and allow_effect is None
            )
            if marked_source_without_policy or not guard_allowed:
                denied_at = self._completion_clock(session, now)
                ended = session.execute(
                    update(Job)
                    .where(
                        Job.id == claim.job_id,
                        Job.status == JobStatus.RUNNING.value,
                        Job.active_attempt_id == claim.attempt_id,
                    )
                    .values(
                        status=JobStatus.FAILED.value,
                        stage="completed",
                        result=None,
                        error_kind=FailureKind.PERMANENT.value,
                        error_code="source_pilot_effect_denied",
                        safe_error_message="source pilot authorization expired or was revoked",
                        active_attempt_id=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        heartbeat_at=denied_at,
                        completed_at=denied_at,
                        updated_at=denied_at,
                    )
                )
                attempt = session.execute(
                    update(JobAttempt)
                    .where(JobAttempt.id == claim.attempt_id,
                           JobAttempt.status == AttemptStatus.RUNNING.value)
                    .values(
                        status=AttemptStatus.FAILED.value,
                        error_kind=FailureKind.PERMANENT.value,
                        error_code="source_pilot_effect_denied",
                        safe_error_message="source pilot authorization expired or was revoked",
                        finished_at=denied_at,
                    )
                )
                if ended.rowcount != 1 or attempt.rowcount != 1:
                    session.rollback()
                    return False
                session.commit()
                raise ExternalEffectDenied("source pilot effect denied")
            transitioned = session.execute(
                update(Job)
                .where(
                    Job.id == claim.job_id,
                    Job.status == JobStatus.RUNNING.value,
                    Job.active_attempt_id == claim.attempt_id,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at > effect_clock,
                    self._before_deadline(effect_clock),
                )
                .values(
                    stage=EXTERNAL_EFFECT_STARTED_STAGE,
                    external_effect_started_at=func.coalesce(
                        Job.external_effect_started_at,
                        effect_clock,
                    ),
                    external_effect_attempt_id=claim.attempt_id,
                    max_attempts=Job.attempt_count,
                    heartbeat_at=effect_clock,
                    updated_at=effect_clock,
                )
                .execution_options(synchronize_session=False)
            )
            if transitioned.rowcount != 1:
                session.rollback()
                self._reap_lost_authority(claim, now=now)
                return False
            attempt = session.execute(
                update(JobAttempt)
                .where(
                    JobAttempt.id == claim.attempt_id,
                    JobAttempt.status == AttemptStatus.RUNNING.value,
                )
                .values(heartbeat_at=effect_clock)
            )
            if attempt.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True

    def succeed(
        self,
        claim: JobClaim,
        *,
        result: dict[str, Any],
        now: datetime | None = None,
    ) -> bool:
        return self.succeed_with(claim, lambda _session, _job: result, now=now)

    def succeed_with(
        self,
        claim: JobClaim,
        commit_action: Callable[[FencedWriteSession, Job], dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> bool:
        """Fence, run domain writes, and complete the job in one transaction."""

        with self._sessions() as session:
            timestamp = self._time(session, now)
            self._mark_deadline_exceeded(
                session, timestamp, job_id=claim.job_id
            )
            self._mark_exhausted(session, timestamp, job_id=claim.job_id)
            job = self._lock_claim(session, claim)
            if job is None:
                # Preserve a deadline/exhaustion transition performed above.
                session.commit()
                return False
            authority_time = self._time(session, now)
            if not self._authority_is_current(job, authority_time):
                self._mark_deadline_exceeded(
                    session, authority_time, job_id=claim.job_id
                )
                self._mark_exhausted(
                    session, authority_time, job_id=claim.job_id
                )
                session.commit()
                return False
            transaction = session.get_transaction()
            if transaction is None or not transaction.is_active:
                raise RuntimeError("fenced transaction is not active")
            result = commit_action(FencedWriteSession(session), job)
            if (
                session.get_transaction() is not transaction
                or not transaction.is_active
            ):
                session.rollback()
                raise RuntimeError("domain callback ended the fenced transaction")
            self._validate_json_value(result, field="job result")
            completion_clock = self._completion_clock(session, now)
            completed = session.execute(
                update(Job)
                .where(
                    Job.id == claim.job_id,
                    Job.status == JobStatus.RUNNING.value,
                    Job.active_attempt_id == claim.attempt_id,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at > completion_clock,
                    self._before_deadline(completion_clock),
                )
                .values(
                    status=JobStatus.SUCCEEDED.value,
                    stage="completed",
                    result=result,
                    error_kind=None,
                    error_code=None,
                    safe_error_message=None,
                    error_details=None,
                    active_attempt_id=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=completion_clock,
                    completed_at=completion_clock,
                    updated_at=completion_clock,
                )
                .execution_options(synchronize_session=False)
            )
            if completed.rowcount != 1:
                session.rollback()
                self._reap_lost_authority(claim, now=now)
                return False
            attempt = session.execute(
                update(JobAttempt)
                .where(
                    JobAttempt.id == claim.attempt_id,
                    JobAttempt.status == AttemptStatus.RUNNING.value,
                )
                .values(
                    status=AttemptStatus.SUCCEEDED.value,
                    finished_at=completion_clock,
                )
            )
            if attempt.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True

    def fail(
        self,
        claim: JobClaim,
        *,
        kind: FailureKind,
        code: str,
        safe_message: str,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
        backoff_seconds: int | None = None,
    ) -> bool:
        if not code.strip():
            raise ValueError("failure code is required")
        if len(code) > 64:
            raise ValueError("failure code exceeds 64 characters")
        if not safe_message.strip():
            raise ValueError("safe error message is required")
        if len(safe_message) > 256:
            raise ValueError("safe error message exceeds 256 characters")
        if backoff_seconds is not None and backoff_seconds < 0:
            raise ValueError("backoff_seconds must be nonnegative")
        if backoff_seconds is not None and backoff_seconds > MAX_BACKOFF_SECONDS:
            raise ValueError("backoff_seconds exceeds the maximum")
        if details is not None:
            self._validate_json_value(details, field="error details")
        with self._sessions() as session:
            timestamp = self._time(session, now)
            self._mark_deadline_exceeded(
                session, timestamp, job_id=claim.job_id
            )
            self._mark_exhausted(session, timestamp, job_id=claim.job_id)
            job = self._lock_claim(session, claim)
            if job is None:
                # Preserve a deadline/exhaustion transition performed above.
                session.commit()
                return False
            timestamp = self._time(session, now)
            if not self._authority_is_current(job, timestamp):
                self._mark_deadline_exceeded(
                    session, timestamp, job_id=claim.job_id
                )
                self._mark_exhausted(
                    session, timestamp, job_id=claim.job_id
                )
                session.commit()
                return False

            terminal = True
            attempt_status = AttemptStatus.FAILED.value
            effective_kind = kind
            effective_code = code
            job_status = JobStatus.FAILED.value
            retry_delay: int | None = None
            if kind == FailureKind.TRANSIENT and job.attempt_count < job.max_attempts:
                terminal = False
                job_status = JobStatus.RETRY_WAIT.value
                retry_delay = (
                    backoff_seconds
                    if backoff_seconds is not None
                    else min(
                        MAX_BACKOFF_SECONDS,
                        2 ** min(job.attempt_count - 1, 17),
                    )
                )
                attempt_status = AttemptStatus.RETRY_WAIT.value
            elif kind == FailureKind.TRANSIENT:
                effective_kind = FailureKind.RETRY_EXHAUSTED
                effective_code = "retry_budget_exhausted"
            elif kind == FailureKind.NEEDS_OPERATOR:
                job_status = JobStatus.NEEDS_OPERATOR.value
            elif kind == FailureKind.CANCELLED:
                job_status = JobStatus.CANCELLED.value
                attempt_status = AttemptStatus.CANCELLED.value

            failure_clock = self._completion_clock(session, now)
            values: dict[str, Any] = {
                "status": job_status,
                "stage": "retry_wait" if not terminal else "completed",
                "result": None,
                "error_kind": effective_kind.value,
                "error_code": effective_code,
                "safe_error_message": safe_message,
                "error_details": details,
                "active_attempt_id": None,
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": failure_clock,
                "completed_at": failure_clock if terminal else None,
                "updated_at": failure_clock,
            }
            if retry_delay is not None:
                values["available_at"] = self._add_seconds(
                    session,
                    failure_clock,
                    retry_delay,
                    now=now,
                )
            transitioned = session.execute(
                update(Job)
                .where(
                    Job.id == claim.job_id,
                    Job.status == JobStatus.RUNNING.value,
                    Job.active_attempt_id == claim.attempt_id,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at > failure_clock,
                    self._before_deadline(failure_clock),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if transitioned.rowcount != 1:
                session.rollback()
                self._reap_lost_authority(claim, now=now)
                return False
            attempt = session.execute(
                update(JobAttempt)
                .where(
                    JobAttempt.id == claim.attempt_id,
                    JobAttempt.status == AttemptStatus.RUNNING.value,
                )
                .values(
                    status=attempt_status,
                    finished_at=failure_clock,
                    error_kind=kind.value,
                    error_code=code,
                    safe_error_message=safe_message,
                )
            )
            if attempt.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True

    @staticmethod
    def _claimable(timestamp: datetime):
        return and_(
            Job.attempt_count < Job.max_attempts,
            JobStore._before_deadline(timestamp),
            or_(
                and_(
                    Job.status.in_(
                        [JobStatus.QUEUED.value, JobStatus.RETRY_WAIT.value]
                    ),
                    Job.available_at <= timestamp,
                ),
                and_(
                    Job.status == JobStatus.RUNNING.value,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at <= timestamp,
                ),
            ),
        )

    @staticmethod
    def _before_deadline(timestamp: datetime):
        return or_(Job.deadline_at.is_(None), Job.deadline_at > timestamp)

    @staticmethod
    def _lock_claim(session: Session, claim: JobClaim) -> Job | None:
        return session.scalar(
            select(Job)
            .where(
                Job.id == claim.job_id,
                Job.status == JobStatus.RUNNING.value,
                Job.active_attempt_id == claim.attempt_id,
            )
            .with_for_update()
        )

    @staticmethod
    def _authority_is_current(job: Job, timestamp: datetime) -> bool:
        lease_expires_at = job.lease_expires_at
        if lease_expires_at is None or _aware(lease_expires_at) <= timestamp:
            return False
        return job.deadline_at is None or _aware(job.deadline_at) > timestamp

    @classmethod
    def _mark_deadline_exceeded(
        cls,
        session: Session,
        timestamp: datetime,
        *,
        job_id: uuid.UUID | None = None,
        exclude_api_submitted: bool = False,
    ) -> int:
        uncertain_predicate = and_(
            Job.status == JobStatus.RUNNING.value,
            Job.external_effect_started_at.is_not(None),
            Job.external_effect_attempt_id == Job.active_attempt_id,
            Job.deadline_at.is_not(None),
            Job.deadline_at <= timestamp,
        )
        if exclude_api_submitted:
            uncertain_predicate = and_(uncertain_predicate, NON_PILOT_SOURCE)
        uncertain = cls._transition_terminal_jobs(
            session,
            uncertain_predicate,
            timestamp=timestamp,
            failure_kind=FailureKind.NEEDS_OPERATOR,
            error_code="external_effect_outcome_uncertain",
            safe_message="external model attempt outcome is uncertain",
            job_id=job_id,
            job_status=JobStatus.NEEDS_OPERATOR,
            attempt_failure_kind=FailureKind.NEEDS_OPERATOR,
            attempt_error_code="external_effect_deadline_exceeded",
            attempt_safe_message=(
                "job deadline expired after external effect began"
            ),
        )
        predicate = and_(
            Job.status.in_(
                [
                    JobStatus.QUEUED.value,
                    JobStatus.RETRY_WAIT.value,
                    JobStatus.RUNNING.value,
                ]
            ),
            or_(
                Job.status != JobStatus.RUNNING.value,
                Job.external_effect_attempt_id != Job.active_attempt_id,
                Job.external_effect_attempt_id.is_(None),
            ),
            Job.deadline_at.is_not(None),
            Job.deadline_at <= timestamp,
        )
        if exclude_api_submitted:
            predicate = and_(predicate, NON_PILOT_SOURCE)
        regular = cls._transition_terminal_jobs(
            session,
            predicate,
            timestamp=timestamp,
            failure_kind=FailureKind.PERMANENT,
            error_code="deadline_exceeded",
            safe_message="job deadline exceeded",
            job_id=job_id,
        )
        return uncertain + regular

    @classmethod
    def _mark_exhausted(
        cls,
        session: Session,
        timestamp: datetime,
        *,
        job_id: uuid.UUID | None = None,
        exclude_api_submitted: bool = False,
    ) -> int:
        uncertain_predicate = and_(
            Job.status == JobStatus.RUNNING.value,
            Job.external_effect_started_at.is_not(None),
            Job.external_effect_attempt_id == Job.active_attempt_id,
            Job.attempt_count >= Job.max_attempts,
            Job.lease_expires_at.is_not(None),
            Job.lease_expires_at <= timestamp,
        )
        if exclude_api_submitted:
            uncertain_predicate = and_(uncertain_predicate, NON_PILOT_SOURCE)
        uncertain = cls._transition_terminal_jobs(
            session,
            uncertain_predicate,
            timestamp=timestamp,
            failure_kind=FailureKind.NEEDS_OPERATOR,
            error_code="external_effect_outcome_uncertain",
            safe_message="external model attempt outcome is uncertain",
            job_id=job_id,
            job_status=JobStatus.NEEDS_OPERATOR,
            attempt_failure_kind=FailureKind.NEEDS_OPERATOR,
            attempt_error_code="external_effect_lease_expired",
            attempt_safe_message="worker lease expired after external effect began",
        )
        predicate = and_(
            Job.attempt_count >= Job.max_attempts,
            or_(
                Job.status != JobStatus.RUNNING.value,
                Job.external_effect_attempt_id != Job.active_attempt_id,
                Job.external_effect_attempt_id.is_(None),
            ),
            or_(
                Job.status.in_(
                    [JobStatus.QUEUED.value, JobStatus.RETRY_WAIT.value]
                ),
                and_(
                    Job.status == JobStatus.RUNNING.value,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at <= timestamp,
                ),
            ),
        )
        if exclude_api_submitted:
            predicate = and_(predicate, NON_PILOT_SOURCE)
        exhausted = cls._transition_terminal_jobs(
            session,
            predicate,
            timestamp=timestamp,
            failure_kind=FailureKind.RETRY_EXHAUSTED,
            error_code="claim_budget_exhausted",
            safe_message="retry budget exhausted",
            job_id=job_id,
            attempt_failure_kind=FailureKind.TRANSIENT,
            attempt_error_code="lease_expired",
            attempt_safe_message="worker lease expired",
        )
        return uncertain + exhausted

    @staticmethod
    def _transition_terminal_jobs(
        session: Session,
        predicate,
        *,
        timestamp: datetime,
        failure_kind: FailureKind,
        error_code: str,
        safe_message: str,
        job_id: uuid.UUID | None,
        attempt_failure_kind: FailureKind | None = None,
        attempt_error_code: str | None = None,
        attempt_safe_message: str | None = None,
        job_status: JobStatus = JobStatus.FAILED,
    ) -> int:
        """Conditionally terminalize current candidates without lost updates.

        The initial read only finds candidate IDs. The UPDATE repeats the full
        state predicate and the observed attempt token, so a concurrent claim,
        heartbeat, or completion wins cleanly instead of being overwritten by
        a stale ORM flush.
        """

        query = select(Job.id, Job.active_attempt_id).where(predicate)
        if job_id is not None:
            query = query.where(Job.id == job_id)
        else:
            query = query.order_by(Job.id).limit(REAPER_BATCH_SIZE)
        candidates = session.execute(query).all()
        transitioned = 0
        for candidate_job_id, active_attempt_id in candidates:
            result = session.execute(
                update(Job)
                .where(
                    Job.id == candidate_job_id,
                    Job.active_attempt_id == active_attempt_id,
                    predicate,
                )
                .values(
                    status=job_status.value,
                    stage="completed",
                    error_kind=failure_kind.value,
                    error_code=error_code,
                    safe_error_message=safe_message,
                    error_details=None,
                    result=None,
                    active_attempt_id=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=timestamp,
                    completed_at=timestamp,
                    updated_at=timestamp,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                continue
            transitioned += 1
            if active_attempt_id is not None:
                session.execute(
                    update(JobAttempt)
                    .where(
                        JobAttempt.id == active_attempt_id,
                        JobAttempt.status == AttemptStatus.RUNNING.value,
                    )
                    .values(
                        status=AttemptStatus.ABANDONED.value,
                        finished_at=timestamp,
                        error_kind=(
                            attempt_failure_kind or failure_kind
                        ).value,
                        error_code=attempt_error_code or error_code,
                        safe_error_message=(
                            attempt_safe_message or safe_message
                        ),
                    )
                )
        return transitioned

    @staticmethod
    def _validate_lease_seconds(lease_seconds: int) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if lease_seconds > MAX_LEASE_SECONDS:
            raise ValueError("lease_seconds exceeds the maximum")

    @staticmethod
    def _validate_json_value(value: Any, *, field: str) -> None:
        try:
            json.dumps(value, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field} must be finite JSON") from error

    def _time(self, session: Session, now: datetime | None) -> datetime:
        return _aware(now) if now is not None else self._database_now(session)

    def _completion_clock(self, session: Session, now: datetime | None):
        if now is not None:
            return _aware(now)
        if session.get_bind().dialect.name == "postgresql":
            # Unlike PostgreSQL ``now()``, this is evaluated at statement time.
            return func.clock_timestamp()
        return self._database_now(session)

    @staticmethod
    def _add_seconds(
        session: Session,
        clock,
        seconds: int,
        *,
        now: datetime | None,
    ):
        delta = timedelta(seconds=seconds)
        if now is None and session.get_bind().dialect.name == "postgresql":
            return clock + literal(delta, type_=Interval())
        return clock + delta

    def _reap_lost_authority(
        self,
        claim: JobClaim,
        *,
        now: datetime | None,
    ) -> None:
        with self._sessions() as session:
            timestamp = self._time(session, now)
            self._mark_deadline_exceeded(
                session, timestamp, job_id=claim.job_id
            )
            self._mark_exhausted(session, timestamp, job_id=claim.job_id)
            session.commit()

    @staticmethod
    def _normalize_job_datetimes(job: Job) -> None:
        for field in (
            "available_at",
            "deadline_at",
            "lease_expires_at",
            "heartbeat_at",
            "cancel_requested_at",
            "started_at",
            "completed_at",
            "updated_at",
            "created_at",
        ):
            value = getattr(job, field)
            if value is not None:
                setattr(job, field, _aware(value))

    @staticmethod
    def _database_now(session: Session) -> datetime:
        clock = (
            func.clock_timestamp()
            if session.get_bind().dialect.name == "postgresql"
            else func.now()
        )
        value = session.scalar(select(clock))
        if not isinstance(value, datetime):
            raise RuntimeError("database did not return a timestamp")
        return _aware(value)
