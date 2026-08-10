"""Step 4 — market-side structure: gate, content-keyed cache, goldens."""

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
from el.domain.tables import (
    Base,
    CandidateSetMember,
    MarketRulesCapture,
    MarketStructureRow,
    ThesisAnalysis,
)
from el.marketstructure.gate import (
    MarketGateVerdict,
    market_structure_gate,
)
from el.marketstructure.service import MarketStructureService
from el.models.market_adapter import FixtureMarketStructureProposer
from el.retrieval.provider import FixtureMarketProvider
from el.retrieval.service import RetrievalService

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_PATH = FIXTURES / "retrieval" / "frozen_snapshot_phase0.json"
GOLDEN_PATH = FIXTURES / "markets" / "golden_market_structures.json"


def _goldens() -> dict[str, MarketStructure]:
    raw = json.loads(GOLDEN_PATH.read_text())
    return {
        s["market_id"]: MarketStructure.model_validate(s)
        for s in raw["structures"]
    }


GOLDENS = _goldens()


def _session_factory():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _structure(**overrides) -> ExtractedStructure:
    base = dict(
        claim_summary="Gemini ranks #1 on LMSYS Chatbot Arena by the end of 2026.",
        entities=[Entity(name="Google Gemini", role="subject")],
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


def _insert_analysis(sessions) -> uuid.UUID:
    structure = _structure()
    with sessions() as session:
        row = ThesisAnalysis(
            input_text=structure.claim_summary,
            extracted_structure=structure.model_dump(mode="json"),
            normalized_claim_summary=structure.claim_summary,
            client_type="human_ui",
        )
        session.add(row)
        session.commit()
        return row.id


def _candidate_set(sessions, provider=None) -> uuid.UUID:
    provider = provider or FixtureMarketProvider.from_path(SNAPSHOT_PATH)
    outcome = RetrievalService(provider, sessions).retrieve_candidates(
        _insert_analysis(sessions)
    )
    return outcome.candidate_set_id


# --- goldens ----------------------------------------------------------


def test_all_goldens_validate_and_cover_the_pool():
    assert len(GOLDENS) == 10
    for market_id, golden in GOLDENS.items():
        assert golden.market_id == market_id
        assert golden.schema_version == 1


def test_goldens_pass_the_gate():
    for golden in GOLDENS.values():
        result = market_structure_gate(
            market_id=golden.market_id,
            snapshot_id=golden.snapshot_id,
            structure=golden,
        )
        assert result.verdict is MarketGateVerdict.PASS, (
            golden.market_id,
            result.reasons,
        )


# --- gate -------------------------------------------------------------


def test_gate_rejects_identity_mismatch():
    golden = GOLDENS["mkt_gemini_lmsys_1"]
    result = market_structure_gate(
        market_id="some_other_market",
        snapshot_id=golden.snapshot_id,
        structure=golden,
    )
    assert result.verdict is MarketGateVerdict.REJECTED_INVALID
    assert "identity mismatch" in result.reasons[0]


def test_gate_rejects_stale_extraction_policy():
    # v1 semantics (e.g. settlement-date horizons) must not pass as v2.
    golden = GOLDENS["mkt_gemini_lmsys_1"]
    stale = golden.model_copy(update={"extraction_policy_version": 1})
    result = market_structure_gate(
        market_id=stale.market_id,
        snapshot_id=stale.snapshot_id,
        structure=stale,
    )
    assert result.verdict is MarketGateVerdict.REJECTED_INVALID
    assert "stale extraction policy" in result.reasons[0]


def test_gate_rejects_restricted_vocabulary():
    golden = GOLDENS["mkt_msft_stock_high"]
    tainted = golden.model_copy(
        update={
            "metric": Metric(
                what="you should buy when the price has edge",
                measured_by="NASDAQ",
                objective=True,
            )
        }
    )
    result = market_structure_gate(
        market_id=tainted.market_id,
        snapshot_id=tainted.snapshot_id,
        structure=tainted,
    )
    assert result.verdict is MarketGateVerdict.REJECTED_INVALID
    assert "restricted vocabulary" in result.reasons[0]


# --- service: cache behavior (ratification item 8) ----------------------


def test_first_run_extracts_then_cache_serves_repeat_snapshot():
    sessions = _session_factory()
    proposer = FixtureMarketStructureProposer(GOLDENS)
    service = MarketStructureService(proposer, sessions)

    first = service.ensure_structures(_candidate_set(sessions))
    assert first.extracted == 10
    assert first.cache_hits == 0
    assert proposer.calls == 10

    # Same snapshot again (new analysis, new candidate set, same rules).
    second = service.ensure_structures(_candidate_set(sessions))
    assert second.extracted == 0
    assert second.cache_hits == 10
    assert proposer.calls == 10  # not a single new model call
    assert len(second.structures) == 10


def test_cache_reuses_across_snapshots_with_unchanged_rules():
    sessions = _session_factory()
    proposer = FixtureMarketStructureProposer(GOLDENS)
    service = MarketStructureService(proposer, sessions)
    service.ensure_structures(_candidate_set(sessions))
    assert proposer.calls == 10

    # New snapshot id, identical rules text -> identical hashes -> reuse.
    provider = FixtureMarketProvider(
        snapshot_id="phase0-frozen-NEXTDAY",
        as_of_ts=datetime(2026, 5, 23, tzinfo=timezone.utc),
        markets=FixtureMarketProvider.from_path(SNAPSHOT_PATH)
        .retrieve(None)
        .markets,
    )
    outcome = service.ensure_structures(_candidate_set(sessions, provider))
    assert outcome.snapshot_id == "phase0-frozen-NEXTDAY"
    assert outcome.cache_hits == 10
    assert outcome.extracted == 0
    assert proposer.calls == 10
    # Cache hits return the structure stamped with the CURRENT snapshot,
    # never the stale first-capture snapshot embedded in the cached row.
    assert {s.snapshot_id for s in outcome.structures} == {
        "phase0-frozen-NEXTDAY"
    }


def test_rules_change_forces_reextraction_only_for_changed_market():
    sessions = _session_factory()
    proposer = FixtureMarketStructureProposer(GOLDENS)
    service = MarketStructureService(proposer, sessions)
    service.ensure_structures(_candidate_set(sessions))
    assert proposer.calls == 10

    # Amend one market's resolution rules in a new snapshot.
    base = FixtureMarketProvider.from_path(SNAPSHOT_PATH).retrieve(None).markets
    amended = [
        m.model_copy(
            update={"resolution_rules": m.resolution_rules + " AMENDED."}
        )
        if m.market_id == "mkt_gemini_lmsys_1"
        else m
        for m in base
    ]
    provider = FixtureMarketProvider(
        snapshot_id="phase0-frozen-AMENDED",
        as_of_ts=datetime(2026, 5, 24, tzinfo=timezone.utc),
        markets=amended,
    )
    outcome = service.ensure_structures(_candidate_set(sessions, provider))
    assert outcome.cache_hits == 9
    assert outcome.extracted == 1
    assert proposer.calls == 11

    with sessions() as session:
        rows = session.scalars(
            select(MarketStructureRow).where(
                MarketStructureRow.market_id == "mkt_gemini_lmsys_1"
            )
        ).all()
        assert len(rows) == 2  # one per rules-content version
        assert {r.snapshot_id for r in rows} == {
            "phase0-frozen-20260522",
            "phase0-frozen-AMENDED",
        }


def test_service_skips_ineligible_members():
    sessions = _session_factory()
    records = FixtureMarketProvider.from_path(SNAPSHOT_PATH).retrieve(None).markets
    thin = [
        m.model_copy(update={"liquidity_usd": 5.0})
        if m.market_id == "mkt_gpt5_release_1"
        else m
        for m in records
    ]
    provider = FixtureMarketProvider(
        snapshot_id="phase0-frozen-THIN",
        as_of_ts=datetime(2026, 5, 25, tzinfo=timezone.utc),
        markets=thin,
    )
    proposer = FixtureMarketStructureProposer(GOLDENS)
    service = MarketStructureService(proposer, sessions)
    outcome = service.ensure_structures(_candidate_set(sessions, provider))
    assert outcome.skipped_ineligible == 1
    assert outcome.extracted == 9
    assert all(
        s.market_id != "mkt_gpt5_release_1" for s in outcome.structures
    )


# --- service: top-N structure cap ----------------------------------------


def test_top_n_cap_bounds_structured_set_and_calls():
    # The whole universe is retrieved + eligible, but only the top-N ranked
    # eligible candidates are structured; Gemini (the proposer) is called for
    # the bounded set, never the universe. This is the core scaling invariant.
    sessions = _session_factory()
    proposer = FixtureMarketStructureProposer(GOLDENS)
    service = MarketStructureService(proposer, sessions, top_n=3)
    candidate_set_id = _candidate_set(sessions)
    outcome = service.ensure_structures(candidate_set_id)

    assert outcome.retrieved_count == 10
    assert outcome.eligible_count == 10
    assert outcome.structure_cap == 3
    assert outcome.structured_count == 3
    assert len(outcome.structures) == 3
    assert outcome.skipped_unstructured_count == 7
    assert proposer.calls == 3  # bounded, NOT 10

    # The structured markets are exactly the top-3 eligible by rank.
    with sessions() as session:
        ranked = [
            m.market_id
            for m in session.scalars(
                select(CandidateSetMember)
                .where(CandidateSetMember.candidate_set_id == candidate_set_id)
                .order_by(CandidateSetMember.rank)
            ).all()
        ]
    assert {s.market_id for s in outcome.structures} == set(ranked[:3])


def test_top_n_none_structures_full_eligible_set():
    # Default (unbounded) preserves pre-cap behaviour exactly.
    sessions = _session_factory()
    proposer = FixtureMarketStructureProposer(GOLDENS)
    service = MarketStructureService(proposer, sessions)
    outcome = service.ensure_structures(_candidate_set(sessions))
    assert outcome.structure_cap is None
    assert outcome.structured_count == 10
    assert outcome.skipped_unstructured_count == 0
    assert proposer.calls == 10


def test_top_n_cap_at_or_above_eligible_is_a_noop():
    # The 10-market fixture is unchanged under the production default (15).
    sessions = _session_factory()
    proposer = FixtureMarketStructureProposer(GOLDENS)
    service = MarketStructureService(proposer, sessions, top_n=15)
    outcome = service.ensure_structures(_candidate_set(sessions))
    assert outcome.structured_count == 10  # 10 eligible <= 15
    assert outcome.skipped_unstructured_count == 0
    assert outcome.structure_cap == 15
    assert proposer.calls == 10


def test_service_requires_prior_retrieval():
    sessions = _session_factory()
    service = MarketStructureService(
        FixtureMarketStructureProposer(GOLDENS), sessions
    )
    with pytest.raises(ValueError, match="not found"):
        service.ensure_structures(uuid.uuid4())


def test_rejected_proposal_does_not_persist():
    sessions = _session_factory()

    # Proposer that always answers about the gemini market -> identity
    # mismatch everywhere except the gemini market itself.
    class WrongIdProposer:
        def __init__(self):
            self.calls = 0

        def propose_market_structure(self, *, market_id, snapshot_id, **_):
            self.calls += 1
            return type(
                "R",
                (),
                {
                    "structure": GOLDENS["mkt_gemini_lmsys_1"].model_copy(
                        update={"snapshot_id": snapshot_id}
                    ),
                    "model_adapter": "wrong-id-fixture",
                    "model_run_id": f"w-{self.calls}",
                },
            )()

    service = MarketStructureService(WrongIdProposer(), sessions)
    outcome = service.ensure_structures(_candidate_set(sessions))
    # Only the genuine gemini market passes (ids happen to match there).
    assert outcome.extracted == 1
    assert len(outcome.rejected) == 9
    assert all("identity mismatch" in r.reasons[0] for r in outcome.rejected)
    with sessions() as session:
        rows = session.scalars(select(MarketStructureRow)).all()
        assert len(rows) == 1


def test_rules_capture_precondition_message():
    sessions = _session_factory()
    candidate_set_id = _candidate_set(sessions)
    # Simulate a candidate set whose rules captures were never persisted.
    with sessions() as session:
        for capture in session.scalars(select(MarketRulesCapture)).all():
            session.delete(capture)
        session.commit()
    service = MarketStructureService(
        FixtureMarketStructureProposer(GOLDENS), sessions
    )
    with pytest.raises(ValueError, match="retrieval must run before"):
        service.ensure_structures(candidate_set_id)
