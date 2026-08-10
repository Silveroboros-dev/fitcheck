"""Step 5b — DraftContractService: generate, gate, persist, back-link."""

import json
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from el.domain.structures import (
    ClaimHorizon,
    Entity,
    ExtractedStructure,
    MarketStructure,
    Mechanism,
    Metric,
)
from el.domain.tables import Base, DraftContract, FitCard, ThesisAnalysis
from el.draftcontract.service import DraftContractService
from el.fitgate.service import FitService
from el.marketstructure.service import MarketStructureService
from el.models.draft_adapter import FixtureDraftProposer, ProposedDraft
from el.models.market_adapter import FixtureMarketStructureProposer
from el.retrieval.provider import FixtureMarketProvider
from el.retrieval.service import RetrievalService

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_PATH = FIXTURES / "retrieval" / "frozen_snapshot_phase0.json"
GOLDEN_PATH = FIXTURES / "markets" / "golden_market_structures.json"

GOLDENS = {
    s["market_id"]: MarketStructure.model_validate(s)
    for s in json.loads(GOLDEN_PATH.read_text())["structures"]
}

NO_CLEAN_SUMMARY = "Zzcorp wins the underwater basket weaving cup in 2026."


def _session_factory():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _no_clean_claim() -> ExtractedStructure:
    return ExtractedStructure(
        claim_summary=NO_CLEAN_SUMMARY,
        entities=[Entity(name="Zzcorp", role="subject")],
        event_stage="measured",
        metric=Metric(
            what="underwater basket weaving championship outcome",
            measured_by="nobody",
            objective=True,
        ),
        horizon=ClaimHorizon(window_end=date(2026, 12, 31), precision="day"),
        mechanism=Mechanism(),
        stance="yes",
        resolution_source_class="press",
        contractible_version="Will Zzcorp win the cup by Dec 31, 2026?",
    )


def _direct_claim() -> ExtractedStructure:
    return ExtractedStructure(
        claim_summary="Gemini ranks #1 on LMSYS Chatbot Arena by end of 2026.",
        entities=[
            Entity(name="Google Gemini", role="subject"),
            Entity(name="LMSYS Chatbot Arena", role="venue"),
        ],
        event_stage="measured",
        metric=Metric(
            what="LMSYS Chatbot Arena #1 rank",
            measured_by="LMSYS leaderboard",
            objective=True,
        ),
        horizon=ClaimHorizon(window_end=date(2026, 12, 31), precision="day"),
        mechanism=Mechanism(),
        stance="yes",
        resolution_source_class="leaderboard",
        contractible_version="Will a Google Gemini model rank #1 on LMSYS?",
    )


def _good_draft() -> ProposedDraft:
    return ProposedDraft(
        proposed_title=(
            "Will Zzcorp win the underwater basket weaving world championship "
            "on or before December 31, 2026?"
        ),
        proposed_resolution_logic=(
            "Resolves YES if Zzcorp is declared champion at the 2026 world "
            "championship by the organizing federation."
        ),
        resolution_source="World Underwater Basket Weaving Federation results",
        resolution_source_class="official",
        resolution_deadline=date(2026, 12, 31),
        subject_entity="Zzcorp",
        event_stage="measured",
        category="sports",
        time_horizon="by end of 2026",
    )


def _classify(structure: ExtractedStructure):
    sessions = _session_factory()
    with sessions() as session:
        analysis = ThesisAnalysis(
            input_text=structure.claim_summary,
            extracted_structure=structure.model_dump(mode="json"),
            normalized_claim_summary=structure.claim_summary,
            client_type="human_ui",
        )
        session.add(analysis)
        session.commit()
        thesis_analysis_id = analysis.id

    provider = FixtureMarketProvider.from_path(SNAPSHOT_PATH)
    retrieval = RetrievalService(provider, sessions).retrieve_candidates(
        thesis_analysis_id
    )
    fit = FitService(
        MarketStructureService(
            FixtureMarketStructureProposer(GOLDENS), sessions
        ),
        sessions,
    )
    outcome = fit.classify_fit(thesis_analysis_id, retrieval.candidate_set_id)
    return outcome, sessions


def test_generates_and_backlinks_draft_on_no_clean_card():
    fit_outcome, sessions = _classify(_no_clean_claim())
    assert fit_outcome.fit_class.value == "no_clean_expression"  # precondition

    service = DraftContractService(
        FixtureDraftProposer({NO_CLEAN_SUMMARY: _good_draft()}), sessions
    )
    result = service.generate(fit_outcome.fit_card_id)

    assert result.generated
    assert result.gate_verdict == "pass"
    assert result.draft_contract_id is not None
    assert result.proposer_calls == 1
    assert result.proposer_failures == 0

    with sessions() as session:
        draft = session.get(DraftContract, result.draft_contract_id)
        assert draft.proposed_title.startswith("Will Zzcorp")
        assert draft.thesis_analysis_id == fit_outcome.thesis_analysis_id
        assert draft.resolution_source.startswith("World Underwater")
        card = session.get(FitCard, fit_outcome.fit_card_id)
        assert card.draft_contract_id == draft.id  # back-link set


def test_provenance_complete_on_persisted_draft():
    fit_outcome, sessions = _classify(_no_clean_claim())
    service = DraftContractService(
        FixtureDraftProposer({NO_CLEAN_SUMMARY: _good_draft()}), sessions
    )
    result = service.generate(fit_outcome.fit_card_id)

    with sessions() as session:
        draft = session.get(DraftContract, result.draft_contract_id)
        for key in (
            "gate_policy_version",
            "proposer_policy_version",
            "extraction_schema_version",
            "model_adapter",
            "model_run_id",
            "trace_id",
            "eval_pack_version",
            "generated_at",
            "token_rules_version",
            "echo",
            "checks",
            "rejection_reasons_input",
        ):
            assert key in draft.provenance, f"partial provenance: missing {key}"
        assert draft.provenance["gate_policy_version"] == "draftgen-v1"
        assert len(draft.provenance["checks"]) == 7
        assert draft.provenance["echo"]["subject_entity"] == "Zzcorp"
        assert draft.provenance["echo"]["resolution_deadline"] == "2026-12-31"
        # The fit gate's rejection reasons reached the proposer.
        assert draft.provenance["rejection_reasons_input"]


def test_echo_fields_persisted_and_exposed_first_class():
    from el.domain.contracts import DraftContractOut

    fit_outcome, sessions = _classify(_no_clean_claim())
    service = DraftContractService(
        FixtureDraftProposer({NO_CLEAN_SUMMARY: _good_draft()}), sessions
    )
    result = service.generate(fit_outcome.fit_card_id)

    with sessions() as session:
        draft = session.get(DraftContract, result.draft_contract_id)
        # First-class columns, not buried in provenance.
        assert draft.resolution_deadline == date(2026, 12, 31)
        assert draft.resolution_source_class == "official"
        assert draft.subject_entity == "Zzcorp"
        # And exposed through the public contract.
        dto = DraftContractOut.model_validate(draft)
        assert dto.resolution_deadline == date(2026, 12, 31)
        assert dto.resolution_source_class == "official"
        assert dto.subject_entity == "Zzcorp"


def test_rejected_draft_does_not_persist_and_card_stays_valid():
    fit_outcome, sessions = _classify(_no_clean_claim())
    drifted = _good_draft().model_copy(
        update={"resolution_deadline": date(2027, 1, 31)}
    )
    service = DraftContractService(
        FixtureDraftProposer({NO_CLEAN_SUMMARY: drifted}), sessions
    )
    result = service.generate(fit_outcome.fit_card_id)

    assert not result.generated
    assert result.gate_verdict == "rejected_invalid"
    assert result.draft_contract_id is None
    assert any("drifted" in reason for reason in result.reasons)

    with sessions() as session:
        assert session.scalars(select(DraftContract)).all() == []
        card = session.get(FitCard, fit_outcome.fit_card_id)
        assert card.draft_contract_id is None  # valid card, just no draft


def test_proposer_failure_degrades_gracefully():
    fit_outcome, sessions = _classify(_no_clean_claim())

    class ExplodingProposer:
        def propose_draft(self, **kwargs):
            raise RuntimeError("model down")

    service = DraftContractService(ExplodingProposer(), sessions)
    result = service.generate(fit_outcome.fit_card_id)

    assert not result.generated
    assert result.gate_verdict == "proposer_failed"
    assert result.proposer_calls == 2  # retry budget 1 -> two attempts
    assert result.proposer_failures == 2
    with sessions() as session:
        card = session.get(FitCard, fit_outcome.fit_card_id)
        assert card.draft_contract_id is None


def test_direct_card_is_not_applicable():
    fit_outcome, sessions = _classify(_direct_claim())
    assert fit_outcome.fit_class.value == "direct"  # precondition

    proposer = FixtureDraftProposer({})  # never called
    service = DraftContractService(proposer, sessions)
    result = service.generate(fit_outcome.fit_card_id)

    assert not result.generated
    assert result.gate_verdict == "not_applicable"
    assert result.draft_contract_id is None
    assert proposer.calls == 0


def test_second_generate_is_idempotent():
    fit_outcome, sessions = _classify(_no_clean_claim())
    proposer = FixtureDraftProposer({NO_CLEAN_SUMMARY: _good_draft()})
    service = DraftContractService(proposer, sessions)

    first = service.generate(fit_outcome.fit_card_id)
    second = service.generate(fit_outcome.fit_card_id)

    assert first.generated and not second.generated
    assert second.gate_verdict == "already_present"
    assert second.draft_contract_id == first.draft_contract_id
    assert proposer.calls == 1  # not called again
    with sessions() as session:
        assert len(session.scalars(select(DraftContract)).all()) == 1
