"""Focused offline source-job regressions for the public v3.1 reference."""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from el.domain.tables import (
    Base,
    Job,
    SourceCandidateChoice,
    SourceInterpretation,
    SourceInterpretationRequest,
)
from el.product.wiring import (
    HARBOR_CANDIDATE,
    MULTI_THESIS_FIXTURE,
    ORCHARD_CANDIDATE,
    build_services,
    local_actor,
)
from el.sourceinterpretation.jobs import SourceInterpretationWorker


def _env(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'source-jobs.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    services = build_services(mode="fixture", session_factory=sessions)
    return SimpleNamespace(
        sessions=sessions, services=services, actor=local_actor(sessions)
    )


def _submit(env, *, text=MULTI_THESIS_FIXTURE, key="source-job"):
    return env.services.source_interpretation_jobs.submit(
        text,
        source_url=None,
        idempotency_key=key,
        owner_client_type=env.actor.client_type.value,
        owner_actor_id=env.actor.actor_id,
        agent_client_id=env.actor.agent_client_id,
        owner_user_id=env.actor.user_id,
    )


def _worker(env, worker_id="source-job-worker"):
    return SourceInterpretationWorker(
        jobs=env.services.jobs,
        source_interpretation=env.services.source_interpretation,
        session_factory=env.sessions,
        worker_id=worker_id,
    )


def _count(env, table) -> int:
    with env.sessions() as session:
        return session.scalar(select(func.count()).select_from(table)) or 0


def test_fixture_source_job_queues_then_preserves_ordered_quotes_and_human_gate(
    tmp_path,
):
    env = _env(tmp_path)
    interpreter = env.services.source_interpretation._interpreter
    submitted = _submit(env)
    assert submitted.status == "queued"
    assert interpreter.calls == 0
    assert _count(env, SourceInterpretation) == 0

    assert _worker(env).run_once().status == "succeeded"
    status = env.services.source_interpretation_jobs.get_owned(
        submitted.job_id,
        owner_client_type=env.actor.client_type.value,
        owner_actor_id=env.actor.actor_id,
        agent_client_id=env.actor.agent_client_id,
    )
    assert status.interpretation is not None
    assert [candidate.ordinal for candidate in status.interpretation.candidates] == [1, 2]
    assert [
        candidate.selected_source_quote
        for candidate in status.interpretation.candidates
    ] == [HARBOR_CANDIDATE, ORCHARD_CANDIDATE]
    assert _count(env, SourceCandidateChoice) == 0


def test_privacy_refusal_has_no_raw_source_or_provider_call(tmp_path):
    env = _env(tmp_path)
    interpreter = env.services.source_interpretation._interpreter
    submitted = _submit(
        env,
        text="I am relying on confidential data for this synthetic thesis.",
        key="privacy-refusal",
    )
    with env.sessions() as session:
        request = session.get(
            SourceInterpretationRequest, submitted.source_interpretation_request_id
        )
        assert request.input_text is None
        assert request.input_digest is None
        assert request.source_url is None
        assert request.privacy_refusal_code is not None

    assert _worker(env).run_once().status == "succeeded"
    status = env.services.source_interpretation_jobs.get_owned(
        submitted.job_id,
        owner_client_type=env.actor.client_type.value,
        owner_actor_id=env.actor.actor_id,
        agent_client_id=env.actor.agent_client_id,
    )
    assert status.interpretation is not None
    assert status.interpretation.outcome == "refusal"
    assert interpreter.calls == 0


def test_pin_drift_fails_before_fixture_provider_call(tmp_path):
    env = _env(tmp_path)
    submitted = _submit(env, key="pin-drift")
    source = env.services.source_interpretation
    interpreter = source._interpreter
    source._prompt_policy_version = "synthetic-drift"
    source._system_variant_id = "fixture/source-drift"

    assert _worker(env).run_once().status == "failed"
    job = env.services.jobs.get(submitted.job_id)
    assert job.error_code == "source_pins_mismatch"
    assert interpreter.calls == 0


def test_stale_source_attempt_cannot_publish_an_interpretation(tmp_path):
    env = _env(tmp_path)
    submitted = _submit(env, key="stale-fence")
    jobs = env.services.jobs
    start = jobs.get(submitted.job_id).created_at + timedelta(seconds=1)
    stale = jobs.claim_due(
        worker_id="stale-worker",
        lease_seconds=1,
        job_types=["source_interpretation_v1"],
        now=start,
    )
    assert stale is not None
    current = jobs.claim_due(
        worker_id="current-worker",
        lease_seconds=30,
        job_types=["source_interpretation_v1"],
        now=start + timedelta(seconds=2),
    )
    assert current is not None
    computation = env.services.source_interpretation.compute_allowed(
        MULTI_THESIS_FIXTURE, source_url=None
    )

    def stale_write(session, job):
        outcome = env.services.source_interpretation.persist_in_session(
            session,
            computation,
            client_type=env.actor.client_type.value,
            agent_client_id=env.actor.agent_client_id,
            source_interpretation_request_id=uuid.UUID(
                stale.payload["source_interpretation_request_id"]
            ),
            job_id=job.id,
        )
        return {"source_interpretation_id": str(outcome.source_interpretation_id)}

    assert jobs.succeed_with(stale, stale_write, now=start + timedelta(seconds=2)) is False
    assert _count(env, SourceInterpretation) == 0
    assert jobs.get(submitted.job_id).status == "running"
