"""FC-R01: retained source wording cannot break generated fit explanations."""

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from el.domain.contracts import ThesisAnalysisIn
from el.domain.enums import ClientType, FitClass
from el.domain.structures import Entity, Mechanism, Metric
from el.domain.tables import FitCard, MarketRecommendation, NormalizationDecision, ThesisAnalysis
from el.domain.vocabulary import vocabulary_violations
from el.extraction.gate_v2 import GATE_POLICY_VERSION
from el.extraction.service import ExtractionService, NormalizationAttemptConflict, NormalizationAttemptNotFound
from el.fitgate.m1_subject_only import aggregate_thesis, evaluate_market
from el.fitgate.service import FitService
from el.marketstructure.gate import MarketGateVerdict, market_structure_gate
from el.marketstructure.service import MarketStructureService
from el.models.adapter_v2 import CandidateProposalV2, ExtractionProposalV2
from el.models.market_adapter import FixtureMarketStructureProposer, MARKET_EXTRACTION_POLICY_VERSION
from el.retrieval.provider import FixtureMarketProvider
from el.retrieval.service import RetrievalService
from test_fit_service import GOLDENS, SNAPSHOT_PATH, _classify, _lmsys_claim, _session_factory
from test_fitgate_checks import make_claim, make_market


def test_accepted_source_mechanism_classifies_without_partial_records_or_advice():
    source = "Gemini ranks #1 on LMSYS Chatbot Arena by end of 2026 because people buy widgets."
    structure = _lmsys_claim(mechanism=Mechanism(asserted_causal_chain="people buy widgets"))
    proposal = ExtractionProposalV2(
        mode="candidate",
        candidate=CandidateProposalV2(selected_source_quote=source, structure=structure),
        clarification=None,
    )

    class SyntheticProposer:
        def propose_extraction(self, input_text):
            return SimpleNamespace(proposal=proposal, model_adapter="synthetic", model_run_id="fc-r01-offline")

    sessions = _session_factory()
    extraction = ExtractionService(SyntheticProposer(), sessions, gate_policy_version=GATE_POLICY_VERSION)
    attempted = extraction.propose(ThesisAnalysisIn(
        input_text=source, client_type=ClientType.AGENT_MCP, agent_client_id="source-owner",
    ))
    assert attempted.outcome == "candidate"
    binding = dict(client_type="agent_mcp", agent_client_id="source-owner", actor_id="source-owner", expected_input_digest=attempted.input_digest)
    for changed in ({"agent_client_id": "foreign-actor"}, {"expected_input_digest": "0" * 64}):
        with pytest.raises((NormalizationAttemptConflict, NormalizationAttemptNotFound)):
            extraction.accept(attempted.attempt_id, **(binding | changed))
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ThesisAnalysis)) == 0
        assert session.scalar(select(func.count()).select_from(NormalizationDecision)) == 0
    accepted = extraction.accept(attempted.attempt_id, **binding)
    replay = extraction.accept(attempted.attempt_id, **binding)
    assert replay.decision_id == accepted.decision_id
    assert replay.thesis_analysis_id == accepted.thesis_analysis_id
    retrieved = RetrievalService(FixtureMarketProvider.from_path(SNAPSHOT_PATH), sessions).retrieve_candidates(accepted.thesis_analysis_id)
    service = FitService(MarketStructureService(FixtureMarketStructureProposer(GOLDENS), sessions), sessions)
    # Repeat an already accepted thesis; acceptance identity and retained input
    # stay fixed, while each successful fit call writes a complete transaction.
    for expected_count in (1, 2):
        outcome = service.classify_fit(accepted.thesis_analysis_id, retrieved.candidate_set_id)
        assert outcome.fit_class is FitClass.INDIRECT
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(FitCard)) == expected_count
            assert session.scalar(select(func.count()).select_from(MarketRecommendation)) == expected_count
            card = session.get(FitCard, outcome.fit_card_id)
            assert not vocabulary_violations(card.what_it_captures + " " + card.what_it_misses)
            details = card.provenance["per_market"]["mkt_gemini_lmsys_1"]["checks"]
            mechanism = next(c for c in details if c["check_id"] == "X1")
            assert mechanism["detail_status"] == "restricted_source_wording_withheld"
            assert not vocabulary_violations(mechanism["detail"])
            analysis = session.get(ThesisAnalysis, accepted.thesis_analysis_id)
            assert analysis.extracted_structure["mechanism"]["asserted_causal_chain"] == "people buy widgets"


@pytest.mark.parametrize("overrides", [
    {"mechanism": Mechanism(asserted_causal_chain="people sell widgets")},
    {"metric": Metric(what="buy volume", measured_by="exchange", objective=True)},
    {"entities": [Entity(name="people buy widgets", role="subject")]},
])
def test_all_free_text_claim_check_details_are_safe_on_card_and_rejection(overrides):
    verdict = evaluate_market(make_claim(**overrides), make_market())
    service = FitService(None, None)
    thesis = aggregate_thesis([(verdict, 0)])
    captures, misses = service._card_text(thesis, verdict)
    reason = service._rejection_reason(verdict)
    assert not vocabulary_violations(captures + " " + misses + " " + reason)
    assert "source wording withheld" in misses or "source wording withheld" in reason


def test_benign_mechanism_detail_is_preserved():
    outcome, sessions = _classify(_lmsys_claim(mechanism=Mechanism(asserted_causal_chain="improved reasoning raises rank")))
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        assert "improved reasoning raises rank" in card.what_it_misses
        assert "source wording withheld" not in card.what_it_misses


def test_source_market_identifier_is_not_interpolated_as_generated_advice():
    verdict = evaluate_market(make_claim(), make_market(market_id="buy widgets"))
    thesis = aggregate_thesis([(verdict, 0)])
    captures, misses = FitService(None, None)._card_text(thesis, verdict)
    assert not vocabulary_violations(captures + " " + misses)
    assert verdict.market_id == thesis.recommended_market_id == "buy widgets"
    assert "identifier retained in provenance" in captures


def test_market_restricted_metric_still_rejects_before_fit():
    market = make_market(metric=Metric(what="buy volume", measured_by="exchange", objective=True), extraction_policy_version=MARKET_EXTRACTION_POLICY_VERSION)
    result = market_structure_gate(market_id=market.market_id, snapshot_id=market.snapshot_id, structure=market)
    assert result.verdict is MarketGateVerdict.REJECTED_INVALID
    assert any("restricted vocabulary" in reason for reason in result.reasons)
