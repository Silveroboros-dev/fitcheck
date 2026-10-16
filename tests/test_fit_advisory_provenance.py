"""FC-R03: accepted veto causes remain attributable through FitService."""

from datetime import date
from itertools import combinations

import pytest
from sqlalchemy import select

from el.domain.enums import FitClass
from el.domain.structures import ClaimHorizon
from el.domain.tables import (
    FitCard,
    MarketRecommendation,
    MarketRulesCapture,
    RejectedMarketRow,
    ThesisAnalysis,
)
from el.fitgate.gate import merge_advisory
from el.fitgate.policy import class_rank, evaluate_market
from el.fitgate.service import FitService
from el.marketstructure.service import MarketStructureService
from el.models.fit_adapter import FixtureFitProposer
from el.models.market_adapter import FixtureMarketStructureProposer
from el.retrieval.provider import FixtureMarketProvider
from el.retrieval.service import RetrievalService
from tests.test_fit_service import (
    GOLDENS,
    SNAPSHOT_PATH,
    _ADVISORY_CONDITIONS,
    _all_pass_advisory,
    _lmsys_claim,
    _session_factory,
)

MARKET_ID = "mkt_gemini_lmsys_1"


def _veto_advisory(fails, **updates):
    advisory = _all_pass_advisory(MARKET_ID)
    return advisory.model_copy(
        update={
            "condition_verdicts": [
                verdict.model_copy(
                    update={"status": "fail" if verdict.condition in fails else "pass"}
                )
                for verdict in advisory.condition_verdicts
            ],
            **updates,
        }
    )


def _classify_advisory(advisory, *, claim=None):
    claim = claim or _lmsys_claim()
    sessions = _session_factory()
    with sessions() as session:
        analysis = ThesisAnalysis(
            input_text=claim.claim_summary,
            extracted_structure=claim.model_dump(mode="json"),
            normalized_claim_summary=claim.claim_summary,
            client_type="human_ui",
        )
        session.add(analysis)
        session.commit()
        analysis_id = analysis.id
    retrieval = RetrievalService(
        FixtureMarketProvider.from_path(SNAPSHOT_PATH), sessions
    ).retrieve_candidates(analysis_id)
    outcome = FitService(
        MarketStructureService(
            FixtureMarketStructureProposer(GOLDENS), sessions, top_n=1
        ),
        sessions,
        advisory=FixtureFitProposer({MARKET_ID: advisory}),
    ).classify_fit(analysis_id, retrieval.candidate_set_id)
    return outcome, sessions


@pytest.mark.parametrize(
    ("fails", "published"),
    [
        ({"same_metric"}, "weak_proxy"),
        ({"same_metric", "same_event_stage"}, "no_clean_expression"),
    ],
)
def test_service_accepted_veto_preserves_causes_card_rejection_and_provenance(
    fails, published
):
    advisory = _veto_advisory(
        fails,
        what_it_misses="Unsupported provider explanation must not narrate the veto.",
    )
    outcome, sessions = _classify_advisory(advisory)

    assert outcome.fit_class.value == published
    assert outcome.recommended_market_id is None
    assert outcome.authority == "deterministic_with_advisory_veto"
    assert outcome.advisory_calls == 1
    assert outcome.advisory_fallbacks == 0
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        recommendation = session.get(MarketRecommendation, outcome.market_recommendation_id)
        rejected = session.scalars(select(RejectedMarketRow)).one()
        capture = session.scalars(
            select(MarketRulesCapture).where(MarketRulesCapture.market_id == MARKET_ID)
        ).one()
        per_market = card.provenance["per_market"][MARKET_ID]
        metadata = per_market["advisory"]
        assert per_market["ceiling"] == "direct"
        assert per_market["published"] == published
        assert per_market["hard_fails"] == 0
        assert per_market["fired"] == []
        for condition in fails:
            assert condition in card.what_it_misses
            assert condition in rejected.reason
        assert "Stacked condition failures" not in card.what_it_misses
        assert "outranked" not in rejected.reason
        assert advisory.what_it_misses not in card.what_it_misses
        assert metadata["vetoed"] is True
        assert {failure["condition"] for failure in metadata["failures"]} == fails
        for failure in metadata["failures"]:
            assert failure["status"] == "fail"
            assert failure["cap"] == "weak_proxy"
            assert failure["hard"] is True
            assert failure["claim_evidence"] == "LMSYS Chatbot Arena"
            assert failure["market_evidence"] == "This market resolves to YES"
        assert metadata["evidence_validation"] == {
            "quote_span": "passed",
            "claim_corpus": "input_text_and_extracted_structure",
            "market_snapshot_id": capture.snapshot_id,
            "contract_terms_hash": capture.contract_terms_hash,
            "resolution_rules_hash": capture.resolution_rules_hash,
        }
        assert recommendation.provenance == card.provenance


def test_service_soft_veto_recommendation_narrates_verified_cause():
    outcome, sessions = _classify_advisory(_veto_advisory({"horizon_covers_claim"}))
    assert outcome.fit_class.value == "indirect"
    assert outcome.recommended_market_id == MARKET_ID
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        assert "horizon_covers_claim" in card.what_it_misses
        assert "cap indirect" in card.what_it_misses
        assert card.what_it_captures.startswith("Deterministic conditions met")
        assert card.provenance["per_market"][MARKET_ID]["advisory"]["failures"][0]["hard"] is False


def test_service_veto_preserves_deterministic_and_advisory_causes_together():
    claim = _lmsys_claim(
        horizon=ClaimHorizon(window_end=date(2026, 12, 20), precision="day")
    )
    outcome, sessions = _classify_advisory(_veto_advisory({"same_metric"}), claim=claim)
    assert outcome.fit_class.value == "weak_proxy"
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        rejected = session.scalars(select(RejectedMarketRow)).one()
        per_market = card.provenance["per_market"][MARKET_ID]
        assert per_market["ceiling"] == "indirect"
        deterministic_details = [
            check["detail"] for check in per_market["checks"] if check["cap"] is not None
        ]
        assert deterministic_details
        for detail in deterministic_details:
            assert detail in card.what_it_misses
            assert detail in rejected.reason
        assert "same_metric" in card.what_it_misses
        assert "same_metric" in rejected.reason


def test_service_rejected_veto_never_attributes_unverified_causes():
    advisory = _veto_advisory({"same_metric"})
    advisory.condition_verdicts[0].market_evidence = "Invented evidence absent from captured rules"
    outcome, sessions = _classify_advisory(advisory)
    assert outcome.fit_class.value == "direct"
    assert outcome.authority == "deterministic_fallback"
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        metadata = card.provenance["per_market"][MARKET_ID]["advisory"]
        assert metadata["status"] == "fallback"
        assert "failures" not in metadata
        assert "vetoed" not in metadata
        assert "Advisory veto" not in card.what_it_misses
        assert "Invented evidence" not in card.what_it_misses


def test_all_advisory_failure_combinations_are_demotion_only():
    base = evaluate_market(_lmsys_claim(), GOLDENS[MARKET_ID])
    for ceiling in FitClass:
        deterministic = base.model_copy(
            update={"deterministic_ceiling": ceiling, "published": ceiling}
        )
        for count in range(len(_ADVISORY_CONDITIONS) + 1):
            for fails in combinations(_ADVISORY_CONDITIONS, count):
                for suggested in FitClass:
                    merge = merge_advisory(
                        deterministic,
                        _veto_advisory(fails, suggested_class=suggested),
                        market_id=MARKET_ID,
                    )
                    assert merge.accepted
                    assert class_rank(merge.verdict.published) <= class_rank(ceiling)
                    assert merge.verdict.deterministic_ceiling == ceiling
