"""Durable job idempotency, retry, lease, and fencing semantics."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from el.domain.tables import Base, Job, JobAttempt, User
from el.jobs import (
    AttemptStatus,
    FailureKind,
    IdempotencyConflict,
    JobNotFound,
    JobStatus,
    JobStore,
    canonical_payload_hash,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def _store(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    return JobStore(sessions), sessions


def _submit(store: JobStore, **overrides):
    values = {
        "job_type": "market_universe_refresh",
        "owner_client_type": "system",
        "owner_actor_id": "snapshot-scheduler",
        "idempotency_key": "polymarket-2026-08-06T12",
        "payload": {"provider": "polydata", "cutoff": "2026-08-06T12:00:00Z"},
        "pinned_manifest": {"normalization_policy_version": "market-v1"},
        "max_attempts": 3,
        "now": NOW,
    }
    values.update(overrides)
    return store.submit_or_get(**values)


def test_submit_is_actor_scoped_idempotent_and_payload_sensitive(tmp_path):
    store, sessions = _store(tmp_path)

    first = _submit(store)
    duplicate = _submit(store)
    other_actor = _submit(store, owner_actor_id="other-scheduler")

    assert first.created
    assert not duplicate.created
    assert duplicate.job_id == first.job_id
    assert other_actor.job_id != first.job_id
    with pytest.raises(IdempotencyConflict):
        _submit(store, payload={"provider": "other"})

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 2


def test_caller_owned_unrelated_insert_error_preserves_outer_work(tmp_path):
    store, sessions = _store(tmp_path)
    user_id = uuid.uuid4()
    with sessions() as session:
        session.add(User(id=user_id, email=f"{user_id}@example.invalid"))
        with pytest.raises(IntegrityError):
            _submit(
                store,
                session=session,
                idempotency_key="missing-owner-fk",
                owner_user_id=uuid.uuid4(),
            )
        # Only the Job insert failed. The caller still owns a usable transaction.
        assert session.get(User, user_id) is not None
        session.commit()

    with sessions() as session:
        assert session.get(User, user_id) is not None
        assert session.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.parametrize("conflicting_payload", [False, True])
def test_caller_owned_same_key_collision_recovers_exactly(tmp_path, conflicting_payload):
    store, sessions = _store(tmp_path)
    winner = None
    with sessions() as session:
        original_scalar = session.scalar
        raced = False

        def scalar_with_winner(statement, *args, **kwargs):
            nonlocal winner, raced
            result = original_scalar(statement, *args, **kwargs)
            if (
                not raced
                and result is None
                and statement.column_descriptions[0]["entity"] is Job
            ):
                raced = True
                winner = _submit(store)
            return result

        session.scalar = scalar_with_winner
        if conflicting_payload:
            with pytest.raises(IdempotencyConflict):
                _submit(store, session=session, payload={"provider": "different"})
        else:
            duplicate = _submit(store, session=session)
            assert duplicate.job_id == winner.job_id
            assert not duplicate.created
        session.commit()

    assert winner.created
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_unknown_payload_hash_version_fails_closed(tmp_path):
    store, sessions = _store(tmp_path)
    submitted = _submit(store)
    with sessions() as session:
        job = session.get(Job, submitted.job_id)
        job.payload_hash_version = 999
        session.commit()

    with pytest.raises(IdempotencyConflict, match="hash version"):
        _submit(store)


@pytest.mark.parametrize(
    "non_finite",
    [float("nan"), float("inf"), float("-inf")],
)
def test_canonical_payload_hash_rejects_non_finite_numbers(non_finite):
    with pytest.raises(ValueError, match="Out of range float values"):
        canonical_payload_hash({"value": non_finite}, {})


def test_omitted_available_at_is_stable_across_idempotent_retries(tmp_path):
    store, _ = _store(tmp_path)

    first = _submit(store, now=NOW)
    duplicate = _submit(store, now=NOW + timedelta(hours=1))

    assert not duplicate.created
    assert duplicate.job_id == first.job_id
    assert store.get(first.job_id).available_at == NOW


@pytest.mark.parametrize(
    "changed_guarantee",
    [
        {"max_attempts": 4},
        {"priority": 10},
        {"available_at": NOW + timedelta(minutes=5)},
        {"deadline_at": NOW + timedelta(hours=1)},
    ],
)
def test_idempotency_conflicts_when_execution_guarantees_change(
    tmp_path, changed_guarantee
):
    store, _ = _store(tmp_path)
    _submit(store)

    with pytest.raises(IdempotencyConflict, match="execution guarantees"):
        _submit(store, **changed_guarantee)


def test_equivalent_offset_timestamps_share_idempotency_and_normalize_to_utc(
    tmp_path,
):
    store, _ = _store(tmp_path)
    plus_two = timezone(timedelta(hours=2))
    available_at = NOW + timedelta(minutes=5)
    deadline_at = NOW + timedelta(hours=1)

    first = _submit(
        store,
        available_at=available_at,
        deadline_at=deadline_at,
        now=NOW.astimezone(plus_two),
    )
    duplicate = _submit(
        store,
        available_at=available_at.astimezone(plus_two),
        deadline_at=deadline_at.astimezone(plus_two),
        now=(NOW + timedelta(minutes=1)).astimezone(plus_two),
    )

    assert not duplicate.created
    assert duplicate.job_id == first.job_id
    job = store.get(first.job_id)
    assert job.available_at == available_at
    assert job.available_at.tzinfo == timezone.utc
    assert job.deadline_at == deadline_at
    assert job.deadline_at.tzinfo == timezone.utc


def test_owned_read_hides_missing_and_other_actor(tmp_path):
    store, _ = _store(tmp_path)
    submitted = _submit(store)

    owned = store.get_owned(
        submitted.job_id,
        owner_client_type="system",
        owner_actor_id="snapshot-scheduler",
    )
    assert owned.id == submitted.job_id
    with pytest.raises(JobNotFound, match="job not found"):
        store.get_owned(
            submitted.job_id,
            owner_client_type="system",
            owner_actor_id="other",
        )
    with pytest.raises(JobNotFound, match="job not found"):
        store.get_owned(
            uuid.uuid4(),
            owner_client_type="system",
            owner_actor_id="snapshot-scheduler",
        )


def test_owner_cancellation_terminalizes_queued_job(tmp_path):
    store, sessions = _store(tmp_path)
    submitted = _submit(store)

    assert store.cancel_owned(
        submitted.job_id,
        owner_client_type="system",
        owner_actor_id="snapshot-scheduler",
        now=NOW,
    )
    job = store.get(submitted.job_id)
    assert job.status == JobStatus.CANCELLED.value
    assert job.cancel_requested_at == NOW
    assert job.completed_at == NOW
    assert job.error_kind == FailureKind.CANCELLED.value
    assert job.error_code == "cancelled_by_owner"
    assert store.claim_due(worker_id="worker", now=NOW) is None
    assert not store.cancel_owned(
        submitted.job_id,
        owner_client_type="system",
        owner_actor_id="snapshot-scheduler",
        now=NOW,
    )
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 0


def test_owner_cancellation_revokes_active_attempt_and_fences_outputs(tmp_path):
    store, sessions = _store(tmp_path)
    submitted = _submit(store)
    claim = store.claim_due(worker_id="worker", lease_seconds=60, now=NOW)
    assert claim is not None

    assert store.cancel_owned(
        submitted.job_id,
        owner_client_type="system",
        owner_actor_id="snapshot-scheduler",
        now=NOW + timedelta(seconds=1),
    )
    assert not store.heartbeat(claim, now=NOW + timedelta(seconds=2))
    assert not store.succeed(
        claim,
        result={"fit_card_id": "must-not-commit"},
        now=NOW + timedelta(seconds=2),
    )
    assert not store.fail(
        claim,
        kind=FailureKind.PERMANENT,
        code="must_not_win",
        safe_message="must not win",
        now=NOW + timedelta(seconds=2),
    )
    job = store.get(submitted.job_id)
    assert job.status == JobStatus.CANCELLED.value
    assert job.result is None
    with sessions() as session:
        attempt = session.get(JobAttempt, claim.attempt_id)
    assert attempt is not None
    assert attempt.status == AttemptStatus.CANCELLED.value
    assert attempt.error_kind == FailureKind.CANCELLED.value


def test_owner_cancellation_hides_other_actor(tmp_path):
    store, _ = _store(tmp_path)
    submitted = _submit(store)

    with pytest.raises(JobNotFound, match="job not found"):
        store.cancel_owned(
            submitted.job_id,
            owner_client_type="system",
            owner_actor_id="other-scheduler",
            now=NOW,
        )
    assert store.get(submitted.job_id).status == JobStatus.QUEUED.value


def test_expired_lease_reclaims_and_fences_stale_attempt(tmp_path):
    store, sessions = _store(tmp_path)
    submitted = _submit(store)

    abandoned = store.claim_due(worker_id="worker-a", lease_seconds=30, now=NOW)
    assert abandoned is not None
    assert store.claim_due(
        worker_id="worker-b", lease_seconds=30, now=NOW + timedelta(seconds=29)
    ) is None

    reclaimed = store.claim_due(
        worker_id="worker-b", lease_seconds=30, now=NOW + timedelta(seconds=31)
    )
    assert reclaimed is not None
    assert reclaimed.job_id == abandoned.job_id
    assert reclaimed.attempt_id != abandoned.attempt_id
    assert reclaimed.attempt_number == 2

    assert not store.heartbeat(abandoned, now=NOW + timedelta(seconds=32))
    assert not store.succeed(
        abandoned,
        result={"snapshot_id": "stale"},
        now=NOW + timedelta(seconds=32),
    )
    assert store.succeed(
        reclaimed,
        result={"snapshot_id": "fresh"},
        now=NOW + timedelta(seconds=32),
    )

    job = store.get(submitted.job_id)
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.result == {"snapshot_id": "fresh"}
    with sessions() as session:
        attempts = session.scalars(
            select(JobAttempt).order_by(JobAttempt.attempt_number)
        ).all()
    assert [row.status for row in attempts] == [
        AttemptStatus.ABANDONED.value,
        AttemptStatus.SUCCEEDED.value,
    ]


def test_expired_lease_fences_worker_before_another_worker_reclaims(tmp_path):
    store, _ = _store(tmp_path)
    submitted = _submit(store)
    expired = store.claim_due(worker_id="worker-a", lease_seconds=30, now=NOW)
    assert expired is not None

    after_expiry = NOW + timedelta(seconds=31)
    assert not store.heartbeat(expired, now=after_expiry)
    assert not store.succeed(
        expired,
        result={"snapshot_id": "stale"},
        now=after_expiry,
    )
    assert not store.fail(
        expired,
        kind=FailureKind.PERMANENT,
        code="stale_worker",
        safe_message="stale worker",
        now=after_expiry,
    )
    still_reclaimable = store.get(submitted.job_id)
    assert still_reclaimable.status == JobStatus.RUNNING.value
    assert still_reclaimable.active_attempt_id == expired.attempt_id

    reclaimed = store.claim_due(worker_id="worker-b", now=after_expiry)
    assert reclaimed is not None
    assert reclaimed.attempt_number == 2
    assert reclaimed.attempt_id != expired.attempt_id


def test_completion_rechecks_wall_clock_and_rolls_back_domain_writes(
    tmp_path,
    monkeypatch,
):
    store, _ = _store(tmp_path)
    submitted = _submit(store)
    claim = store.claim_due(worker_id="worker-a", lease_seconds=30, now=NOW)
    assert claim is not None
    clock = iter(
        [
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=2),
            NOW + timedelta(seconds=31),
            NOW + timedelta(seconds=31),
        ]
    )
    monkeypatch.setattr(store, "_database_now", lambda _session: next(clock))

    def domain_write(_session, job):
        job.stage = "domain-write-that-must-roll-back"
        return {"snapshot_id": "too-late"}

    assert not store.succeed_with(claim, domain_write)
    still_running = store.get(submitted.job_id)
    assert still_running.status == JobStatus.RUNNING.value
    assert still_running.stage == "claimed"
    assert still_running.result is None

    reclaimed = store.claim_due(
        worker_id="worker-b",
        now=NOW + timedelta(seconds=31),
    )
    assert reclaimed is not None
    assert reclaimed.attempt_id != claim.attempt_id


@pytest.mark.parametrize("method", ["commit", "rollback", "close", "begin"])
def test_fenced_domain_callback_cannot_end_the_job_transaction(tmp_path, method):
    store, _ = _store(tmp_path)
    submitted = _submit(store)
    claim = store.claim_due(worker_id="worker", lease_seconds=30, now=NOW)
    assert claim is not None

    def premature_transaction_end(session, job):
        job.stage = "must-roll-back"
        session.flush()
        getattr(session, method)()
        return {"fit_card_id": "must-not-commit"}

    with pytest.raises(AttributeError):
        store.succeed_with(claim, premature_transaction_end, now=NOW)

    job = store.get(submitted.job_id)
    assert job.status == JobStatus.RUNNING.value
    assert job.stage == "claimed"
    assert job.result is None


@pytest.mark.parametrize("statement", ["COMMIT", "ROLLBACK"])
@pytest.mark.parametrize("method", ["scalar", "scalars"])
def test_fenced_domain_callback_rejects_transaction_control_sql(
    tmp_path, statement, method
):
    store, _ = _store(tmp_path)
    submitted = _submit(store)
    claim = store.claim_due(worker_id="worker", lease_seconds=30, now=NOW)
    assert claim is not None

    def attempts_transaction_control(session, job):
        job.stage = "must-roll-back"
        session.flush()
        getattr(session, method)(text(statement))
        return {"fit_card_id": "must-not-commit"}

    with pytest.raises(TypeError, match="typed SELECT statements only"):
        store.succeed_with(claim, attempts_transaction_control, now=NOW)

    job = store.get(submitted.job_id)
    assert job.status == JobStatus.RUNNING.value
    assert job.stage == "claimed"
    assert job.result is None


@pytest.mark.parametrize("method", ["execute", "get_bind"])
def test_fenced_domain_callback_does_not_expose_raw_connection_escape_hatches(
    tmp_path, method
):
    store, _ = _store(tmp_path)
    submitted = _submit(store)
    claim = store.claim_due(worker_id="worker", lease_seconds=30, now=NOW)
    assert claim is not None

    def accesses_escape_hatch(session, job):
        getattr(session, method)(text("SELECT 1"))
        return {"fit_card_id": "must-not-commit"}

    with pytest.raises(AttributeError):
        store.succeed_with(claim, accesses_escape_hatch, now=NOW)

    job = store.get(submitted.job_id)
    assert job.status == JobStatus.RUNNING.value
    assert job.result is None


def test_fenced_domain_callback_exception_rolls_back_flushed_writes(tmp_path):
    store, _ = _store(tmp_path)
    submitted = _submit(store)
    claim = store.claim_due(worker_id="worker", lease_seconds=30, now=NOW)
    assert claim is not None

    def raises_after_flush(session, job):
        job.stage = "must-roll-back"
        session.flush()
        raise RuntimeError("simulated domain failure")

    with pytest.raises(RuntimeError, match="simulated domain failure"):
        store.succeed_with(claim, raises_after_flush, now=NOW)

    job = store.get(submitted.job_id)
    assert job.status == JobStatus.RUNNING.value
    assert job.stage == "claimed"
    assert job.result is None


@pytest.mark.parametrize("operation", ["heartbeat", "fail"])
def test_heartbeat_and_failure_use_final_statement_time_fence(
    tmp_path,
    monkeypatch,
    operation,
):
    store, _ = _store(tmp_path)
    submitted = _submit(store)
    claim = store.claim_due(worker_id="worker-a", lease_seconds=30, now=NOW)
    assert claim is not None
    clock = iter(
        [
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=2),
            NOW + timedelta(seconds=31),
            NOW + timedelta(seconds=31),
        ]
    )
    monkeypatch.setattr(store, "_database_now", lambda _session: next(clock))

    if operation == "heartbeat":
        accepted = store.heartbeat(claim, stage="too-late")
    else:
        accepted = store.fail(
            claim,
            kind=FailureKind.PERMANENT,
            code="too_late",
            safe_message="too late",
        )

    assert not accepted
    still_running = store.get(submitted.job_id)
    assert still_running.status == JobStatus.RUNNING.value
    assert still_running.stage == "claimed"
    assert still_running.error_code is None
    reclaimed = store.claim_due(
        worker_id="worker-b",
        now=NOW + timedelta(seconds=31),
    )
    assert reclaimed is not None
    assert reclaimed.attempt_id != claim.attempt_id


@pytest.mark.parametrize("lease_seconds", [0, -1])
def test_lease_seconds_must_be_positive_for_claim_and_heartbeat(
    tmp_path, lease_seconds
):
    store, _ = _store(tmp_path)
    _submit(store)

    with pytest.raises(ValueError, match="lease_seconds must be positive"):
        store.claim_due(
            worker_id="worker", lease_seconds=lease_seconds, now=NOW
        )

    claim = store.claim_due(worker_id="worker", now=NOW)
    assert claim is not None
    with pytest.raises(ValueError, match="lease_seconds must be positive"):
        store.heartbeat(claim, lease_seconds=lease_seconds, now=NOW)


def test_deadline_terminalizes_queued_job_before_it_becomes_available(tmp_path):
    store, sessions = _store(tmp_path)
    deadline = NOW + timedelta(minutes=1)
    submitted = _submit(
        store,
        available_at=NOW + timedelta(hours=1),
        deadline_at=deadline,
    )

    assert store.claim_due(worker_id="worker", now=deadline) is None
    job = store.get(submitted.job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.error_kind == FailureKind.PERMANENT.value
    assert job.error_code == "deadline_exceeded"
    assert job.completed_at == deadline
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 0


def test_deadline_terminalizes_retry_wait_job(tmp_path):
    store, sessions = _store(tmp_path)
    deadline = NOW + timedelta(seconds=10)
    submitted = _submit(store, deadline_at=deadline)
    claim = store.claim_due(worker_id="worker", now=NOW)
    assert claim is not None
    assert store.fail(
        claim,
        kind=FailureKind.TRANSIENT,
        code="provider_timeout",
        safe_message="provider timed out",
        backoff_seconds=60,
        now=NOW,
    )
    assert store.get(submitted.job_id).status == JobStatus.RETRY_WAIT.value

    assert store.claim_due(worker_id="worker", now=deadline) is None
    job = store.get(submitted.job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.error_code == "deadline_exceeded"
    with sessions() as session:
        attempt = session.scalar(select(JobAttempt))
    assert attempt is not None
    assert attempt.status == AttemptStatus.RETRY_WAIT.value


def test_deadline_fences_running_worker_and_abandons_attempt(tmp_path):
    store, sessions = _store(tmp_path)
    deadline = NOW + timedelta(seconds=10)
    submitted = _submit(store, deadline_at=deadline)
    claim = store.claim_due(worker_id="worker", lease_seconds=60, now=NOW)
    assert claim is not None

    assert not store.heartbeat(claim, now=deadline)
    assert not store.succeed(
        claim,
        result={"snapshot_id": "too-late"},
        now=deadline,
    )
    assert not store.fail(
        claim,
        kind=FailureKind.TRANSIENT,
        code="provider_timeout",
        safe_message="provider timed out",
        now=deadline,
    )
    job = store.get(submitted.job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.error_code == "deadline_exceeded"
    assert job.completed_at == deadline
    with sessions() as session:
        attempt = session.get(JobAttempt, claim.attempt_id)
    assert attempt is not None
    assert attempt.status == AttemptStatus.ABANDONED.value
    assert attempt.error_code == "deadline_exceeded"


def test_expired_deadline_is_terminal_at_submission(tmp_path):
    store, sessions = _store(tmp_path)
    submitted = _submit(store, deadline_at=NOW)

    job = store.get(submitted.job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.attempt_count == 0
    assert job.error_code == "deadline_exceeded"
    assert store.claim_due(worker_id="worker", now=NOW) is None
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 0


def test_renewed_final_lease_is_not_exhausted_by_stale_reaper(tmp_path):
    store, _ = _store(tmp_path)
    submitted = _submit(store, max_attempts=1)
    claim = store.claim_due(worker_id="worker", lease_seconds=30, now=NOW)
    assert claim is not None

    assert store.heartbeat(
        claim,
        lease_seconds=60,
        now=NOW + timedelta(seconds=29),
    )
    assert store.claim_due(
        worker_id="reaper-racer",
        now=NOW + timedelta(seconds=31),
    ) is None
    running = store.get(submitted.job_id)
    assert running.status == JobStatus.RUNNING.value
    assert running.active_attempt_id == claim.attempt_id
    assert running.lease_expires_at == NOW + timedelta(seconds=89)

    assert store.succeed(
        claim,
        result={"snapshot_id": "winner"},
        now=NOW + timedelta(seconds=32),
    )
    assert store.get(submitted.job_id).status == JobStatus.SUCCEEDED.value


def test_final_expired_lease_keeps_attempt_cause_separate_from_job_exhaustion(
    tmp_path,
):
    store, sessions = _store(tmp_path)
    submitted = _submit(store, max_attempts=1)
    claim = store.claim_due(worker_id="worker", lease_seconds=30, now=NOW)
    assert claim is not None

    assert store.claim_due(
        worker_id="reaper",
        now=NOW + timedelta(seconds=31),
    ) is None
    job = store.get(submitted.job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.error_kind == FailureKind.RETRY_EXHAUSTED.value
    assert job.error_code == "claim_budget_exhausted"
    with sessions() as session:
        attempt = session.get(JobAttempt, claim.attempt_id)
    assert attempt is not None
    assert attempt.status == AttemptStatus.ABANDONED.value
    assert attempt.error_kind == FailureKind.TRANSIENT.value
    assert attempt.error_code == "lease_expired"


def test_claim_exposes_job_correlation_and_attempt_trace_ids(tmp_path):
    store, sessions = _store(tmp_path)
    submitted = _submit(store, correlation_id="job-correlation")

    claim = store.claim_due(worker_id="worker", now=NOW)
    assert claim is not None
    assert claim.job_correlation_id == "job-correlation"
    assert claim.attempt_trace_id != claim.job_correlation_id
    with sessions() as session:
        attempt = session.get(JobAttempt, claim.attempt_id)
    assert attempt is not None
    assert attempt.attempt_trace_id == claim.attempt_trace_id
    assert store.get(submitted.job_id).correlation_id == claim.job_correlation_id


def test_transient_backoff_is_due_bounded_and_audited(tmp_path):
    store, sessions = _store(tmp_path)
    submitted = _submit(store, max_attempts=2)

    first = store.claim_due(worker_id="worker", now=NOW)
    assert first is not None
    assert store.fail(
        first,
        kind=FailureKind.TRANSIENT,
        code="provider_timeout",
        safe_message="provider timed out",
        now=NOW,
    )
    waiting = store.get(submitted.job_id)
    assert waiting.status == JobStatus.RETRY_WAIT.value
    available_at = waiting.available_at
    if available_at.tzinfo is None:  # SQLite loses timezone metadata.
        available_at = available_at.replace(tzinfo=timezone.utc)
    assert available_at == NOW + timedelta(seconds=1)
    assert store.claim_due(worker_id="worker", now=NOW) is None

    second = store.claim_due(
        worker_id="worker", now=NOW + timedelta(seconds=1)
    )
    assert second is not None
    assert store.fail(
        second,
        kind=FailureKind.TRANSIENT,
        code="provider_timeout",
        safe_message="provider timed out",
        now=NOW + timedelta(seconds=1),
    )
    failed = store.get(submitted.job_id)
    assert failed.status == JobStatus.FAILED.value
    assert failed.error_kind == FailureKind.RETRY_EXHAUSTED.value
    assert failed.error_code == "retry_budget_exhausted"
    with sessions() as session:
        attempts = session.scalars(
            select(JobAttempt).order_by(JobAttempt.attempt_number)
        ).all()
    assert attempts[-1].status == AttemptStatus.FAILED.value
    assert attempts[-1].error_kind == FailureKind.TRANSIENT.value
    assert attempts[-1].error_code == "provider_timeout"
    assert attempts[-1].safe_error_message == "provider timed out"
    assert store.claim_due(worker_id="worker", now=NOW + timedelta(days=1)) is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"code": ""}, "failure code is required"),
        ({"code": "   "}, "failure code is required"),
        ({"code": "x" * 65}, "failure code exceeds 64 characters"),
        ({"safe_message": ""}, "safe error message is required"),
        ({"safe_message": "x" * 257}, "safe error message exceeds 256 characters"),
        ({"backoff_seconds": -1}, "backoff_seconds must be nonnegative"),
    ],
)
def test_failure_inputs_are_validated_before_mutating_the_job(
    tmp_path, overrides, message
):
    store, sessions = _store(tmp_path)
    submitted = _submit(store)
    claim = store.claim_due(worker_id="worker", now=NOW)
    assert claim is not None
    values = {
        "kind": FailureKind.TRANSIENT,
        "code": "provider_timeout",
        "safe_message": "provider timed out",
        "now": NOW,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        store.fail(claim, **values)

    job = store.get(submitted.job_id)
    assert job.status == JobStatus.RUNNING.value
    assert job.active_attempt_id == claim.attempt_id
    with sessions() as session:
        attempt = session.get(JobAttempt, claim.attempt_id)
    assert attempt is not None
    assert attempt.status == AttemptStatus.RUNNING.value


def test_permanent_failure_is_terminal_and_duplicate_completion_is_fenced(
    tmp_path,
):
    store, _ = _store(tmp_path)
    submitted = _submit(store)
    claim = store.claim_due(worker_id="worker", now=NOW)
    assert claim is not None

    assert store.fail(
        claim,
        kind=FailureKind.PERMANENT,
        code="schema_drift",
        safe_message="provider schema is unsupported",
        now=NOW,
    )
    assert not store.succeed(claim, result={"unexpected": True}, now=NOW)
    job = store.get(submitted.job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.attempt_count == 1


def test_concurrent_claimers_get_at_most_one_attempt(tmp_path):
    store, sessions = _store(tmp_path)
    _submit(store)

    def claim(worker: str):
        return store.claim_due(worker_id=worker, lease_seconds=30, now=NOW)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, [f"worker-{index}" for index in range(8)]))

    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 1
