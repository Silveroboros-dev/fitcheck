"""MCP tool contract tests (step 7) — the binding adversarial set."""

import json
import uuid
from datetime import date, timedelta
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
    ApiClient,
    Base,
    ConvictionEvent,
    MarketRecommendation,
    ReviewCandidate,
    ThesisAnalysis,
    User,
)
from el.draftcontract.service import DraftContractService
from el.extraction.service import ExtractionService
from el.fitgate.service import FitService
from el.ledger.service import LedgerService
from el.marketstructure.service import MarketStructureService
from el.mcp.auth import hash_api_key, resolve_principal
from el.mcp.contracts import BlindPriorRequired, NotFound
from el.mcp.tools import McpTools
from el.mcp.vocab_guard import A7Violation, assert_a7_clean
from el.models.adapter import ExtractionProposal, FixtureProposer
from el.models.draft_adapter import FixtureDraftProposer, ProposedDraft
from el.models.market_adapter import FixtureMarketStructureProposer
from el.mcp.contracts import FitCardResult, NormalizeResult
from el.retrieval.provider import FixtureMarketProvider
from el.retrieval.service import RetrievalService

FIX = Path(__file__).parent / "fixtures"
CLAIMS_DIR = FIX / "claims"
SNAPSHOT = FIX / "retrieval" / "frozen_snapshot_phase0.json"
GOLDENS = {
    s["market_id"]: MarketStructure.model_validate(s)
    for s in json.loads((FIX / "markets" / "golden_market_structures.json").read_text())[
        "structures"
    ]
}
CLAIM_FIXTURES = {
    json.loads(p.read_text())["input_text"]: ExtractionProposal.model_validate(
        json.loads(p.read_text())["proposal"]
    )
    for p in sorted(CLAIMS_DIR.glob("*.json"))
}
CLEAN = "Gemini is going to be ranked #1 chatbot on LMSYS Chatbot Arena by the end of 2026."
NO_CLEAN_SUMMARY = "Zzcorp wins the underwater basket weaving cup in 2026."


def _sessions():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


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


def _tools(sessions):
    extraction = ExtractionService(FixtureProposer(CLAIM_FIXTURES), sessions)
    retrieval = RetrievalService(FixtureMarketProvider.from_path(SNAPSHOT), sessions)
    fit = FitService(
        MarketStructureService(FixtureMarketStructureProposer(GOLDENS), sessions),
        sessions,
    )
    draft = DraftContractService(
        FixtureDraftProposer({NO_CLEAN_SUMMARY: _good_draft()}), sessions
    )
    ledger = LedgerService(sessions)
    return McpTools(
        extraction=extraction,
        retrieval=retrieval,
        fit=fit,
        draft=draft,
        ledger=ledger,
        session_factory=sessions,
    )


def _principal(sessions, *, client_type="agent_mcp", key=None):
    key = key or f"key-{uuid.uuid4()}"
    with sessions() as s:
        user = User(email=f"u-{uuid.uuid4()}@example.com")
        s.add(user)
        s.flush()
        s.add(
            ApiClient(
                user_id=user.id,
                key_hash=hash_api_key(key),
                client_type=client_type,
                rate_limit_tier="default",
            )
        )
        s.commit()
    with sessions() as s:
        return resolve_principal(s, key)


def _seed_no_clean_thesis(sessions, principal=None) -> uuid.UUID:
    structure = ExtractedStructure(
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
    with sessions() as s:
        a = ThesisAnalysis(
            input_text="zz",
            extracted_structure=structure.model_dump(mode="json"),
            normalized_claim_summary=NO_CLEAN_SUMMARY,
            client_type=(
                principal.client_type.value if principal is not None else "agent_mcp"
            ),
            agent_client_id=(
                principal.agent_client_id if principal is not None else None
            ),
        )
        s.add(a)
        s.commit()
        return a.id


# --- 1. full agent loop -------------------------------------------------
def test_full_agent_loop():
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    norm = tools.normalize_claim(p, input_text=CLEAN)
    tid = norm.thesis_analysis_id

    preview = tools.preview_market_fit(p, thesis_analysis_id=tid)
    assert preview.current_odds is None  # pre-prior: withheld

    tools.submit_blind_prior(p, thesis_analysis_id=tid, prior_probability=0.7)
    card = tools.classify_market_fit(p, thesis_analysis_id=tid)
    assert card.semantic_fit_class == "direct"
    assert card.current_odds is not None  # revealed after the prior

    saved = tools.create_ledger_entry(
        p,
        fit_card_id=card.fit_card_id,
        conviction_level="leaning",
        intended_exposure_bucket="$100",
        user_justification="Strong leaderboard momentum.",
    )
    assert saved.attestation_status == "unattested"  # agent save
    got = tools.get_ledger_entry(p, ledger_entry_id=saved.id)
    assert got.id == saved.id


def test_unbound_legacy_card_does_not_borrow_newer_run_rejections():
    sessions = _sessions()
    tools = _tools(sessions)
    principal = _principal(sessions)
    thesis_id = tools.normalize_claim(
        principal, input_text=CLEAN
    ).thesis_analysis_id
    tools.submit_blind_prior(
        principal, thesis_analysis_id=thesis_id, prior_probability=0.7
    )

    older = tools.classify_market_fit(principal, thesis_analysis_id=thesis_id)
    newer = tools.classify_market_fit(principal, thesis_analysis_id=thesis_id)

    with sessions() as session:
        older_recommendation = session.scalar(
            select(MarketRecommendation).where(
                MarketRecommendation.fit_card_id == older.fit_card_id
            )
        )
        newer_recommendation = session.scalar(
            select(MarketRecommendation).where(
                MarketRecommendation.fit_card_id == newer.fit_card_id
            )
        )
        assert older_recommendation is not None
        assert newer_recommendation is not None
        assert tools._rejected_markets(session, older.fit_card_id)
        assert tools._rejected_markets(session, newer.fit_card_id)

        # Simulate an expand/contract legacy card whose recommendation could
        # not be unambiguously backfilled. It must not inherit the newer run's
        # rejection evidence merely because the thesis ID matches.
        older_recommendation.fit_card_id = None
        newer_recommendation.created_at = (
            older_recommendation.created_at + timedelta(seconds=1)
        )
        session.commit()

        assert tools._rejected_markets(session, older.fit_card_id) == []
        assert tools._rejected_markets(session, newer.fit_card_id)


# --- 2. classify before prior -> blind_prior_required -------------------
def test_classify_before_prior_raises_blind_prior_required():
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    tid = tools.normalize_claim(p, input_text=CLEAN).thesis_analysis_id
    with pytest.raises(BlindPriorRequired):
        tools.classify_market_fit(p, thesis_analysis_id=tid)


# --- 3. preview before prior -> odds withheld everywhere ----------------
def test_preview_withholds_all_odds():
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    tid = tools.normalize_claim(p, input_text=CLEAN).thesis_analysis_id
    preview = tools.preview_market_fit(p, thesis_analysis_id=tid)
    assert preview.current_odds is None
    assert preview.provenance["mcp_preview"] is True
    assert preview.provenance["odds_withheld"] is True
    blob = json.dumps(preview.model_dump(mode="json"))
    assert "current_probability" not in blob  # scrubbed nested too


def test_preview_before_prior_records_fit_context_without_odds():
    sessions = _sessions()
    tools = _tools(sessions)
    principal = _principal(sessions)
    thesis_id = tools.normalize_claim(principal, input_text=CLEAN).thesis_analysis_id

    tools.preview_market_fit(principal, thesis_analysis_id=thesis_id)
    tools.submit_blind_prior(
        principal, thesis_analysis_id=thesis_id, prior_probability=0.6
    )

    with sessions() as session:
        event = session.scalar(
            select(ConvictionEvent).where(
                ConvictionEvent.thesis_analysis_id == thesis_id
            )
        )
        assert event.prior_type == "blind"
        assert event.market_context_seen is True
        assert event.odds_revealed_at is None


def test_prior_without_preview_records_no_fit_context():
    sessions = _sessions()
    tools = _tools(sessions)
    principal = _principal(sessions)
    thesis_id = tools.normalize_claim(principal, input_text=CLEAN).thesis_analysis_id

    tools.submit_blind_prior(
        principal, thesis_analysis_id=thesis_id, prior_probability=0.6
    )

    with sessions() as session:
        event = session.scalar(
            select(ConvictionEvent).where(
                ConvictionEvent.thesis_analysis_id == thesis_id
            )
        )
        assert event.market_context_seen is False


def test_prior_context_exposure_is_monotonic_before_reveal():
    sessions = _sessions()
    tools = _tools(sessions)
    principal = _principal(sessions)
    thesis_id = tools.normalize_claim(principal, input_text=CLEAN).thesis_analysis_id

    created = tools.submit_blind_prior(
        principal, thesis_analysis_id=thesis_id, prior_probability=0.6
    )
    assert created.status == "created"
    tools.preview_market_fit(principal, thesis_analysis_id=thesis_id)
    updated = tools.submit_blind_prior(
        principal, thesis_analysis_id=thesis_id, prior_probability=0.65
    )
    assert updated.status == "updated"

    with sessions() as session:
        event = session.scalar(
            select(ConvictionEvent).where(
                ConvictionEvent.thesis_analysis_id == thesis_id
            )
        )
        assert event.market_context_seen is True


# --- 4. submit prior -> classify reveals thesis-side odds ----------------
def test_classify_reveals_thesis_side_odds_after_prior():
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    tid = tools.normalize_claim(p, input_text=CLEAN).thesis_analysis_id
    tools.submit_blind_prior(p, thesis_analysis_id=tid, prior_probability=0.6)
    card = tools.classify_market_fit(p, thesis_analysis_id=tid)
    assert card.current_odds is not None
    assert card.odds_side in ("yes", "no")


# --- 5. write-once holds at the MCP boundary ----------------------------
def test_prior_write_once_at_mcp_boundary():
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    tid = tools.normalize_claim(p, input_text=CLEAN).thesis_analysis_id
    tools.submit_blind_prior(p, thesis_analysis_id=tid, prior_probability=0.6)
    tools.classify_market_fit(p, thesis_analysis_id=tid)  # reveals -> stamps
    again = tools.submit_blind_prior(p, thesis_analysis_id=tid, prior_probability=0.99)
    assert again.status == "locked"


# --- 6. cross-actor odds isolation --------------------------------------
def test_cross_actor_odds_isolation():
    sessions = _sessions()
    tools = _tools(sessions)
    a = _principal(sessions)
    b = _principal(sessions)
    tid = tools.normalize_claim(a, input_text=CLEAN).thesis_analysis_id
    tools.submit_blind_prior(a, thesis_analysis_id=tid, prior_probability=0.7)
    # B cannot reach A's thesis; the boundary denies before blind-prior state.
    with pytest.raises(NotFound):
        tools.classify_market_fit(b, thesis_analysis_id=tid)


# --- 7. cross-user/object access denied ---------------------------------
def test_cross_user_access_denied_by_guessed_id():
    sessions = _sessions()
    tools = _tools(sessions)
    a = _principal(sessions)
    b = _principal(sessions)
    tid = tools.normalize_claim(a, input_text=CLEAN).thesis_analysis_id
    tools.submit_blind_prior(a, thesis_analysis_id=tid, prior_probability=0.7)
    card = tools.classify_market_fit(a, thesis_analysis_id=tid)
    saved = tools.create_ledger_entry(
        a,
        fit_card_id=card.fit_card_id,
        conviction_level="leaning",
        intended_exposure_bucket="$50",
        user_justification="A's thesis.",
    )
    # B cannot read A's entry by id, nor a random guessed id.
    with pytest.raises(NotFound):
        tools.get_ledger_entry(b, ledger_entry_id=saved.id)
    with pytest.raises(NotFound):
        tools.get_ledger_entry(b, ledger_entry_id=uuid.uuid4())
    # B's own ledger is empty.
    assert tools.get_ledger_entries(b) == []
    # correct/reject on a guessed fit_card id -> NotFound.
    with pytest.raises(NotFound):
        tools.correct_fit(b, fit_card_id=uuid.uuid4(), corrected_class="indirect")


def test_object_authorization_blocks_b_on_as_real_objects():
    # P0: B, knowing A's REAL thesis/card ids, is denied every side-effecting
    # op on A's objects — NotFound, no existence leak.
    sessions = _sessions()
    tools = _tools(sessions)
    a = _principal(sessions)
    b = _principal(sessions)
    tid = tools.normalize_claim(a, input_text=CLEAN).thesis_analysis_id
    tools.submit_blind_prior(a, thesis_analysis_id=tid, prior_probability=0.7)
    a_card = tools.classify_market_fit(a, thesis_analysis_id=tid).fit_card_id

    # thesis-scoped: B cannot preview, submit a prior, or classify A's thesis.
    with pytest.raises(NotFound):
        tools.preview_market_fit(b, thesis_analysis_id=tid)
    with pytest.raises(NotFound):
        tools.submit_blind_prior(b, thesis_analysis_id=tid, prior_probability=0.5)
    with pytest.raises(NotFound):
        tools.classify_market_fit(b, thesis_analysis_id=tid)
    # card-scoped: B cannot draft, save, correct, or reject A's fit card.
    with pytest.raises(NotFound):
        tools.draft_contract_preview(b, fit_card_id=a_card)
    with pytest.raises(NotFound):
        tools.create_ledger_entry(
            b,
            fit_card_id=a_card,
            conviction_level="leaning",
            intended_exposure_bucket="$50",
            user_justification="not mine",
        )
    with pytest.raises(NotFound):
        tools.correct_fit(b, fit_card_id=a_card, corrected_class="indirect")
    with pytest.raises(NotFound):
        tools.reject_market(b, fit_card_id=a_card, market_id="mkt_x", reason="no")
    # B's ledger stays empty; A still owns its work.
    assert tools.get_ledger_entries(b) == []


# --- 8. no-clean classify/reveal returns no odds gracefully -------------
def test_no_clean_classify_returns_no_odds():
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    tid = _seed_no_clean_thesis(sessions, p)
    tools.submit_blind_prior(p, thesis_analysis_id=tid, prior_probability=0.5)
    card = tools.classify_market_fit(p, thesis_analysis_id=tid)
    assert card.semantic_fit_class == "no_clean_expression"
    assert card.current_odds is None  # no market, no odds — no crash


# --- 9 + 10. agent save unattested; plain save no review_candidate ------
def test_save_is_unattested_and_creates_no_review_candidate():
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    tid = tools.normalize_claim(p, input_text=CLEAN).thesis_analysis_id
    tools.submit_blind_prior(p, thesis_analysis_id=tid, prior_probability=0.6)
    card = tools.classify_market_fit(p, thesis_analysis_id=tid)
    saved = tools.create_ledger_entry(
        p,
        fit_card_id=card.fit_card_id,
        conviction_level="conviction",
        intended_exposure_bucket="$250",
        user_justification="Saving.",
    )
    assert saved.attestation_status == "unattested"
    with sessions() as s:
        assert s.scalars(select(ReviewCandidate)).all() == []


# --- 11. correct_fit / reject_market create review_candidate ------------
def test_corrections_create_review_candidates():
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    tid = tools.normalize_claim(p, input_text=CLEAN).thesis_analysis_id
    tools.submit_blind_prior(p, thesis_analysis_id=tid, prior_probability=0.6)
    card = tools.classify_market_fit(p, thesis_analysis_id=tid)
    corr = tools.correct_fit(
        p, fit_card_id=card.fit_card_id, corrected_class="indirect", notes="too strong"
    )
    rej = tools.reject_market(
        p, fit_card_id=card.fit_card_id, market_id="mkt_x", reason="weak proxy"
    )
    assert corr.source == "agent_correction" and corr.status == "pending"
    assert rej.source == "agent_rejection"
    with sessions() as s:
        rows = s.scalars(select(ReviewCandidate)).all()
        assert len(rows) == 2


def test_correct_reject_are_idempotent():
    # P1: repeating the same correction/rejection returns the existing
    # candidate (no duplicate); a different payload creates a new one.
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    tid = tools.normalize_claim(p, input_text=CLEAN).thesis_analysis_id
    tools.submit_blind_prior(p, thesis_analysis_id=tid, prior_probability=0.6)
    card = tools.classify_market_fit(p, thesis_analysis_id=tid)
    a1 = tools.correct_fit(
        p, fit_card_id=card.fit_card_id, corrected_class="indirect", notes="too strong"
    )
    a2 = tools.correct_fit(
        p, fit_card_id=card.fit_card_id, corrected_class="indirect", notes="too strong"
    )
    assert a2.review_candidate_id == a1.review_candidate_id  # idempotent
    tools.reject_market(p, fit_card_id=card.fit_card_id, market_id="mkt_x", reason="weak")
    tools.reject_market(p, fit_card_id=card.fit_card_id, market_id="mkt_x", reason="weak")
    # A different correction payload IS a distinct candidate.
    tools.correct_fit(
        p, fit_card_id=card.fit_card_id, corrected_class="weak_proxy", notes="weaker"
    )
    with sessions() as s:
        # 1 correction + 1 rejection + 1 different correction = 3 (no dups).
        assert len(s.scalars(select(ReviewCandidate)).all()) == 3


def test_demo_fuel_invariant_correction_and_isolation():
    # The "demo fuel" invariant in ONE flow: a correction produces Step-8 fuel
    # (review_candidates 0 -> >=1, via BOTH raw rows and artifact_stats) WHILE
    # the ledger save still works, and a second principal cannot correct the
    # first's real fit card. The individual assertions overlap existing tests
    # by design (count: test_corrections_create_review_candidates; isolation:
    # test_object_authorization_blocks_b_on_as_real_objects) — this locks the
    # demo client's exact narrative as a single regression locus.
    sessions = _sessions()
    tools = _tools(sessions)
    a = _principal(sessions)
    b = _principal(sessions)

    # Baseline: zero fuel, both views agree.
    assert tools.artifact_stats()["review_candidates"] == 0
    with sessions() as s:
        assert s.scalars(select(ReviewCandidate)).all() == []

    tid = tools.normalize_claim(a, input_text=CLEAN).thesis_analysis_id
    tools.submit_blind_prior(a, thesis_analysis_id=tid, prior_probability=0.7)
    card = tools.classify_market_fit(a, thesis_analysis_id=tid)
    saved = tools.create_ledger_entry(
        a,
        fit_card_id=card.fit_card_id,
        conviction_level="leaning",
        intended_exposure_bucket="$100",
        user_justification="Strong leaderboard momentum.",
    )
    # Ledger save still works alongside the correction.
    assert tools.get_ledger_entry(a, ledger_entry_id=saved.id).id == saved.id

    corr = tools.correct_fit(
        a,
        fit_card_id=card.fit_card_id,
        corrected_class="indirect",
        notes="[SYNTHETIC SMOKE] demo-generated; not user evidence",
    )
    assert corr.source == "agent_correction" and corr.status == "pending"

    # Fuel now exists: 0 -> >=1, agreed by raw rows and artifact_stats.
    with sessions() as s:
        assert len(s.scalars(select(ReviewCandidate)).all()) >= 1
    assert tools.artifact_stats()["review_candidates"] >= 1

    # Isolation: B cannot correct A's real fit card (NotFound, no leak).
    with pytest.raises(NotFound):
        tools.correct_fit(
            b, fit_card_id=card.fit_card_id, corrected_class="weak_proxy"
        )


def test_draft_preview_labels_shape_valid_candidate():
    # P2c: a generated draft is explicitly a "shape-valid draft candidate".
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    tid = _seed_no_clean_thesis(sessions, p)
    tools.submit_blind_prior(p, thesis_analysis_id=tid, prior_probability=0.5)
    fc = tools.classify_market_fit(p, thesis_analysis_id=tid).fit_card_id
    draft = tools.draft_contract_preview(p, fit_card_id=fc)
    assert draft.generated is True
    assert draft.label == "shape-valid draft candidate"


@pytest.mark.parametrize(
    "text",
    [
        "Acme is going to sell its cloud unit by the end of 2026.",
        "Acme will buy a competitor in 2026.",
    ],
)
def test_ma_claims_normalize_a7_clean(text):
    # P2: "sell unit" / "buy competitor" must normalize WITHOUT tripping A7 —
    # generated copy rewrites to divest/acquire (the raw input is exempt source).
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    norm = tools.normalize_claim(p, input_text=text)  # no ToolRefused / A7Violation
    summary = norm.normalized_claim_summary.lower()
    assert "divest" in summary or "acquir" in summary


def test_artifact_stats_tracks_preview_and_classify_creation():
    # P2: preview/classify each persist a candidate_set + fit_card (no ephemeral
    # path); artifact_stats surfaces the accumulation for monitoring.
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    tid = tools.normalize_claim(p, input_text=CLEAN).thesis_analysis_id
    base = tools.artifact_stats()
    tools.preview_market_fit(p, thesis_analysis_id=tid)
    tools.submit_blind_prior(p, thesis_analysis_id=tid, prior_probability=0.6)
    tools.classify_market_fit(p, thesis_analysis_id=tid)
    after = tools.artifact_stats()
    assert after["candidate_sets"] >= base["candidate_sets"] + 2
    assert after["fit_cards"] >= base["fit_cards"] + 2


# --- 12. A7 field-aware: blocks generated advice, allows source ---------
def test_a7_blocks_generated_advice_in_checked_field():
    # what_it_captures is a CHECKED system field (not in SOURCE_PATHS).
    bad = {
        "fit_card_id": str(uuid.uuid4()),
        "thesis_analysis_id": str(uuid.uuid4()),
        "candidate_set_id": str(uuid.uuid4()),
        "semantic_fit_class": "direct",
        "recommended_market_id": "mkt_x",
        "current_odds": 0.4,
        "odds_side": "yes",
        "what_it_captures": "honestly you should buy this market now",
        "what_it_misses": "",
        "horizon_match": "good",
        "resolution_risk": "low",
        "fit_confidence": None,
        "draft_contract_recommended": False,
        "rejected_markets": [],
        "provenance": {},
    }
    with pytest.raises(A7Violation):
        assert_a7_clean(bad, source_paths=FitCardResult.SOURCE_PATHS)


def test_a7_allows_source_text_with_buy_sell_sale():
    # input_text + entity names are SOURCE (exempt); buy/sell/sale pass there.
    payload = {
        "thesis_analysis_id": str(uuid.uuid4()),
        "normalized_claim_summary": "Acme divests its cloud unit by 2026.",
        "extracted_structure": {"entities": [{"name": "Sell-Side Capital"}]},
        "input_text": "Will Acme sell its cloud unit? Should I buy or sell?",
    }
    assert_a7_clean(payload, source_paths=NormalizeResult.SOURCE_PATHS)  # no raise


# --- 13. idempotency prevents duplicate mutation ------------------------
def test_create_ledger_entry_idempotent():
    sessions = _sessions()
    tools = _tools(sessions)
    p = _principal(sessions)
    tid = tools.normalize_claim(p, input_text=CLEAN).thesis_analysis_id
    tools.submit_blind_prior(p, thesis_analysis_id=tid, prior_probability=0.6)
    card = tools.classify_market_fit(p, thesis_analysis_id=tid)
    first = tools.create_ledger_entry(
        p,
        fit_card_id=card.fit_card_id,
        conviction_level="leaning",
        intended_exposure_bucket="$100",
        user_justification="once",
    )
    second = tools.create_ledger_entry(
        p,
        fit_card_id=card.fit_card_id,
        conviction_level="leaning",
        intended_exposure_bucket="$100",
        user_justification="twice",
    )
    assert first.id == second.id  # idempotent per (user, thesis)
    entries = tools.get_ledger_entries(p)
    assert len(entries) == 1
