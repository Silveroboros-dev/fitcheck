"""Offline, read-only resume of one account-owned discovery journey."""

from hashlib import sha256
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from el.domain.enums import ClientType
from el.domain.tables import (
    Base, Job, MarketAssessment, MarketChoice, MarketDisplayItem,
    MarketDisplaySet, MarketRulesCapture, NormalizationAttempt,
    NormalizationDecision, SourceInterpretation, SourceInterpretationRequest,
    SourceThesisCandidate, ThesisAnalysis, ThesisRevision, User,
)
from el.domain.vocabulary import vocabulary_violations
from el.mcp.contracts import NotFound
from el.mcp.vocab_guard import A7Violation
from el.product.api import ProductApi
from el.product.app import build_app
from el.product.wiring import HumanActor, build_services
from el.sourceinterpretation.jobs import SourceInterpretationWorker


CLEAN = "Gemini is going to be ranked #1 chatbot on LMSYS Chatbot Arena by the end of 2026."
BUY = "Acme will buy a competitor in 2026."
SELL = "Acme is going to sell its cloud unit by the end of 2026."


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    services = build_services(mode="fixture", session_factory=sessions)

    def actor(email):
        with sessions() as session:
            user = User(email=email)
            session.add(user)
            session.commit()
        return HumanActor(
            user_id=user.id, client_type=ClientType.HUMAN_UI,
            actor_id=f"user:{user.id}", agent_client_id=f"human_ui:{user.id}",
        )

    def client(identity):
        return TestClient(build_app(ProductApi(services, identity)))

    return sessions, services, actor, client


def _journey(client, *, choice="none"):
    source_response = client.post(
        "/api/v3/source-interpretations", json={"input_text": CLEAN}
    )
    assert source_response.status_code == 200, source_response.text
    source = source_response.json()
    candidate = source["candidates"][0]
    selected = client.post(
        f"/api/v3/source-interpretations/{source['source_interpretation_id']}/choice",
        json={"selection_kind": "candidate", "source_thesis_candidate_id": candidate["source_thesis_candidate_id"]},
    )
    assert selected.status_code == 200, selected.text
    attempt_response = client.post(
        f"/api/v3/source-thesis-candidates/{candidate['source_thesis_candidate_id']}/normalization-attempt"
    )
    assert attempt_response.status_code == 200, attempt_response.text
    attempt = attempt_response.json()
    accepted_response = client.post(
        f"/api/v3/normalization-attempts/{attempt['normalization_attempt_id']}/accept",
        json={"expected_input_digest": attempt["input_digest"]},
    )
    assert accepted_response.status_code == 200, accepted_response.text
    thesis_id = accepted_response.json()["thesis_analysis_id"]
    pool_response = client.post(f"/api/v3/theses/{thesis_id}/market-pool")
    assert pool_response.status_code == 200, pool_response.text
    pool = pool_response.json()
    assessment_id = pool["candidate_markets"][0]["market_assessment_id"] if choice == "market" else None
    choice_response = client.post(
        f"/api/v3/market-pools/{pool['market_display_set_id']}/choice",
        json={"selection_kind": choice, "market_assessment_id": assessment_id},
    )
    assert choice_response.status_code == 200, choice_response.text
    return source, thesis_id, pool, choice_response.json()


def _count(sessions, table):
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(table))


@pytest.mark.parametrize("selection", ["none", "market"])
def test_fresh_instance_discovers_and_reads_original_ids_without_writes(env, selection):
    sessions, services, actor, client_for = env
    identity = actor("resume-a@example.invalid")
    first = client_for(identity)
    source, thesis_id, pool, choice = _journey(first, choice=selection)
    tables = (SourceInterpretation, NormalizationAttempt, NormalizationDecision,
              ThesisAnalysis, MarketDisplaySet, MarketChoice)
    with sessions() as session:
        before = {table.__tablename__: session.scalar(select(func.count()).select_from(table)) for table in tables}

    fresh = client_for(identity)
    account = fresh.get("/api/v3/account-state")
    assert account.status_code == 200, account.text
    assert account.json() == {
        "latest_source_interpretation_id": source["source_interpretation_id"],
        "latest_source_interpretation_job_id": None,
        "latest_accepted_thesis_analysis_id": thesis_id,
        "latest_market_display_set_id": pool["market_display_set_id"],
        "latest_market_choice_id": choice["market_choice_id"],
    }
    source_read = fresh.get(f"/api/v3/source-interpretations/{source['source_interpretation_id']}")
    assert source_read.status_code == 200
    assert source_read.json() == source
    accepted = fresh.get(f"/api/v3/theses/{thesis_id}/accepted-state")
    assert accepted.status_code == 200
    assert accepted.json()["latest_market_display_set_id"] == pool["market_display_set_id"]
    assert accepted.json()["restored_source"]["original_source_text"] == CLEAN
    pool_read = fresh.get(f"/api/v3/market-pools/{pool['market_display_set_id']}")
    assert pool_read.status_code == 200
    assert pool_read.json() == pool
    choice_read = fresh.get(f"/api/v3/market-choices/{choice['market_choice_id']}")
    assert choice_read.status_code == 200
    assert choice_read.json() == choice
    assert choice_read.json()["selection_kind"] == selection
    with sessions() as session:
        after = {table.__tablename__: session.scalar(select(func.count()).select_from(table)) for table in tables}
    assert after == before


def test_explicit_older_thesis_resumes_its_latest_pool_without_writes(env, monkeypatch):
    from datetime import datetime, timezone

    sessions, services, actor, client_for = env
    identity = actor("saved-pools@example.invalid")
    client = client_for(identity)
    _, first_thesis, first_pool, _ = _journey(client)
    next_pool_response = client.post(f"/api/v3/theses/{first_thesis}/market-pool")
    assert next_pool_response.status_code == 200, next_pool_response.text
    next_pool = next_pool_response.json()
    _, other_thesis, other_pool, _ = _journey(client)
    assert other_thesis != first_thesis
    with sessions.begin() as session:
        session.get(MarketDisplaySet, uuid.UUID(first_pool["market_display_set_id"])).created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session.get(MarketDisplaySet, uuid.UUID(next_pool["market_display_set_id"])).created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)

    def forbidden(*args, **kwargs):
        raise AssertionError("resume must not perform a new assessment")

    monkeypatch.setattr(services.market_pool, "build", forbidden)
    tables = (NormalizationDecision, MarketAssessment, MarketDisplaySet, MarketChoice)
    before = {table: _count(sessions, table) for table in tables}
    state = client.get(f"/api/v3/theses/{first_thesis}/accepted-state")
    assert state.status_code == 200
    assert state.json()["latest_market_display_set_id"] == next_pool["market_display_set_id"]
    assert state.json()["latest_market_display_set_id"] != other_pool["market_display_set_id"]
    restored = client.get(f"/api/v3/market-pools/{next_pool['market_display_set_id']}")
    assert restored.status_code == 200
    assert restored.json() == next_pool
    assert {table: _count(sessions, table) for table in tables} == before


def test_accepted_wording_keeps_canonical_summary_and_vocabulary_boundary(env):
    sessions, _, actor, client_for = env
    client = client_for(actor("wording@example.invalid"))
    _, thesis_id, pool, _ = _journey(client)
    with sessions.begin() as session:
        thesis = session.get(ThesisAnalysis, uuid.UUID(thesis_id))
        canonical = thesis.normalized_claim_summary
        structure = dict(thesis.extracted_structure)
        structure["contractible_version"] = "A system is launched or demonstrated by 2028."
        thesis.extracted_structure = structure
    for path in (
        f"/api/v3/theses/{thesis_id}/accepted-state",
        f"/api/v3/market-pools/{pool['market_display_set_id']}",
    ):
        out = client.get(path)
        assert out.status_code == 200
        assert out.json()["accepted_thesis_summary"] == canonical
        assert out.json()["accepted_thesis_wording"] == structure["contractible_version"]
    with sessions.begin() as session:
        thesis = session.get(ThesisAnalysis, uuid.UUID(thesis_id))
        thesis.extracted_structure = {**structure, "contractible_version": BUY}
    out = client.get(f"/api/v3/theses/{thesis_id}/accepted-state")
    assert out.status_code == 200
    assert out.json()["accepted_thesis_wording"] == canonical


def test_foreign_uuid_and_cross_client_reads_are_indistinguishable_from_missing(env):
    sessions, _, actor, client_for = env
    a = actor("resume-a@example.invalid")
    b = actor("resume-b@example.invalid")
    source, thesis_id, pool, choice = _journey(client_for(a))
    b_client = client_for(b)
    assert b_client.get("/api/v3/account-state").json() == {
        "latest_source_interpretation_id": None,
        "latest_source_interpretation_job_id": None,
        "latest_accepted_thesis_analysis_id": None,
        "latest_market_display_set_id": None,
        "latest_market_choice_id": None,
    }
    for path, foreign_id in (
        (f"/api/v3/source-interpretations/{source['source_interpretation_id']}", source['source_interpretation_id']),
        (f"/api/v3/theses/{thesis_id}/accepted-state", thesis_id),
        (f"/api/v3/market-pools/{pool['market_display_set_id']}", pool['market_display_set_id']),
        (f"/api/v3/market-choices/{choice['market_choice_id']}", choice['market_choice_id']),
    ):
        missing = path.replace(foreign_id, str(uuid.uuid4()))
        assert b_client.get(path).status_code == b_client.get(missing).status_code == 404
        assert b_client.get(path).json() == b_client.get(missing).json() == {"error": "not_found"}
    candidate_id = source["candidates"][0]["source_thesis_candidate_id"]
    with sessions() as session:
        attempt_id = session.scalar(select(NormalizationAttempt.id).where(
            NormalizationAttempt.source_thesis_candidate_id == uuid.UUID(candidate_id)
        ))
    assert b_client.post(
        f"/api/v3/source-interpretations/{source['source_interpretation_id']}/choice",
        json={"selection_kind": "candidate", "source_thesis_candidate_id": candidate_id},
    ).status_code == 404
    assert b_client.post(
        f"/api/v3/source-thesis-candidates/{candidate_id}/normalization-attempt"
    ).status_code == 404
    assert b_client.post(
        f"/api/v3/normalization-attempts/{attempt_id}/accept",
        json={"expected_input_digest": None},
    ).status_code == 404
    b_source, b_thesis_id, b_pool, b_choice = _journey(b_client)
    assert b_client.get("/api/v3/account-state").json()["latest_accepted_thesis_analysis_id"] == b_thesis_id
    assert client_for(a).get("/api/v3/account-state").json()["latest_accepted_thesis_analysis_id"] == thesis_id
    assert b_source["source_interpretation_id"] != source["source_interpretation_id"]
    assert b_pool["market_display_set_id"] != pool["market_display_set_id"]
    assert b_choice["market_choice_id"] != choice["market_choice_id"]
    agent = HumanActor(
        user_id=a.user_id, client_type=ClientType.AGENT_MCP,
        actor_id=a.actor_id, agent_client_id=a.agent_client_id,
    )
    assert client_for(agent).get("/api/v3/account-state").status_code == 404


def test_broken_pool_and_choice_parent_binding_fail_closed(env):
    sessions, _, actor, client_for = env
    identity = actor("resume-a@example.invalid")
    client = client_for(identity)
    _, _, pool, choice = _journey(client)
    pool_id = uuid.UUID(pool["market_display_set_id"])
    with sessions() as session:
        row = session.get(MarketDisplaySet, pool_id)
        row.fit_card_id = uuid.uuid4()
        session.commit()
    assert client.get(f"/api/v3/market-pools/{pool_id}").status_code == 409
    assert client.get(f"/api/v3/market-choices/{choice['market_choice_id']}").status_code == 409
    assert client.get("/api/v3/account-state").json()["latest_market_display_set_id"] is None


@pytest.mark.parametrize("corruption", ["count", "outcome", "pair_class"])
def test_saved_pool_read_rejects_stale_exact_pool_result(env, corruption):
    sessions, _, actor, client_for = env
    client = client_for(actor(f"pool-binding-{corruption}@example.invalid"))
    _, _, pool, _ = _journey(client)
    pool_id = uuid.UUID(pool["market_display_set_id"])
    assert client.get(f"/api/v3/market-pools/{pool_id}").status_code == 200

    with sessions.begin() as session:
        display_set = session.get(MarketDisplaySet, pool_id)
        if corruption == "count":
            display_set.displayed_count -= 1
        elif corruption == "outcome":
            display_set.system_pool_outcome = (
                "candidate_expressions"
                if display_set.system_pool_outcome == "no_clean_expression"
                else "no_clean_expression"
            )
        else:
            items = session.scalars(
                select(MarketDisplayItem)
                .where(MarketDisplayItem.market_display_set_id == pool_id)
                .order_by(MarketDisplayItem.display_rank)
            ).all()
            assert items
            if display_set.system_pool_outcome == "no_clean_expression":
                session.get(MarketAssessment, items[0].market_assessment_id).pair_class = "direct"
            else:
                for item in items:
                    session.get(MarketAssessment, item.market_assessment_id).pair_class = "not_an_expression"

    response = client.get(f"/api/v3/market-pools/{pool_id}")
    assert response.status_code == 409, response.text


def test_durable_source_job_is_discovered_only_by_its_owner(env):
    sessions, services, actor, client_for = env
    a = actor("resume-a@example.invalid")
    b = actor("resume-b@example.invalid")
    a_client = client_for(a)
    submitted = a_client.post(
        "/api/v3/source-interpretation-jobs",
        json={"input_text": CLEAN, "idempotency_key": "resume-source-a"},
    )
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    worker = SourceInterpretationWorker(
        jobs=services.jobs, source_interpretation=services.source_interpretation,
        session_factory=sessions, worker_id="resume-test-worker",
    )
    assert worker.run_once().status == "succeeded"
    source_id = a_client.get(f"/api/v3/source-interpretation-jobs/{job_id}").json()["interpretation"]["source_interpretation_id"]
    assert client_for(a).get("/api/v3/account-state").json()["latest_source_interpretation_job_id"] == job_id
    assert client_for(a).get("/api/v3/account-state").json()["latest_source_interpretation_id"] == source_id
    assert client_for(b).get("/api/v3/account-state").json()["latest_source_interpretation_job_id"] is None
    assert client_for(b).get(f"/api/v3/source-interpretations/{source_id}").status_code == 404
    with sessions() as session:
        request = session.get(SourceInterpretationRequest, uuid.UUID(submitted.json()["source_interpretation_request_id"]))
        request.owner_actor_id = b.actor_id
        session.commit()
    assert client_for(a).get(f"/api/v3/source-interpretations/{source_id}").status_code == 404
    assert client_for(a).get("/api/v3/account-state").json()["latest_source_interpretation_id"] is None


def test_same_user_mcp_source_is_not_human_account_state(env):
    _, services, actor, client_for = env
    identity = actor("resume-a@example.invalid")
    mcp_source = services.source_interpretation.interpret(
        CLEAN, source_url=None,
        client_type=ClientType.AGENT_MCP.value,
        agent_client_id=identity.agent_client_id,
    )
    human = client_for(identity)
    assert human.get("/api/v3/account-state").json()["latest_source_interpretation_id"] is None
    assert human.get(
        f"/api/v3/source-interpretations/{mcp_source.source_interpretation_id}"
    ).status_code == 404


def _durable_accepted_source(env):
    sessions, services, actor, client_for = env
    identity = actor("resume-a@example.invalid")
    client = client_for(identity)
    submitted = client.post(
        "/api/v3/source-interpretation-jobs",
        json={"input_text": CLEAN, "idempotency_key": "accepted-source-job"},
    )
    assert submitted.status_code == 202
    worker = SourceInterpretationWorker(
        jobs=services.jobs, source_interpretation=services.source_interpretation,
        session_factory=sessions, worker_id="accepted-source-worker",
    )
    assert worker.run_once().status == "succeeded"
    job_id = submitted.json()["job_id"]
    source = client.get(f"/api/v3/source-interpretation-jobs/{job_id}").json()["interpretation"]
    source_id = source["source_interpretation_id"]
    candidate_id = source["candidates"][0]["source_thesis_candidate_id"]
    selected = client.post(
        f"/api/v3/source-interpretations/{source_id}/choice",
        json={"selection_kind": "candidate", "source_thesis_candidate_id": candidate_id},
    )
    assert selected.status_code == 200
    attempt = client.post(
        f"/api/v3/source-thesis-candidates/{candidate_id}/normalization-attempt"
    )
    assert attempt.status_code == 200
    accepted = client.post(
        f"/api/v3/normalization-attempts/{attempt.json()['normalization_attempt_id']}/accept",
        json={"expected_input_digest": attempt.json()["input_digest"]},
    )
    assert accepted.status_code == 200
    thesis_id = accepted.json()["thesis_analysis_id"]
    assert client.get(f"/api/v3/theses/{thesis_id}/accepted-state").status_code == 200
    return client, job_id, source_id, thesis_id, identity


@pytest.mark.parametrize("broken", [
    "request_url", "payload_digest", "result_source_id", "result_request_id",
    "result_digest", "job_actor",
])
def test_async_source_parent_corruption_cannot_restore_or_discover(env, broken):
    sessions, _, _, client_for = env
    client, job_id, source_id, thesis_id, _ = _durable_accepted_source(env)
    with sessions() as session:
        job = session.get(Job, uuid.UUID(job_id))
        source = session.get(SourceInterpretation, uuid.UUID(source_id))
        request = session.get(SourceInterpretationRequest, source.source_interpretation_request_id)
        if broken == "request_url":
            request.source_url = "https://example.invalid/foreign"
        elif broken == "payload_digest":
            job.payload = {**job.payload, "input_digest": "0" * 64}
        elif broken == "result_source_id":
            job.result = {**job.result, "source_interpretation_id": str(uuid.uuid4())}
        elif broken == "result_request_id":
            job.result = {**job.result, "source_interpretation_request_id": str(uuid.uuid4())}
        elif broken == "result_digest":
            job.result = {**job.result, "input_digest": "0" * 64}
        else:
            job.owner_actor_id = f"user:{uuid.uuid4()}"
        session.commit()
    assert client.get(f"/api/v3/source-interpretations/{source_id}").status_code == 404
    assert client.get(f"/api/v3/theses/{thesis_id}/accepted-state").status_code == 404
    assert client.get(f"/api/v3/source-interpretation-jobs/{job_id}").status_code == 404
    assert client.get(
        "/api/v3/source-interpretation-jobs/by-idempotency/accepted-source-job"
    ).status_code == 404
    account = client.get("/api/v3/account-state")
    assert account.status_code == 200
    assert account.json()["latest_source_interpretation_id"] is None
    assert account.json()["latest_source_interpretation_job_id"] is None
    assert account.json()["latest_accepted_thesis_analysis_id"] is None


@pytest.mark.parametrize("missing_ref", ["job", "request"])
def test_partial_async_reference_cannot_restore_accepted_source(env, missing_ref):
    sessions, services, _, _ = env
    _, _, source_id, thesis_id, identity = _durable_accepted_source(env)
    with sessions() as session:
        decision = session.scalar(select(NormalizationDecision).where(
            NormalizationDecision.thesis_analysis_id == uuid.UUID(thesis_id)
        ))
        attempt = session.get(NormalizationAttempt, decision.attempt_id)
        source = session.get(SourceInterpretation, uuid.UUID(source_id))
        if missing_ref == "job":
            source.job_id = None
        else:
            source.source_interpretation_request_id = None
        with session.no_autoflush:
            with pytest.raises(NotFound):
                ProductApi(services, identity)._restored_source_for_acceptance(
                    session, uuid.UUID(thesis_id), decision.id, attempt
                )
        session.rollback()


def _durable_selected_market(env):
    client, job_id, source_id, thesis_id, _ = _durable_accepted_source(env)
    pool = client.post(f"/api/v3/theses/{thesis_id}/market-pool")
    assert pool.status_code == 200, pool.text
    pool_data = pool.json()
    choice = client.post(
        f"/api/v3/market-pools/{pool_data['market_display_set_id']}/choice",
        json={
            "selection_kind": "market",
            "market_assessment_id": pool_data["candidate_markets"][0][
                "market_assessment_id"
            ],
        },
    )
    assert choice.status_code == 200, choice.text
    return client, job_id, source_id, choice.json()["market_choice_id"]


def test_revisit_activation_accepts_valid_durable_source_job_binding(env):
    sessions, _, _, _ = env
    client, _, _, choice_id = _durable_selected_market(env)
    activation = client.post(
        f"/api/v3/market-choices/{choice_id}/revisit-activation"
    )
    assert activation.status_code == 200, activation.text
    assert activation.json()["binding"]["market_choice_id"] == choice_id
    assert _count(sessions, ThesisRevision) == 1


@pytest.mark.parametrize(
    "broken",
    [
        "job_type",
        "job_actor",
        "job_user",
        "status",
        "payload_request_id",
        "payload_digest",
        "result_source_id",
        "result_request_id",
        "result_digest",
        "missing_job",
        "missing_request",
    ],
)
def test_revisit_activation_rejects_broken_async_source_job_binding(env, broken):
    sessions, _, _, _ = env
    client, job_id, source_id, choice_id = _durable_selected_market(env)

    with sessions.begin() as session:
        job = session.get(Job, uuid.UUID(job_id))
        source = session.get(SourceInterpretation, uuid.UUID(source_id))
        request = session.get(
            SourceInterpretationRequest,
            source.source_interpretation_request_id,
        )
        if broken == "job_type":
            job.job_type = "other_job_type"
        elif broken == "job_actor":
            job.owner_actor_id = f"user:{uuid.uuid4()}"
        elif broken == "job_user":
            job.owner_user_id = uuid.uuid4()
        elif broken == "status":
            job.status = "needs_operator"
        elif broken == "payload_request_id":
            job.payload = {
                **job.payload,
                "source_interpretation_request_id": str(uuid.uuid4()),
            }
        elif broken == "payload_digest":
            job.payload = {**job.payload, "input_digest": "0" * 64}
        elif broken == "result_source_id":
            job.result = {
                **job.result,
                "source_interpretation_id": str(uuid.uuid4()),
            }
        elif broken == "result_request_id":
            job.result = {
                **job.result,
                "source_interpretation_request_id": str(uuid.uuid4()),
            }
        elif broken == "result_digest":
            job.result = {**job.result, "input_digest": "0" * 64}
        elif broken == "missing_job":
            session.delete(job)
        else:
            assert broken == "missing_request"
            session.delete(request)

    assert client.get(f"/api/v3/market-choices/{choice_id}").status_code == 404
    assert _count(sessions, ThesisRevision) == 0
    activation = client.post(
        f"/api/v3/market-choices/{choice_id}/revisit-activation"
    )
    assert activation.status_code == 409
    assert activation.json()["error"] == "revisit_conflict"
    assert _count(sessions, ThesisRevision) == 0


def _restricted_durable_source(env, source_text, key):
    sessions, services, actor, client_for = env
    identity = actor(f"{key}@example.invalid")
    client = client_for(identity)
    submitted = client.post(
        "/api/v3/source-interpretation-jobs",
        json={"input_text": source_text, "idempotency_key": key},
    )
    assert submitted.status_code == 202, submitted.text
    worker = SourceInterpretationWorker(
        jobs=services.jobs,
        source_interpretation=services.source_interpretation,
        session_factory=sessions,
        worker_id=f"{key}-worker",
    )
    assert worker.run_once().status == "succeeded"
    job_id = submitted.json()["job_id"]
    job = client.get(f"/api/v3/source-interpretation-jobs/{job_id}")
    assert job.status_code == 200, job.text
    source = job.json()["interpretation"]
    source_id = source["source_interpretation_id"]
    candidate_id = source["candidates"][0]["source_thesis_candidate_id"]
    selected = client.post(
        f"/api/v3/source-interpretations/{source_id}/choice",
        json={
            "selection_kind": "candidate",
            "source_thesis_candidate_id": candidate_id,
        },
    )
    assert selected.status_code == 200, selected.text
    attempt = client.post(
        f"/api/v3/source-thesis-candidates/{candidate_id}/normalization-attempt"
    )
    assert attempt.status_code == 200, attempt.text
    accepted = client.post(
        f"/api/v3/normalization-attempts/"
        f"{attempt.json()['normalization_attempt_id']}/accept",
        json={"expected_input_digest": attempt.json()["input_digest"]},
    )
    assert accepted.status_code == 200, accepted.text
    return (
        sessions,
        client_for,
        identity,
        job_id,
        source_id,
        accepted.json()["thesis_analysis_id"],
    )


@pytest.mark.parametrize("source_text", [BUY, SELL])
def test_resumed_exact_restricted_source_quote_is_allowed(env, source_text):
    _, client_for, identity, job_id, source_id, thesis_id = (
        _restricted_durable_source(
            env,
            source_text,
            f"restricted-{sha256(source_text.encode()).hexdigest()[:8]}",
        )
    )
    fresh = client_for(identity)
    job = fresh.get(f"/api/v3/source-interpretation-jobs/{job_id}")
    source = fresh.get(f"/api/v3/source-interpretations/{source_id}")
    accepted = fresh.get(f"/api/v3/theses/{thesis_id}/accepted-state")
    assert [job.status_code, source.status_code, accepted.status_code] == [
        200,
        200,
        200,
    ]

    nested = job.json()["interpretation"]["candidates"][0]
    direct = source.json()["candidates"][0]
    restored = accepted.json()["restored_source"]
    expected_digest = sha256(source_text.encode()).hexdigest()
    for candidate in (nested, direct):
        assert candidate["selected_source_quote"] == source_text
        assert candidate["source_quote_digest"] == expected_digest
        assert not vocabulary_violations(candidate["claim_summary"])
    assert restored["selected_source_quote"] == source_text
    assert restored["original_source_text"] == source_text
    assert restored["accepted_normalization_input"] == source_text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("selected_source_quote", "Acme will buy a rival in 2026."),
        ("source_quote_digest", "0" * 64),
    ],
)
def test_tampered_candidate_quote_or_digest_cannot_resume(env, field, value):
    sessions, client_for, identity, job_id, source_id, thesis_id = (
        _restricted_durable_source(env, BUY, f"tamper-{field}")
    )
    with sessions.begin() as session:
        candidate = session.scalar(
            select(SourceThesisCandidate).where(
                SourceThesisCandidate.source_interpretation_id
                == uuid.UUID(source_id)
            )
        )
        setattr(candidate, field, value)

    fresh = client_for(identity)
    for path in (
        f"/api/v3/source-interpretation-jobs/{job_id}",
        f"/api/v3/source-interpretations/{source_id}",
        f"/api/v3/theses/{thesis_id}/accepted-state",
    ):
        response = fresh.get(path)
        assert response.status_code == 404
        assert response.json() == {"error": "not_found"}
    account = fresh.get("/api/v3/account-state")
    assert account.status_code == 200
    for key in (
        "latest_source_interpretation_id",
        "latest_source_interpretation_job_id",
        "latest_accepted_thesis_analysis_id",
    ):
        assert account.json()[key] is None


def test_resumed_market_pool_rechecks_generated_text_but_allows_captured_source(
    env,
):
    sessions, services, actor, client_for = env
    identity = actor("resume-market-text@example.invalid")
    _, _, pool, _ = _journey(client_for(identity))
    pool_id = uuid.UUID(pool["market_display_set_id"])
    captured_title = "Will Acme sell its unit?"
    captured_rules = "The captured source says buy or sell."

    with sessions.begin() as session:
        item = session.scalar(
            select(MarketDisplayItem)
            .where(MarketDisplayItem.market_display_set_id == pool_id)
            .order_by(MarketDisplayItem.display_rank)
        )
        assessment = session.get(MarketAssessment, item.market_assessment_id)
        capture = session.get(MarketRulesCapture, assessment.rules_capture_id)
        safe_captures = assessment.what_it_captures
        assessment.what_it_captures = "you should buy this market now"
        capture.contract_terms_text = captured_title
        capture.contract_terms_hash = sha256(captured_title.encode()).hexdigest()
        capture.resolution_rules_text = captured_rules
        capture.resolution_rules_hash = sha256(captured_rules.encode()).hexdigest()

    with pytest.raises(A7Violation) as error:
        ProductApi(services, identity).get_market_pool(pool_id)
    assert error.value.offenders == [
        ("candidate_markets[].what_it_captures", ["buy", "you should buy"])
    ]

    with sessions.begin() as session:
        item = session.scalar(
            select(MarketDisplayItem)
            .where(MarketDisplayItem.market_display_set_id == pool_id)
            .order_by(MarketDisplayItem.display_rank)
        )
        assessment = session.get(MarketAssessment, item.market_assessment_id)
        assessment.what_it_captures = safe_captures

    response = client_for(identity).get(f"/api/v3/market-pools/{pool_id}")
    assert response.status_code == 200, response.text
    card = response.json()["candidate_markets"][0]
    assert card["market_title"] == captured_title
    assert card["resolution_conditions"] == captured_rules
