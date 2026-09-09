"""Fixture-backed ownership and decision-origin coverage for public MCP v3.1."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from el.domain.tables import (
    Base,
    MarketChoice,
    MarketDisplaySet,
    NormalizationAttempt,
    NormalizationDecision,
    SourceCandidateChoice,
)
from el.mcp.contracts import NotFound
from el.mcp.wiring import build_fixture_v3_tools, seed_principal
from el.product.wiring import (
    HARBOR_REVISED_INPUT,
    MULTI_THESIS_FIXTURE,
    build_services,
)
from el.sourceinterpretation.jobs import SourceInterpretationWorker


def _count(sessions, table) -> int:
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(table)) or 0


def _fixture_v3():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    services = build_services(mode="fixture", session_factory=sessions)
    return sessions, services, build_fixture_v3_tools(sessions)


def test_two_mcp_principals_cannot_cross_v31_source_or_market_boundaries():
    sessions, services, tools = _fixture_v3()
    owner = seed_principal(sessions, f"owner-{uuid.uuid4()}")
    foreign = seed_principal(sessions, f"foreign-{uuid.uuid4()}")

    queued = tools.submit_source_interpretation(
        owner,
        input_text=MULTI_THESIS_FIXTURE,
        source_url=None,
        idempotency_key="owner-source",
    )
    worker = SourceInterpretationWorker(
        jobs=services.jobs,
        source_interpretation=services.source_interpretation,
        session_factory=sessions,
        worker_id="fixture-v3-owner-check",
    )
    assert worker.run_once().status == "succeeded"
    status = tools.get_source_interpretation_job(owner, job_id=queued.job_id)
    assert status.interpretation is not None
    source = status.interpretation
    candidate = source.candidates[0]

    with pytest.raises(NotFound):
        tools.get_source_interpretation_job(foreign, job_id=queued.job_id)
    with pytest.raises(NotFound):
        tools.get_source_interpretation_job_by_idempotency(
            foreign, idempotency_key="owner-source"
        )

    protected = (
        SourceCandidateChoice,
        NormalizationAttempt,
        NormalizationDecision,
        MarketDisplaySet,
        MarketChoice,
    )
    before = {table: _count(sessions, table) for table in protected}
    with pytest.raises(NotFound):
        tools.choose_source_candidate(
            foreign,
            source_interpretation_id=source.source_interpretation_id,
            selection_kind="candidate",
            source_thesis_candidate_id=candidate.source_thesis_candidate_id,
        )
    with pytest.raises(NotFound):
        tools.propose_selected_normalization(
            foreign, source_thesis_candidate_id=candidate.source_thesis_candidate_id
        )
    assert {table: _count(sessions, table) for table in protected} == before

    choice = tools.choose_source_candidate(
        owner,
        source_interpretation_id=source.source_interpretation_id,
        selection_kind="candidate",
        source_thesis_candidate_id=candidate.source_thesis_candidate_id,
    )
    assert choice.decision_origin == "agent_relay"
    assert choice.human_attestation is False
    initial = tools.propose_selected_normalization(
        owner, source_thesis_candidate_id=candidate.source_thesis_candidate_id
    )
    revised = tools.revise_normalization(
        owner,
        normalization_attempt_id=initial.normalization_attempt_id,
        expected_input_digest=initial.input_digest,
        input_text=HARBOR_REVISED_INPUT,
    )

    before = {table: _count(sessions, table) for table in protected}
    with pytest.raises(NotFound):
        tools.revise_normalization(
            foreign,
            normalization_attempt_id=revised.normalization_attempt_id,
            expected_input_digest=revised.input_digest,
            input_text=HARBOR_REVISED_INPUT,
        )
    with pytest.raises(NotFound):
        tools.accept_normalization(
            foreign,
            normalization_attempt_id=revised.normalization_attempt_id,
            expected_input_digest=revised.input_digest,
        )
    with pytest.raises(NotFound):
        tools.reject_normalization(
            foreign,
            normalization_attempt_id=revised.normalization_attempt_id,
            expected_input_digest=revised.input_digest,
        )
    assert {table: _count(sessions, table) for table in protected} == before

    accepted = tools.accept_normalization(
        owner,
        normalization_attempt_id=revised.normalization_attempt_id,
        expected_input_digest=revised.input_digest,
    )
    assert accepted.decision_origin == "agent_relay"
    assert accepted.human_attestation is False
    assert accepted.thesis_analysis_id is not None

    with pytest.raises(NotFound):
        tools.assess_market_pool(foreign, thesis_analysis_id=accepted.thesis_analysis_id)
    pool = tools.assess_market_pool(owner, thesis_analysis_id=accepted.thesis_analysis_id)
    assert pool.displayed_count == 3
    before = {table: _count(sessions, table) for table in protected}
    with pytest.raises(NotFound):
        tools.choose_market(
            foreign,
            market_display_set_id=pool.market_display_set_id,
            selection_kind="none",
            market_assessment_id=None,
        )
    assert {table: _count(sessions, table) for table in protected} == before

    market_choice = tools.choose_market(
        owner,
        market_display_set_id=pool.market_display_set_id,
        selection_kind="none",
        market_assessment_id=None,
    )
    assert market_choice.decision_origin == "agent_relay"
    assert market_choice.human_attestation is False
