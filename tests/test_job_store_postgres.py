"""PostgreSQL contention gate for the durable job ledger.

These tests are intentionally opt-in because they require a disposable
PostgreSQL database.  Set ``FITCHECK_POSTGRES_TEST_URL`` to run them.  The
suite creates a uniquely named schema, migrates it to Alembic head, and drops
that schema when the module finishes; it never uses the database's public
schema for FitCheck rows.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Interval,
    create_engine,
    func,
    literal,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from el.domain.tables import Job, JobAttempt, User
from el.jobs import (
    AttemptStatus,
    FailureKind,
    IdempotencyConflict,
    JobClaim,
    JobStatus,
    JobStore,
)

TEST_URL_ENV = "FITCHECK_POSTGRES_TEST_URL"
REPO_ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


@dataclass(frozen=True)
class PostgresHarness:
    engine: Engine
    sessions: sessionmaker[Session]
    store: JobStore


def _restore_env(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


@pytest.fixture(scope="module")
def postgres_harness() -> Iterator[PostgresHarness]:
    raw_url = (os.environ.get(TEST_URL_ENV) or "").strip()
    if not raw_url:
        pytest.skip(f"set {TEST_URL_ENV} to run PostgreSQL contention tests")

    base_url = make_url(raw_url)
    if base_url.get_backend_name() != "postgresql":
        pytest.fail(f"{TEST_URL_ENV} must use the PostgreSQL dialect")

    schema = f"fitcheck_pg_gate_{uuid.uuid4().hex}"
    admin_engine = create_engine(base_url, isolation_level="AUTOCOMMIT")
    engine: Engine | None = None
    schema_created = False
    try:
        with admin_engine.connect() as connection:
            connection.execute(CreateSchema(schema))
        schema_created = True

        options = (
            f"-csearch_path={schema} "
            "-clock_timeout=5000 "
            "-cstatement_timeout=15000"
        )
        database_url = base_url.render_as_string(hide_password=False)

        previous_database_url = os.environ.get("DATABASE_URL")
        previous_fitcheck_url = os.environ.get("FITCHECK_DB_URL")
        previous_pgoptions = os.environ.get("PGOPTIONS")
        try:
            os.environ["DATABASE_URL"] = database_url
            os.environ.pop("FITCHECK_DB_URL", None)
            os.environ["PGOPTIONS"] = options
            config = Config(str(REPO_ROOT / "alembic.ini"))
            config.set_main_option(
                "script_location", str(REPO_ROOT / "migrations")
            )
            command.upgrade(config, "head")
        finally:
            _restore_env("DATABASE_URL", previous_database_url)
            _restore_env("FITCHECK_DB_URL", previous_fitcheck_url)
            _restore_env("PGOPTIONS", previous_pgoptions)

        engine = create_engine(
            base_url,
            connect_args={"options": options},
            pool_size=32,
            max_overflow=16,
            pool_timeout=10,
            pool_pre_ping=True,
        )
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        yield PostgresHarness(
            engine=engine,
            sessions=sessions,
            store=JobStore(sessions),
        )
    finally:
        if engine is not None:
            engine.dispose()
        if schema_created:
            with admin_engine.connect() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


@pytest.fixture(autouse=True)
def isolate_job_rows(postgres_harness: PostgresHarness) -> Iterator[None]:
    with postgres_harness.engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE jobs CASCADE"))
    yield
    with postgres_harness.engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE jobs CASCADE"))


def _submit(
    store: JobStore,
    *,
    key: str,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 3,
):
    return store.submit_or_get(
        job_type="market_universe_refresh",
        owner_client_type="system",
        owner_actor_id="postgres-gate",
        idempotency_key=key,
        payload=payload or {"provider": "fixture", "cutoff": key},
        pinned_manifest={"normalization_policy_version": "market-v1"},
        max_attempts=max_attempts,
    )


def _parallel(count: int, operation: Callable[[int], Any]) -> list[Any]:
    barrier = threading.Barrier(count)

    def synchronized(index: int):
        barrier.wait(timeout=10)
        return operation(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(synchronized, range(count)))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _database_now(sessions: sessionmaker[Session]) -> datetime:
    with sessions() as session:
        value = session.scalar(select(func.clock_timestamp()))
    assert isinstance(value, datetime)
    return _aware(value)


def _wait_until(
    sessions: sessionmaker[Session],
    predicate: Callable[[datetime], bool],
    *,
    timeout: float = 5,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate(_database_now(sessions)):
        if time.monotonic() >= deadline:
            pytest.fail("database clock did not reach the race boundary")
        time.sleep(0.005)


def _shorten_lease(
    sessions: sessionmaker[Session], claim: JobClaim, *, milliseconds: int
) -> datetime:
    with sessions.begin() as session:
        expiry_expression = func.clock_timestamp() + literal(
            timedelta(milliseconds=milliseconds), type_=Interval()
        )
        expires_at = session.scalar(select(expiry_expression))
        assert isinstance(expires_at, datetime)
        session.execute(
            update(Job)
            .where(
                Job.id == claim.job_id,
                Job.active_attempt_id == claim.attempt_id,
            )
            .values(lease_expires_at=expires_at)
        )
        session.execute(
            update(JobAttempt)
            .where(JobAttempt.id == claim.attempt_id)
            .values(lease_expires_at=expires_at)
        )
    return _aware(expires_at)


@contextmanager
def _locked_job(
    sessions: sessionmaker[Session], job_id: uuid.UUID
) -> Iterator[None]:
    with sessions() as session:
        row = session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        assert row is not None
        yield
        session.commit()


class _LockSignalingStore(JobStore):
    def __init__(
        self,
        sessions: sessionmaker[Session],
        before_lock: threading.Event,
    ):
        super().__init__(sessions)
        self._before_lock = before_lock

    def _lock_claim(self, session: Session, claim: JobClaim) -> Job | None:
        self._before_lock.set()
        return JobStore._lock_claim(session, claim)


def test_concurrent_same_key_and_conflicting_submissions(
    postgres_harness: PostgresHarness,
) -> None:
    store = postgres_harness.store

    for round_number in range(8):
        key = f"same-key-{round_number}"
        results = _parallel(16, lambda _index: _submit(store, key=key))
        assert sum(result.created for result in results) == 1
        assert len({result.job_id for result in results}) == 1

    for round_number in range(8):
        key = f"conflicting-key-{round_number}"
        barrier = threading.Barrier(2)

        def submit_variant(variant: int):
            barrier.wait(timeout=10)
            try:
                result = _submit(
                    store,
                    key=key,
                    payload={"provider": "fixture", "variant": variant},
                )
                return "created", result.job_id
            except IdempotencyConflict:
                return "conflict", None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit_variant, (1, 2)))
        assert [outcome[0] for outcome in outcomes].count("created") == 1
        assert [outcome[0] for outcome in outcomes].count("conflict") == 1

    with postgres_harness.sessions() as session:
        rows = session.scalars(select(Job)).all()
    assert len(rows) == 16


def test_concurrent_claimers_create_only_one_attempt_per_job(
    postgres_harness: PostgresHarness,
) -> None:
    store = postgres_harness.store

    for round_number in range(12):
        submitted = _submit(store, key=f"single-claim-{round_number}")
        claims = _parallel(
            16,
            lambda index: store.claim_due(
                worker_id=f"single-worker-{round_number}-{index}",
                lease_seconds=30,
                contention_retries=16,
            ),
        )
        winners = [claim for claim in claims if claim is not None]
        assert len(winners) == 1
        assert winners[0].job_id == submitted.job_id

        with postgres_harness.sessions() as session:
            attempts = session.scalars(
                select(JobAttempt).where(JobAttempt.job_id == submitted.job_id)
            ).all()
            job = session.get(Job, submitted.job_id)
        assert job is not None
        assert job.attempt_count == 1
        assert job.active_attempt_id == winners[0].attempt_id
        assert len(attempts) == 1


def test_concurrent_claimers_drain_multiple_jobs_without_duplicates(
    postgres_harness: PostgresHarness,
) -> None:
    store = postgres_harness.store

    for round_number in range(6):
        job_ids = {
            _submit(store, key=f"multi-{round_number}-{index}").job_id
            for index in range(8)
        }
        claims = _parallel(
            12,
            lambda index: store.claim_due(
                worker_id=f"multi-worker-{round_number}-{index}",
                lease_seconds=30,
                contention_retries=32,
            ),
        )
        winners = [claim for claim in claims if claim is not None]
        assert len(winners) == len(job_ids)
        assert {claim.job_id for claim in winners} == job_ids
        assert len({claim.attempt_id for claim in winners}) == len(job_ids)

        with postgres_harness.sessions() as session:
            attempts = session.scalars(
                select(JobAttempt).where(JobAttempt.job_id.in_(job_ids))
            ).all()
        assert len(attempts) == len(job_ids)
        assert {attempt.job_id for attempt in attempts} == job_ids


def test_heartbeat_blocked_across_expiry_cannot_revive_lease(
    postgres_harness: PostgresHarness,
) -> None:
    sessions = postgres_harness.sessions
    store = postgres_harness.store

    for round_number in range(3):
        submitted = _submit(store, key=f"blocked-heartbeat-{round_number}")
        expired = store.claim_due(
            worker_id=f"heartbeat-old-{round_number}", lease_seconds=1
        )
        assert expired is not None
        lease_expires_at = _aware(store.get(submitted.job_id).lease_expires_at)
        before_lock = threading.Event()
        signaling_store = _LockSignalingStore(sessions, before_lock)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with _locked_job(sessions, submitted.job_id):
                heartbeat = pool.submit(
                    signaling_store.heartbeat,
                    expired,
                    lease_seconds=30,
                    stage="must-not-be-written",
                )
                assert before_lock.wait(timeout=5)
                _wait_until(
                    sessions,
                    lambda now: now > lease_expires_at,
                )
            assert not heartbeat.result(timeout=5)

        reclaimed = store.claim_due(
            worker_id=f"heartbeat-new-{round_number}", lease_seconds=30
        )
        assert reclaimed is not None
        assert reclaimed.attempt_number == 2
        assert reclaimed.attempt_id != expired.attempt_id

        with sessions() as session:
            old_attempt = session.get(JobAttempt, expired.attempt_id)
            job = session.get(Job, submitted.job_id)
        assert old_attempt is not None
        assert old_attempt.status == AttemptStatus.ABANDONED.value
        assert job is not None
        assert job.active_attempt_id == reclaimed.attempt_id
        assert job.stage == "claimed"


def test_stale_completion_rolls_back_and_reclaimer_owns_result(
    postgres_harness: PostgresHarness,
) -> None:
    sessions = postgres_harness.sessions
    store = postgres_harness.store

    for round_number in range(3):
        submitted = _submit(store, key=f"stale-completion-{round_number}")
        stale = store.claim_due(
            worker_id=f"stale-worker-{round_number}", lease_seconds=1
        )
        assert stale is not None
        lease_expires_at = _aware(store.get(submitted.job_id).lease_expires_at)
        callback_entered = threading.Event()
        release_callback = threading.Event()
        reclaimer_started = threading.Event()

        def stale_domain_write(session: Session, job: Job) -> dict[str, Any]:
            job.stage = "stale-domain-write"
            callback_entered.set()
            assert release_callback.wait(timeout=5)
            return {"winner": "stale"}

        def reclaim() -> JobClaim | None:
            reclaimer_started.set()
            return store.claim_due(
                worker_id=f"fresh-worker-{round_number}",
                lease_seconds=30,
                contention_retries=32,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            stale_completion = pool.submit(
                store.succeed_with, stale, stale_domain_write
            )
            assert callback_entered.wait(timeout=5)
            _wait_until(sessions, lambda now: now > lease_expires_at)
            fresh_claim = pool.submit(reclaim)
            assert reclaimer_started.wait(timeout=5)
            time.sleep(0.025)
            release_callback.set()
            assert not stale_completion.result(timeout=10)
            fresh = fresh_claim.result(timeout=10)

        assert fresh is not None
        assert fresh.attempt_number == 2
        assert fresh.attempt_id != stale.attempt_id
        assert not store.heartbeat(stale, lease_seconds=30)
        assert not store.fail(
            stale,
            kind=FailureKind.PERMANENT,
            code="stale_attempt",
            safe_message="stale attempt",
        )
        assert not store.succeed(stale, result={"winner": "stale-again"})
        assert store.succeed(fresh, result={"winner": "fresh"})

        job = store.get(submitted.job_id)
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.stage == "completed"
        assert job.result == {"winner": "fresh"}
        with sessions() as session:
            attempts = session.scalars(
                select(JobAttempt)
                .where(JobAttempt.job_id == submitted.job_id)
                .order_by(JobAttempt.attempt_number)
            ).all()
        assert [attempt.status for attempt in attempts] == [
            AttemptStatus.ABANDONED.value,
            AttemptStatus.SUCCEEDED.value,
        ]


def test_cancellation_and_fenced_completion_have_exactly_one_winner(
    postgres_harness: PostgresHarness,
) -> None:
    sessions = postgres_harness.sessions
    store = postgres_harness.store

    for round_number in range(12):
        submitted = _submit(store, key=f"cancel-complete-{round_number}")
        claim = store.claim_due(
            worker_id=f"cancel-complete-worker-{round_number}",
            lease_seconds=30,
        )
        assert claim is not None
        marker_email = f"cancel-complete-{uuid.uuid4().hex}@example.test"
        barrier = threading.Barrier(2)

        def complete() -> bool:
            barrier.wait(timeout=5)

            def write_marker(session, _job):
                session.add(User(email=marker_email))
                session.flush()
                return {"winner": "completion"}

            return store.succeed_with(claim, write_marker)

        def cancel() -> bool:
            barrier.wait(timeout=5)
            return store.cancel_owned(
                submitted.job_id,
                owner_client_type="system",
                owner_actor_id="postgres-gate",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            completed_future = pool.submit(complete)
            cancelled_future = pool.submit(cancel)
            completed = completed_future.result(timeout=10)
            cancelled = cancelled_future.result(timeout=10)

        assert completed is not cancelled
        assert not store.heartbeat(claim, lease_seconds=30)
        with sessions() as session:
            job = session.get(Job, submitted.job_id)
            attempt = session.get(JobAttempt, claim.attempt_id)
            marker_count = session.scalar(
                select(func.count())
                .select_from(User)
                .where(User.email == marker_email)
            )
        assert job is not None
        assert attempt is not None
        if completed:
            assert job.status == JobStatus.SUCCEEDED.value
            assert job.result == {"winner": "completion"}
            assert attempt.status == AttemptStatus.SUCCEEDED.value
            assert marker_count == 1
        else:
            assert job.status == JobStatus.CANCELLED.value
            assert job.result is None
            assert attempt.status == AttemptStatus.CANCELLED.value
            assert marker_count == 0


def test_lease_renewal_and_final_attempt_reaper_never_split_brain(
    postgres_harness: PostgresHarness,
) -> None:
    sessions = postgres_harness.sessions
    store = postgres_harness.store

    for round_number in range(12):
        submitted = _submit(
            store,
            key=f"renewal-reaper-{round_number}",
            max_attempts=1,
        )
        claim = store.claim_due(
            worker_id=f"renewal-worker-{round_number}", lease_seconds=30
        )
        assert claim is not None
        expires_at = _shorten_lease(sessions, claim, milliseconds=250)
        _wait_until(
            sessions,
            lambda now: now >= expires_at - timedelta(milliseconds=12),
        )

        barrier = threading.Barrier(2)

        def renew():
            barrier.wait(timeout=5)
            return store.heartbeat(claim, lease_seconds=5, stage="renewed")

        def reap():
            barrier.wait(timeout=5)
            return store.claim_due(
                worker_id=f"reaper-{round_number}",
                lease_seconds=30,
                contention_retries=16,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            renewed_future = pool.submit(renew)
            reaped_future = pool.submit(reap)
            renewed = renewed_future.result(timeout=10)
            reaped = reaped_future.result(timeout=10)

        assert reaped is None
        with sessions() as session:
            job = session.get(Job, submitted.job_id)
            attempts = session.scalars(
                select(JobAttempt).where(JobAttempt.job_id == submitted.job_id)
            ).all()
        assert job is not None
        assert len(attempts) == 1
        attempt = attempts[0]

        if renewed:
            assert job.status == JobStatus.RUNNING.value
            assert job.active_attempt_id == claim.attempt_id
            assert job.lease_owner == f"renewal-worker-{round_number}"
            assert job.lease_expires_at is not None
            assert _aware(job.lease_expires_at) > _database_now(sessions)
            assert attempt.status == AttemptStatus.RUNNING.value
        else:
            assert job.status == JobStatus.FAILED.value
            assert job.error_kind == FailureKind.RETRY_EXHAUSTED.value
            assert job.error_code == "claim_budget_exhausted"
            assert job.active_attempt_id is None
            assert job.lease_owner is None
            assert job.lease_expires_at is None
            assert attempt.status == AttemptStatus.ABANDONED.value
            assert not store.succeed(claim, result={"winner": "stale"})
