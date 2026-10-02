"""Step 5 — FitService: classify, persist, provenance (blueprint §14)."""

import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
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
from el.domain.contracts import FitCardOut, RejectedMarket
from el.domain.tables import (
    Base,
    CandidateSet,
    FitCard,
    MarketRecommendation,
    RejectedMarketRow,
    ThesisAnalysis,
)
from el.fitgate.m1_subject_only import (
    M1_SUBJECT_ONLY_POLICY_VERSION,
    ordinary_discovery_fit_policy,
)
from el.fitgate.service import FitService
from el.marketstructure.service import MarketStructureService
from el.models.market_adapter import FixtureMarketStructureProposer
from el.retrieval.provider import CandidateMarketRecord, FixtureMarketProvider
from el.retrieval.service import RetrievalService

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_PATH = FIXTURES / "retrieval" / "frozen_snapshot_phase0.json"
GOLDEN_PATH = FIXTURES / "markets" / "golden_market_structures.json"

GOLDENS = {
    s["market_id"]: MarketStructure.model_validate(s)
    for s in json.loads(GOLDEN_PATH.read_text())["structures"]
}


def _session_factory():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _lmsys_claim(**overrides) -> ExtractedStructure:
    base = dict(
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
    base.update(overrides)
    return ExtractedStructure(**base)


def _classify(
    structure: ExtractedStructure,
    *,
    structure_cap: int | None = None,
    policy=None,
):
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
    service = FitService(
        MarketStructureService(
            FixtureMarketStructureProposer(GOLDENS), sessions, top_n=structure_cap
        ),
        sessions,
        **({"policy": policy} if policy is not None else {}),
    )
    outcome = service.classify_fit(
        thesis_analysis_id, retrieval.candidate_set_id
    )
    return outcome, sessions


def test_direct_claim_end_to_end_card_recommendation_rejections():
    outcome, sessions = _classify(_lmsys_claim())

    assert outcome.fit_class.value == "direct"
    assert outcome.recommended_market_id == "mkt_gemini_lmsys_1"
    assert outcome.thesis_side == "yes"
    assert not outcome.draft_contract_recommended
    assert outcome.authority == "deterministic_only"
    assert outcome.rejected_count > 0

    with sessions() as session:
        card = session.scalars(select(FitCard)).one()
        assert card.semantic_fit_class == "direct"
        assert card.recommended_market_id == "mkt_gemini_lmsys_1"
        assert card.fit_confidence is None  # NULL, never a sentinel
        assert card.horizon_match == "good"
        assert card.resolution_risk == "low"
        assert "event_stage_match" in card.what_it_captures

        recommendation = session.scalars(select(MarketRecommendation)).one()
        assert recommendation.recommended_market_id == "mkt_gemini_lmsys_1"
        assert recommendation.fit_score is None
        assert recommendation.expression_type == "direct"
        assert recommendation.snapshot_id == "phase0-frozen-20260522"
        assert recommendation.contract_terms_hash
        assert recommendation.resolution_rules_hash
        assert recommendation.fit_card_id == card.id
        candidate_set = session.get(CandidateSet, card.candidate_set_id)
        assert recommendation.retrieval_id == candidate_set.retrieval_id

        rejections = session.scalars(select(RejectedMarketRow)).all()
        assert len(rejections) == outcome.rejected_count
        for row in rejections:
            assert row.market_recommendation_id == recommendation.id
            assert row.reason  # check-derived, never empty


def test_provenance_is_complete_or_nothing():
    outcome, sessions = _classify(_lmsys_claim())
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        provenance = card.provenance
        for key in (
            "gate_policy_version",
            "extraction_schema_version",
            "market_structure_schema_version",
            "model_adapter",
            "model_run_id",
            "trace_id",
            "eval_pack_version",
            "judged_at",
            "authority",
            "confidence_source",
            "escalation",
            "per_market",
            "structure_extraction",
        ):
            assert key in provenance, f"partial provenance: missing {key}"
        assert provenance["gate_policy_version"] == "loop3-v1"
        assert provenance["authority"] == "deterministic_only"
        assert (
            provenance["confidence_source"]
            == "deterministic_fallback_uncalibrated"
        )
        # Full check vectors per evaluated market (validated rejections
        # carry their evidence).
        recommended = provenance["per_market"]["mkt_gemini_lmsys_1"]
        assert recommended["ceiling"] == "direct"
        assert len(recommended["checks"]) == 10  # the full §14 check set


def test_explicit_successor_reaches_verdict_card_and_outcome_provenance():
    outcome, sessions = _classify(
        _lmsys_claim(), policy=ordinary_discovery_fit_policy()
    )
    assert outcome.gate_policy_version == M1_SUBJECT_ONLY_POLICY_VERSION
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        assert card.provenance["gate_policy_version"] == (
            M1_SUBJECT_ONLY_POLICY_VERSION
        )
        assert {
            row["gate_policy_version"]
            for row in card.provenance["per_market"].values()
        } == {M1_SUBJECT_ONLY_POLICY_VERSION}


def test_no_clean_claim_persists_null_recommendation_and_draft_flag():
    unmatched = _lmsys_claim(
        claim_summary="Zzcorp wins the underwater basket weaving cup in 2026.",
        entities=[Entity(name="Zzcorp", role="subject")],
        metric=Metric(
            what="underwater basket weaving championship outcome",
            measured_by="nobody",
            objective=True,
        ),
        resolution_source_class="press",
        contractible_version="Will Zzcorp win the cup by Dec 31, 2026?",
    )
    outcome, sessions = _classify(unmatched)

    assert outcome.fit_class.value == "no_clean_expression"
    assert outcome.recommended_market_id is None
    assert outcome.thesis_side is None
    assert outcome.draft_contract_recommended

    with sessions() as session:
        card = session.scalars(select(FitCard)).one()
        assert card.semantic_fit_class == "no_clean_expression"
        assert "No recommended expression" in card.what_it_captures
        recommendation = session.scalars(select(MarketRecommendation)).one()
        assert recommendation.recommended_market_id is None
        assert recommendation.contract_terms_hash is None
        # Every evaluated market is a rejection row with reasons.
        rejections = session.scalars(select(RejectedMarketRow)).all()
        assert len(rejections) == outcome.rejected_count > 0


def test_escalation_predicate_logs_only():
    outcome, sessions = _classify(_lmsys_claim())
    # v1: no calibrated advisory exists, so every judgment is
    # escalation-eligible — logged in provenance, decided by the
    # service, invisible to the class.
    assert outcome.escalation_eligible
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        assert "advisory_absent" in card.provenance["escalation"]["reasons"]


def test_published_class_equals_deterministic_ceiling_pre_advisory():
    # no_model_owned_final_class, service level: until the advisory
    # merge lands, the persisted class IS the ceiling.
    outcome, sessions = _classify(_lmsys_claim())
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        per_market = card.provenance["per_market"]
        recommended = per_market[card.recommended_market_id]
        assert card.semantic_fit_class == recommended["published"]
        assert recommended["published"] == recommended["ceiling"]


# --- top-N structure cap (recall preservation) -------------------------------


@pytest.mark.parametrize("cap", [None, 15, 10, 2, 1])
def test_recommendation_survives_structure_cap(cap):
    # The recommended LMSYS market is rank-1, so any cap >= 1 keeps it: Phase-0
    # recall does not drop the expected recommendation at N=10 or N=15 (no-ops
    # on a 10-market pool) NOR at the production-scale caps that actually bite.
    outcome, sessions = _classify(_lmsys_claim(), structure_cap=cap)

    assert outcome.fit_class.value == "direct"
    assert outcome.recommended_market_id == "mkt_gemini_lmsys_1"
    assert outcome.structure_cap == cap
    expected_structured = 10 if cap is None else min(cap, 10)
    assert outcome.structured_count == expected_structured

    # The funnel is recorded durably in card provenance.
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        funnel = card.provenance["structure_extraction"]
        assert funnel["retrieved_count"] == 10
        assert funnel["eligible_count"] == 10
        assert funnel["structured_count"] == expected_structured
        assert funnel["skipped_unstructured_count"] == 10 - expected_structured
        assert funnel["structure_cap"] == cap


# --- advisory integration (build commit 8) -------------------------------------


_ADVISORY_CONDITIONS = (
    "same_event_stage",
    "same_metric",
    "horizon_covers_claim",
    "subject_is_claim_subject",
    "resolution_observes_truth_conditions",
)


def _advisory(
    market_id: str,
    *,
    claim_evidence: str,
    market_evidence: str,
    confidence: float = 0.85,
):
    from el.models.fit_adapter import ConditionVerdict, FitAdvisory

    return FitAdvisory(
        market_id=market_id,
        condition_verdicts=[
            ConditionVerdict(
                condition=condition,
                status="pass",
                claim_evidence=claim_evidence,
                market_evidence=market_evidence,
            )
            for condition in _ADVISORY_CONDITIONS
        ],
        bridge_assumptions=["LMSYS keeps publishing the English leaderboard"],
        falsifier="LMSYS retires the leaderboard before resolution",
        suggested_class="direct",
        what_it_captures="Same leaderboard, same rank condition, same window.",
        what_it_misses="A tie counts for the market; the claim says ranked #1.",
        confidence=confidence,
    )


def _all_pass_advisory(market_id: str):
    # Complete (all five conditions) AND citation-bearing: both spans are
    # verbatim substrings of the LMSYS claim and of every snapshot market's
    # rules ("This market resolves to YES ...").
    return _advisory(
        market_id,
        claim_evidence="LMSYS Chatbot Arena",
        market_evidence="This market resolves to YES",
    )


def test_service_with_accepted_advisory_persists_confidence_and_text():
    from el.models.fit_adapter import FixtureFitProposer

    structure = _lmsys_claim()
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
    fixtures = {mid: _all_pass_advisory(mid) for mid in GOLDENS}
    service = FitService(
        MarketStructureService(
            FixtureMarketStructureProposer(GOLDENS), sessions
        ),
        sessions,
        advisory=FixtureFitProposer(fixtures),
    )
    outcome = service.classify_fit(thesis_analysis_id, retrieval.candidate_set_id)

    assert outcome.fit_class.value == "direct"
    assert outcome.fit_confidence == 0.85
    assert outcome.confidence_source == "advisory"
    assert outcome.authority == "deterministic_only"  # no veto occurred
    assert outcome.advisory_calls > 0
    assert outcome.advisory_fallbacks == 0

    with sessions() as session:
        card = session.scalars(select(FitCard)).one()
        assert card.fit_confidence == 0.85
        # Advisory text carries the card when accepted (vocabulary-gated).
        assert card.what_it_captures.startswith("Same leaderboard")
        per_market = card.provenance["per_market"]["mkt_gemini_lmsys_1"]
        assert per_market["advisory"]["status"] == "accepted"
        assert per_market["published"] == per_market["ceiling"]


def test_service_falls_back_when_proposer_keeps_failing():
    class ExplodingProposer:
        def propose_fit(self, **kwargs):
            raise RuntimeError("model unavailable")

    structure = _lmsys_claim()
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
    service = FitService(
        MarketStructureService(
            FixtureMarketStructureProposer(GOLDENS), sessions
        ),
        sessions,
        advisory=ExplodingProposer(),
    )
    outcome = service.classify_fit(thesis_analysis_id, retrieval.candidate_set_id)

    # A dead proposer degrades, never breaks: deterministic card, NULL
    # confidence, fallback authority, retry budget spent (2 calls/market).
    assert outcome.fit_class.value == "direct"
    assert outcome.fit_confidence is None
    assert outcome.confidence_source == "deterministic_fallback_uncalibrated"
    assert outcome.authority == "deterministic_fallback"
    assert outcome.advisory_fallbacks > 0
    assert outcome.advisory_calls == 2 * (
        outcome.rejected_count + 1
    )  # retry budget 1 -> two attempts per evaluated market


def test_service_rejects_advisory_with_invented_evidence():
    # Quote-span (anti-mad-libs): an advisory whose market_evidence is not
    # a verbatim quote of the rules is discarded -> deterministic fallback.
    from el.models.fit_adapter import FixtureFitProposer

    structure = _lmsys_claim()
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
    fixtures = {
        mid: _advisory(
            mid,
            claim_evidence="LMSYS Chatbot Arena",
            market_evidence="INVENTED EVIDENCE THAT IS IN NO MARKET'S RULES",
        )
        for mid in GOLDENS
    }
    service = FitService(
        MarketStructureService(
            FixtureMarketStructureProposer(GOLDENS), sessions
        ),
        sessions,
        advisory=FixtureFitProposer(fixtures),
    )
    outcome = service.classify_fit(thesis_analysis_id, retrieval.candidate_set_id)

    # Class still stands on the deterministic ceiling; the uncited advisory
    # never reaches the card.
    assert outcome.fit_class.value == "direct"
    assert outcome.confidence_source == "deterministic_fallback_uncalibrated"
    assert outcome.authority == "deterministic_fallback"
    assert outcome.fit_confidence is None
    assert outcome.advisory_fallbacks > 0


def test_service_card_serializes_through_public_contract():
    # Regression: a deterministic-fallback card (fit_confidence=None, rich
    # provenance with authority/escalation/per_market) must validate
    # through the public FitCardOut boundary. The strict Provenance model
    # used to forbid those extras, so the library could pass while the
    # API/MCP contract broke. This is the test that catches that.
    outcome, sessions = _classify(_lmsys_claim())
    assert outcome.fit_confidence is None  # the branch that used to break

    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        rejections = session.scalars(select(RejectedMarketRow)).all()
        dto = FitCardOut.model_validate(
            {
                "id": card.id,
                "thesis_analysis_id": card.thesis_analysis_id,
                "candidate_set_id": card.candidate_set_id,
                "semantic_fit_class": card.semantic_fit_class,
                "recommended_market_id": card.recommended_market_id,
                "what_it_captures": card.what_it_captures,
                "what_it_misses": card.what_it_misses,
                "horizon_match": card.horizon_match,
                "resolution_risk": card.resolution_risk,
                "rejected_markets": [
                    RejectedMarket(market_id=r.market_id, reason=r.reason)
                    for r in rejections
                ],
                "fit_confidence": card.fit_confidence,
                "draft_contract_id": card.draft_contract_id,
                "provenance": card.provenance,
            }
        )

    assert dto.fit_confidence is None
    assert dto.semantic_fit_class.value == "direct"
    assert dto.provenance.authority == "deterministic_only"
    assert (
        dto.provenance.confidence_source
        == "deterministic_fallback_uncalibrated"
    )
    # The Loop 3 fields survive the boundary (not dropped, not rejected).
    assert dto.provenance.per_market
    assert dto.provenance.escalation["eligible"] is True
    # The structure-cap funnel round-trips through the strict contract.
    assert dto.provenance.structure_extraction is not None
    assert dto.provenance.structure_extraction.structure_cap is None
    assert dto.provenance.structure_extraction.structured_count == 10


def _mock_record(market_id: str, title: str = "Mock Title") -> CandidateMarketRecord:
    return CandidateMarketRecord(
        market_id=market_id,
        title=title,
        venue="SyntheticPhase0",
        description="Mock Description",
        resolution_rules="Mock rules",
        close_date=date(2026, 12, 31),
        current_probability=0.5,
    )


def _mock_market_structure(
    market_id: str,
    *,
    horizon_date: date = date(2026, 12, 31),
    diff_entities: bool = False,
    diff_stage: bool = False,
) -> MarketStructure:
    entities = [
        Entity(name="Google Gemini", role="subject"),
        Entity(name="LMSYS Chatbot Arena", role="venue"),
    ]
    if diff_entities:
        entities = [
            Entity(name="Wrong Entity", role="subject"),
            Entity(name="LMSYS Chatbot Arena", role="venue"),
        ]
    stage = "measured"
    if diff_stage:
        stage = "announced"
    return MarketStructure(
        schema_version=1,
        market_id=market_id,
        snapshot_id="phase0-frozen-20260522",
        event_stage=stage,
        metric=Metric(
            what="rank #1 (or tied) on the English LMSYS Chatbot Arena leaderboard",
            measured_by="LMSYS Chatbot Arena leaderboard",
            objective=True,
        ),
        horizon={
            "resolution_date": horizon_date,
            "timezone": "America/New_York",
        },
        entities=entities,
        threshold="rank #1 or tied for #1",
        direction=None,
        resolution_source_class="leaderboard",
        extraction_policy_version=2,
    )


def _classify_adaptive(
    structure: ExtractedStructure,
    records: list[CandidateMarketRecord],
    goldens: dict[str, MarketStructure],
    *,
    cap_initial: int | None = None,
    cap_expanded: int | None = None,
):
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

    provider = FixtureMarketProvider(
        snapshot_id="phase0-frozen-20260522",
        as_of_ts=datetime.now(timezone.utc),
        markets=records,
    )
    retrieval = RetrievalService(provider, sessions).retrieve_candidates(
        thesis_analysis_id
    )
    service = FitService(
        MarketStructureService(
            FixtureMarketStructureProposer(goldens), sessions, top_n=None
        ),
        sessions,
        cap_initial=cap_initial,
        cap_expanded=cap_expanded,
    )
    outcome = service.classify_fit(
        thesis_analysis_id, retrieval.candidate_set_id
    )
    return outcome, sessions


def test_adaptive_cap_initial_direct_no_expansion():
    claim = _lmsys_claim()
    records = [_mock_record("m1")]
    goldens = {"m1": _mock_market_structure("m1", horizon_date=date(2026, 12, 31))}
    outcome, sessions = _classify_adaptive(
        claim, records, goldens, cap_initial=5, cap_expanded=20
    )
    assert outcome.fit_class.value == "direct"
    assert outcome.expanded is False
    assert outcome.final_cap == 5
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        se = card.provenance["structure_extraction"]
        assert se["expanded"] is False
        assert se["initial_cap"] == 5
        assert se["final_cap"] == 5
        assert se["initial_fit_class"] == "direct"
        assert se["final_fit_class"] == "direct"
        assert se["expansion_reason"] is None


def test_adaptive_cap_initial_indirect_expansion():
    claim = _lmsys_claim()
    records = [
        _mock_record("m1"),
        _mock_record("m2"),
        _mock_record("m3"),
        _mock_record("m4"),
        _mock_record("m5"),
        _mock_record("m6"),
    ]
    goldens = {
        "m1": _mock_market_structure("m1", horizon_date=date(2026, 12, 20)),
        "m2": _mock_market_structure("m2", horizon_date=date(2026, 12, 20)),
        "m3": _mock_market_structure("m3", horizon_date=date(2026, 12, 20)),
        "m4": _mock_market_structure("m4", horizon_date=date(2026, 12, 20)),
        "m5": _mock_market_structure("m5", horizon_date=date(2026, 12, 20)),
        "m6": _mock_market_structure("m6", horizon_date=date(2026, 12, 20)),
    }
    outcome, sessions = _classify_adaptive(
        claim, records, goldens, cap_initial=5, cap_expanded=20
    )
    assert outcome.fit_class.value == "indirect"
    assert outcome.expanded is True
    assert outcome.final_cap == 20
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        se = card.provenance["structure_extraction"]
        assert se["expanded"] is True
        assert se["initial_cap"] == 5
        assert se["final_cap"] == 20
        assert se["initial_fit_class"] == "indirect"
        assert se["final_fit_class"] == "indirect"
        assert se["expansion_reason"] == "fit_class_indirect"


def test_adaptive_cap_upgrade_to_direct():
    claim = _lmsys_claim()
    records = [
        _mock_record("m1"),
        _mock_record("m2"),
        _mock_record("m3"),
        _mock_record("m4"),
        _mock_record("m5"),
        _mock_record("m6"),
    ]
    goldens = {
        "m1": _mock_market_structure("m1", horizon_date=date(2026, 12, 20)), # indirect
        "m2": _mock_market_structure("m2", horizon_date=date(2026, 12, 20)),
        "m3": _mock_market_structure("m3", horizon_date=date(2026, 12, 20)),
        "m4": _mock_market_structure("m4", horizon_date=date(2026, 12, 20)),
        "m5": _mock_market_structure("m5", horizon_date=date(2026, 12, 20)),
        "m6": _mock_market_structure("m6", horizon_date=date(2026, 12, 31)), # direct!
    }
    outcome, sessions = _classify_adaptive(
        claim, records, goldens, cap_initial=5, cap_expanded=20
    )
    assert outcome.fit_class.value == "direct"
    assert outcome.expanded is True
    assert outcome.final_cap == 20
    with sessions() as session:
        # Assert exactly ONE card is saved
        cards = session.scalars(select(FitCard)).all()
        assert len(cards) == 1
        card = cards[0]
        se = card.provenance["structure_extraction"]
        assert se["expanded"] is True
        assert se["initial_cap"] == 5
        assert se["final_cap"] == 20
        assert se["initial_fit_class"] == "indirect"
        assert se["final_fit_class"] == "direct"
        assert se["initial_recommended_market_id"] == "m1"
        assert se["final_recommended_market_id"] == "m6"


def test_adaptive_cap_weak_proxy_expansion():
    claim = _lmsys_claim()
    records = [
        _mock_record("m1"),
        _mock_record("m2"),
        _mock_record("m3"),
        _mock_record("m4"),
        _mock_record("m5"),
        _mock_record("m6"),
    ]
    goldens = {
        "m1": _mock_market_structure("m1", horizon_date=date(2026, 12, 10)), # weak
        "m2": _mock_market_structure("m2", horizon_date=date(2026, 12, 10)),
        "m3": _mock_market_structure("m3", horizon_date=date(2026, 12, 10)),
        "m4": _mock_market_structure("m4", horizon_date=date(2026, 12, 10)),
        "m5": _mock_market_structure("m5", horizon_date=date(2026, 12, 10)),
        "m6": _mock_market_structure("m6", horizon_date=date(2026, 12, 10)),
    }
    outcome, sessions = _classify_adaptive(
        claim, records, goldens, cap_initial=5, cap_expanded=20
    )
    assert outcome.fit_class.value == "weak_proxy"
    assert outcome.expanded is True
    assert outcome.final_cap == 20
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        se = card.provenance["structure_extraction"]
        assert se["expanded"] is True
        assert se["initial_fit_class"] == "weak_proxy"
        assert se["final_fit_class"] == "weak_proxy"


def test_adaptive_cap_no_clean_expansion():
    claim = _lmsys_claim()
    records = [
        _mock_record("m1"),
        _mock_record("m2"),
        _mock_record("m3"),
        _mock_record("m4"),
        _mock_record("m5"),
        _mock_record("m6"),
    ]
    # entity diff + stage diff = 2 hard fails -> no_clean_expression
    goldens = {
        "m1": _mock_market_structure("m1", horizon_date=date(2026, 12, 10), diff_entities=True),
        "m2": _mock_market_structure("m2", horizon_date=date(2026, 12, 10), diff_entities=True),
        "m3": _mock_market_structure("m3", horizon_date=date(2026, 12, 10), diff_entities=True),
        "m4": _mock_market_structure("m4", horizon_date=date(2026, 12, 10), diff_entities=True),
        "m5": _mock_market_structure("m5", horizon_date=date(2026, 12, 10), diff_entities=True),
        "m6": _mock_market_structure("m6", horizon_date=date(2026, 12, 10), diff_entities=True),
    }
    outcome, sessions = _classify_adaptive(
        claim, records, goldens, cap_initial=5, cap_expanded=20
    )
    assert outcome.fit_class.value == "no_clean_expression"
    assert outcome.expanded is True
    assert outcome.final_cap == 20
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        se = card.provenance["structure_extraction"]
        assert se["expanded"] is True
        assert se["initial_fit_class"] == "no_clean_expression"
        assert se["final_fit_class"] == "no_clean_expression"


def test_adaptive_cap_no_expansion_when_few_candidates():
    claim = _lmsys_claim()
    # 3 candidates <= cap_initial (5)
    records = [_mock_record("m1"), _mock_record("m2"), _mock_record("m3")]
    goldens = {
        "m1": _mock_market_structure("m1", horizon_date=date(2026, 12, 20)),
        "m2": _mock_market_structure("m2", horizon_date=date(2026, 12, 20)),
        "m3": _mock_market_structure("m3", horizon_date=date(2026, 12, 20)),
    }
    outcome, sessions = _classify_adaptive(
        claim, records, goldens, cap_initial=5, cap_expanded=20
    )
    assert outcome.fit_class.value == "indirect"
    assert outcome.expanded is False
    assert outcome.final_cap == 5
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        se = card.provenance["structure_extraction"]
        assert se["expanded"] is False
        assert se["initial_cap"] == 5
        assert se["final_cap"] == 5


def test_adaptive_cap_dto_round_trip():
    claim = _lmsys_claim()
    records = [
        _mock_record("m1"),
        _mock_record("m2"),
        _mock_record("m3"),
        _mock_record("m4"),
        _mock_record("m5"),
        _mock_record("m6"),
    ]
    goldens = {
        "m1": _mock_market_structure("m1", horizon_date=date(2026, 12, 20)),
        "m2": _mock_market_structure("m2", horizon_date=date(2026, 12, 20)),
        "m3": _mock_market_structure("m3", horizon_date=date(2026, 12, 20)),
        "m4": _mock_market_structure("m4", horizon_date=date(2026, 12, 20)),
        "m5": _mock_market_structure("m5", horizon_date=date(2026, 12, 20)),
        "m6": _mock_market_structure("m6", horizon_date=date(2026, 12, 31)),
    }
    outcome, sessions = _classify_adaptive(
        claim, records, goldens, cap_initial=5, cap_expanded=20
    )
    assert outcome.expanded is True
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        rejections = session.scalars(select(RejectedMarketRow)).all()
        dto = FitCardOut.model_validate(
            {
                "id": card.id,
                "thesis_analysis_id": card.thesis_analysis_id,
                "candidate_set_id": card.candidate_set_id,
                "semantic_fit_class": card.semantic_fit_class,
                "recommended_market_id": card.recommended_market_id,
                "what_it_captures": card.what_it_captures,
                "what_it_misses": card.what_it_misses,
                "horizon_match": card.horizon_match,
                "resolution_risk": card.resolution_risk,
                "rejected_markets": [
                    RejectedMarket(market_id=r.market_id, reason=r.reason)
                    for r in rejections
                ],
                "fit_confidence": card.fit_confidence,
                "draft_contract_id": card.draft_contract_id,
                "provenance": card.provenance,
            }
        )
    assert dto.provenance.structure_extraction.expanded is True
    assert dto.provenance.structure_extraction.initial_cap == 5
    assert dto.provenance.structure_extraction.final_cap == 20
    assert dto.provenance.structure_extraction.initial_fit_class == "indirect"
    assert dto.provenance.structure_extraction.final_fit_class == "direct"
    assert dto.provenance.structure_extraction.cap_policy_version == "adaptive-v1"


def test_adaptive_cap_advisory_calls_exactly_once():
    from el.models.fit_adapter import FixtureFitProposer

    class SpyFitProposer(FixtureFitProposer):
        def __init__(self, fixtures):
            super().__init__(fixtures)
            self.called_market_ids = []

        def propose_fit(self, **kwargs):
            self.called_market_ids.append(kwargs["market_id"])
            return super().propose_fit(**kwargs)

    claim = _lmsys_claim()
    records = [
        _mock_record(mid).model_copy(update={"resolution_rules": "This market resolves to YES"})
        for mid in ["m1", "m2", "m3", "m4", "m5", "m6"]
    ]
    goldens = {
        "m1": _mock_market_structure("m1", horizon_date=date(2026, 12, 20)),
        "m2": _mock_market_structure("m2", horizon_date=date(2026, 12, 20)),
        "m3": _mock_market_structure("m3", horizon_date=date(2026, 12, 20)),
        "m4": _mock_market_structure("m4", horizon_date=date(2026, 12, 20)),
        "m5": _mock_market_structure("m5", horizon_date=date(2026, 12, 20)),
        "m6": _mock_market_structure("m6", horizon_date=date(2026, 12, 31)),
    }
    fixtures = {mid: _all_pass_advisory(mid) for mid in goldens}
    spy_advisory = SpyFitProposer(fixtures)

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
        thesis_analysis_id = analysis.id

    provider = FixtureMarketProvider(
        snapshot_id="phase0-frozen-20260522",
        as_of_ts=datetime.now(timezone.utc),
        markets=records,
    )
    retrieval = RetrievalService(provider, sessions).retrieve_candidates(
        thesis_analysis_id
    )
    service = FitService(
        MarketStructureService(
            FixtureMarketStructureProposer(goldens), sessions, top_n=None
        ),
        sessions,
        advisory=spy_advisory,
        cap_initial=5,
        cap_expanded=20,
    )
    outcome = service.classify_fit(
        thesis_analysis_id, retrieval.candidate_set_id
    )

    assert outcome.expanded is True
    assert len(spy_advisory.called_market_ids) == 6
    from collections import Counter
    counts = Counter(spy_advisory.called_market_ids)
    for mid in ["m1", "m2", "m3", "m4", "m5", "m6"]:
        assert counts[mid] == 1


def test_adaptive_cap_partial_config_preserves_single_pass():
    claim = _lmsys_claim()
    records = [
        _mock_record("m1"),
        _mock_record("m2"),
        _mock_record("m3"),
        _mock_record("m4"),
        _mock_record("m5"),
        _mock_record("m6"),
    ]
    goldens = {
        "m1": _mock_market_structure("m1", horizon_date=date(2026, 12, 20)),
        "m2": _mock_market_structure("m2", horizon_date=date(2026, 12, 20)),
        "m3": _mock_market_structure("m3", horizon_date=date(2026, 12, 20)),
        "m4": _mock_market_structure("m4", horizon_date=date(2026, 12, 20)),
        "m5": _mock_market_structure("m5", horizon_date=date(2026, 12, 20)),
        "m6": _mock_market_structure("m6", horizon_date=date(2026, 12, 31)),
    }
    # Case 1: cap_initial is set, cap_expanded is None
    outcome, sessions = _classify_adaptive(
        claim, records, goldens, cap_initial=5, cap_expanded=None
    )
    assert outcome.fit_class.value == "indirect"
    assert outcome.expanded is False
    assert outcome.final_cap == 5
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        se = card.provenance["structure_extraction"]
        assert se["expanded"] is False

    # Case 2: cap_initial is None, cap_expanded is set
    outcome, sessions = _classify_adaptive(
        claim, records, goldens, cap_initial=None, cap_expanded=20
    )
    assert outcome.fit_class.value == "direct"  # single pass processes all 6, m6 (direct) is included
    assert outcome.expanded is False
    assert outcome.final_cap is None
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        se = card.provenance["structure_extraction"]
        assert se["expanded"] is False
