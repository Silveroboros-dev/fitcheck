"""Step 6 — LedgerService: blind-prior odds lock, conviction capture, strict
save. Fit cards are seeded directly so each test controls thesis_side and the
frozen candidate-member price exactly.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from el.domain.enums import (
    ClientType,
    ConvictionLevel,
    ExposureBucket,
    PriorConfidence,
)
from el.domain.tables import (
    Base,
    CandidateSet,
    CandidateSetMember,
    ConvictionEvent,
    DraftContract,
    FitCard,
    LedgerEntry,
    MarketSnapshot,
    ThesisAnalysis,
    User,
)
from el.domain.vocabulary import vocabulary_violations
from el.ledger.odds import (
    ODDS_SIDE_NO,
    ODDS_SIDE_UNKNOWN,
    ODDS_SIDE_YES,
    orient_odds,
)
from el.ledger.service import LedgerService

SUMMARY = "Acme reports a year-over-year revenue decline by end of 2026."


def _session_factory():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed(
    sessions,
    *,
    thesis_side="yes",
    current_probability=0.4,
    recommended="mkt_rev_decline",
    fit_class="direct",
    with_draft=False,
):
    """Insert user + snapshot + analysis + candidate set/member + fit card,
    with full control over thesis_side and the frozen member price."""
    with sessions() as s:
        user = User(email=f"u-{uuid.uuid4()}@example.com")
        s.add(user)
        snap = MarketSnapshot(
            id=f"snap-{uuid.uuid4()}",
            venue_id="Polymarket",
            as_of_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            retrieval_id="r1",
        )
        s.add(snap)
        analysis = ThesisAnalysis(
            input_text="Acme revenue is going to shrink.",
            extracted_structure={},
            normalized_claim_summary=SUMMARY,
            client_type="human_ui",
        )
        s.add(analysis)
        s.flush()
        cset = CandidateSet(thesis_analysis_id=analysis.id, snapshot_id=snap.id)
        s.add(cset)
        s.flush()
        if recommended is not None:
            s.add(
                CandidateSetMember(
                    candidate_set_id=cset.id,
                    market_id=recommended,
                    rank=1,
                    retrieval_score=1.0,
                    current_probability=current_probability,
                    eligibility_flags={},
                )
            )
        draft_id = None
        if with_draft:
            draft = DraftContract(
                thesis_analysis_id=analysis.id,
                proposed_title="Will Acme report a YoY revenue decline by 2026-12-31?",
                proposed_resolution_logic="Resolves YES on a reported YoY decline.",
                resolution_source="company filings",
                provenance={},
            )
            s.add(draft)
            s.flush()
            draft_id = draft.id
        card = FitCard(
            thesis_analysis_id=analysis.id,
            candidate_set_id=cset.id,
            semantic_fit_class=fit_class,
            recommended_market_id=recommended,
            what_it_captures="captures",
            what_it_misses="misses",
            horizon_match="good",
            resolution_risk="low",
            fit_confidence=None,
            draft_contract_id=draft_id,
            provenance={"thesis_side": thesis_side},
        )
        s.add(card)
        s.flush()
        result = {
            "user_id": user.id,
            "thesis_analysis_id": analysis.id,
            "candidate_set_id": cset.id,
            "fit_card_id": card.id,
            "draft_id": draft_id,
        }
        s.commit()
        return result


# --- pure orient_odds (scenarios 1-4 math) -----------------------------


def test_orient_odds_yes_side():
    assert orient_odds(0.4, "yes") == (0.4, ODDS_SIDE_YES)


def test_orient_odds_no_side_is_one_minus_p():
    assert orient_odds(0.4, "no") == (pytest.approx(0.6), ODDS_SIDE_NO)


def test_orient_odds_unknown_is_null_never_raw_yes():
    assert orient_odds(0.4, "unknown") == (None, ODDS_SIDE_UNKNOWN)
    assert orient_odds(0.4, None) == (None, ODDS_SIDE_UNKNOWN)


def test_orient_odds_missing_price_is_null_side_kept():
    assert orient_odds(None, "yes") == (None, ODDS_SIDE_YES)
    assert orient_odds(None, "no") == (None, ODDS_SIDE_NO)


# --- human save ---------------------------------------------------------


def test_human_save_attested_yes_side_odds():
    sessions = _session_factory()
    ids = _seed(sessions, thesis_side="yes", current_probability=0.4)
    svc = LedgerService(sessions)
    out = svc.create_ledger_entry(
        ids["fit_card_id"],
        user_id=ids["user_id"],
        client_type=ClientType.HUMAN_UI,
        actor_id=str(ids["user_id"]),
        client_ref="session-1",
        conviction_level=ConvictionLevel.LEANING,
        intended_exposure_bucket=ExposureBucket.USD_100,
        user_justification="Channel checks suggest a demand air-pocket.",
    )
    assert out.saved and out.status == "saved"
    assert out.odds_at_entry == 0.4 and out.odds_at_entry_side == "yes"
    assert out.attestation_status == "attested"
    entry = svc.get_ledger_entry(out.ledger_entry_id)
    assert entry.linked_market_id == "mkt_rev_decline"
    assert entry.odds_at_entry == 0.4 and entry.odds_at_entry_side == "yes"


def test_no_side_thesis_odds_is_one_minus_p():
    sessions = _session_factory()
    ids = _seed(sessions, thesis_side="no", current_probability=0.4)
    svc = LedgerService(sessions)
    out = svc.create_ledger_entry(
        ids["fit_card_id"],
        user_id=ids["user_id"],
        client_type=ClientType.HUMAN_UI,
        actor_id=str(ids["user_id"]),
        client_ref="s1",
        conviction_level=ConvictionLevel.CONVICTION,
        intended_exposure_bucket=ExposureBucket.USD_50,
        user_justification="The market's YES is the opposite of my thesis.",
    )
    assert out.odds_at_entry == pytest.approx(0.6)
    assert out.odds_at_entry_side == "no"


def test_side_unknown_odds_is_null():
    sessions = _session_factory()
    ids = _seed(sessions, thesis_side="unknown", current_probability=0.4)
    svc = LedgerService(sessions)
    out = svc.create_ledger_entry(
        ids["fit_card_id"],
        user_id=ids["user_id"],
        client_type=ClientType.HUMAN_UI,
        actor_id=str(ids["user_id"]),
        client_ref="s1",
        conviction_level=ConvictionLevel.EXPLORING,
        intended_exposure_bucket=ExposureBucket.USD_10,
        user_justification="Direction is ambiguous on this market.",
    )
    assert out.odds_at_entry is None and out.odds_at_entry_side == "side_unknown"


def test_missing_price_odds_is_null():
    sessions = _session_factory()
    ids = _seed(sessions, thesis_side="yes", current_probability=None)
    svc = LedgerService(sessions)
    out = svc.create_ledger_entry(
        ids["fit_card_id"],
        user_id=ids["user_id"],
        client_type=ClientType.HUMAN_UI,
        actor_id=str(ids["user_id"]),
        client_ref="s1",
        conviction_level=ConvictionLevel.LEANING,
        intended_exposure_bucket=ExposureBucket.USD_25,
        user_justification="No live price on this market yet.",
    )
    assert out.odds_at_entry is None and out.odds_at_entry_side == "yes"


# --- agent surface: blind-prior protocol --------------------------------


def test_agent_save_requires_blind_prior_lock():
    sessions = _session_factory()
    ids = _seed(sessions)
    svc = LedgerService(sessions)
    rejected = svc.create_ledger_entry(
        ids["fit_card_id"],
        user_id=ids["user_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-key-1",
        client_ref="run-1",
        conviction_level=ConvictionLevel.LEANING,
        intended_exposure_bucket=ExposureBucket.USD_100,
        user_justification="Agent thesis.",
        agent_client_id="agent-key-1",
    )
    assert not rejected.saved and rejected.status == "rejected_incomplete"
    assert any("blind prior" in v for v in rejected.violations)

    svc.submit_blind_prior(
        ids["thesis_analysis_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-key-1",
        client_ref="run-1",
        prior_probability=0.55,
        agent_client_id="agent-key-1",
    )
    saved = svc.create_ledger_entry(
        ids["fit_card_id"],
        user_id=ids["user_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-key-1",
        client_ref="run-1",
        conviction_level=ConvictionLevel.LEANING,
        intended_exposure_bucket=ExposureBucket.USD_100,
        user_justification="Agent thesis.",
        agent_client_id="agent-key-1",
    )
    assert saved.saved and saved.attestation_status == "unattested"


def test_reveal_requires_lock_then_stamps_odds_revealed_at():
    sessions = _session_factory()
    ids = _seed(sessions, thesis_side="yes", current_probability=0.4)
    svc = LedgerService(sessions)
    # Agent surface: withheld before a blind prior.
    withheld = svc.reveal_current_odds(
        ids["fit_card_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-1",
        client_ref="run-1",
    )
    assert not withheld.revealed and withheld.current_odds is None

    bp = svc.submit_blind_prior(
        ids["thesis_analysis_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-1",
        client_ref="run-1",
        prior_probability=0.7,
        agent_client_id="agent-1",
    )
    revealed = svc.reveal_current_odds(
        ids["fit_card_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-1",
        client_ref="run-1",
    )
    assert revealed.revealed and revealed.current_odds == 0.4  # yes-side
    assert revealed.odds_revealed_at is not None
    with sessions() as s:
        event = s.get(ConvictionEvent, bp.conviction_event_id)
        assert event.odds_revealed_at is not None  # stamped on the blind event


def test_prior_cannot_be_overwritten_after_reveal():
    sessions = _session_factory()
    ids = _seed(sessions)
    svc = LedgerService(sessions)
    svc.submit_blind_prior(
        ids["thesis_analysis_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-1",
        client_ref="run-1",
        prior_probability=0.6,
        agent_client_id="agent-1",
    )
    svc.reveal_current_odds(
        ids["fit_card_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-1",
        client_ref="run-1",
    )
    locked = svc.submit_blind_prior(
        ids["thesis_analysis_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-1",
        client_ref="run-1",
        prior_probability=0.99,  # attempt to move it after seeing odds
        agent_client_id="agent-1",
    )
    assert locked.status == "locked"
    with sessions() as s:
        event = s.get(ConvictionEvent, locked.conviction_event_id)
        assert event.prior_probability == 0.6  # unchanged


def test_prior_frozen_after_ledger_save_without_reveal():
    # The original bug: save (which yields odds_at_entry) without an explicit
    # reveal left odds_revealed_at NULL, so a late submit could overwrite the
    # prior. The save must freeze it write-once.
    sessions = _session_factory()
    ids = _seed(sessions, thesis_side="yes", current_probability=0.4)
    svc = LedgerService(sessions)
    bp = svc.submit_blind_prior(
        ids["thesis_analysis_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-1",
        client_ref="run-1",
        prior_probability=0.6,
        agent_client_id="agent-1",
    )
    saved = svc.create_ledger_entry(
        ids["fit_card_id"],
        user_id=ids["user_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-1",
        client_ref="run-1",
        conviction_level=ConvictionLevel.LEANING,
        intended_exposure_bucket=ExposureBucket.USD_100,
        user_justification="Agent thesis, saved without an explicit reveal.",
        agent_client_id="agent-1",
    )
    assert saved.saved and saved.odds_at_entry == 0.4  # odds received via save

    locked = svc.submit_blind_prior(
        ids["thesis_analysis_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-1",
        client_ref="run-1",
        prior_probability=0.99,  # attempt to move it after saving
        agent_client_id="agent-1",
    )
    assert locked.status == "locked"
    with sessions() as s:
        ev = s.get(ConvictionEvent, bp.conviction_event_id)
        assert ev.prior_probability == 0.6  # unchanged
        assert ev.odds_revealed_at is not None  # stamped at save
        assert ev.ledger_entry_id == saved.ledger_entry_id


def test_no_clean_reveal_does_not_crash():
    # Revealing on a no-clean card has no market/side; it must return a clean
    # withheld outcome, never crash Pydantic on side=None.
    sessions = _session_factory()
    ids = _seed(
        sessions,
        recommended=None,
        fit_class="no_clean_expression",
        thesis_side="unknown",
    )
    svc = LedgerService(sessions)
    out = svc.reveal_current_odds(
        ids["fit_card_id"],
        client_type=ClientType.HUMAN_UI,
        actor_id=str(ids["user_id"]),
        client_ref="s1",
    )
    assert out.revealed is False
    assert out.current_odds is None
    assert out.side == "side_unknown"
    assert "no linked market" in out.reason


def test_lock_scoped_by_authenticated_actor():
    sessions = _session_factory()
    ids = _seed(sessions, thesis_side="yes", current_probability=0.4)
    svc = LedgerService(sessions)
    svc.submit_blind_prior(
        ids["thesis_analysis_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-A",
        client_ref="shared-ref",
        prior_probability=0.6,
        agent_client_id="agent-A",
    )
    # Same client_ref, DIFFERENT authenticated actor -> no lock, odds withheld.
    other = svc.reveal_current_odds(
        ids["fit_card_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-B",
        client_ref="shared-ref",
    )
    assert not other.revealed
    mine = svc.reveal_current_odds(
        ids["fit_card_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-A",
        client_ref="shared-ref",
    )
    assert mine.revealed


# --- actor-level lock: client_ref cannot bypass the freeze ---------------


def _blind(svc, ids, *, actor_id, client_ref, p):
    return svc.submit_blind_prior(
        ids["thesis_analysis_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id=actor_id,
        client_ref=client_ref,
        prior_probability=p,
        agent_client_id=actor_id,
    )


def _agent_save(svc, ids, *, actor_id, client_ref):
    return svc.create_ledger_entry(
        ids["fit_card_id"],
        user_id=ids["user_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id=actor_id,
        client_ref=client_ref,
        conviction_level=ConvictionLevel.LEANING,
        intended_exposure_bucket=ExposureBucket.USD_100,
        user_justification="Agent thesis.",
        agent_client_id=actor_id,
    )


def test_new_client_ref_after_reveal_cannot_bypass_freeze():
    sessions = _session_factory()
    ids = _seed(sessions, thesis_side="yes", current_probability=0.4)
    svc = LedgerService(sessions)
    bp = _blind(svc, ids, actor_id="agent-1", client_ref="run-1", p=0.6)
    svc.reveal_current_odds(
        ids["fit_card_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-1",
        client_ref="run-1",
    )
    # The bypass attempt: same actor, NEW client_ref, post-reveal.
    bypass = _blind(svc, ids, actor_id="agent-1", client_ref="run-2", p=0.99)
    assert bypass.status == "locked"
    assert bypass.conviction_event_id == bp.conviction_event_id
    with sessions() as s:
        evs = s.scalars(
            select(ConvictionEvent).where(
                ConvictionEvent.thesis_analysis_id == ids["thesis_analysis_id"],
                ConvictionEvent.prior_type == "blind",
            )
        ).all()
        assert len(evs) == 1  # no duplicate blind event for the actor
        assert evs[0].prior_probability == 0.6  # unchanged


def test_new_client_ref_after_save_cannot_bypass_freeze():
    sessions = _session_factory()
    ids = _seed(sessions, thesis_side="yes", current_probability=0.4)
    svc = LedgerService(sessions)
    bp = _blind(svc, ids, actor_id="agent-1", client_ref="run-1", p=0.6)
    _agent_save(svc, ids, actor_id="agent-1", client_ref="run-1")
    bypass = _blind(svc, ids, actor_id="agent-1", client_ref="run-2", p=0.99)
    assert bypass.status == "locked"
    with sessions() as s:
        evs = s.scalars(
            select(ConvictionEvent).where(
                ConvictionEvent.thesis_analysis_id == ids["thesis_analysis_id"],
                ConvictionEvent.prior_type == "blind",
            )
        ).all()
        assert len(evs) == 1 and evs[0].prior_probability == 0.6


def test_save_with_different_client_ref_uses_actor_level_prior():
    # A save under a different client_ref still finds the actor-level lock —
    # it does not require (or mint) a new prior.
    sessions = _session_factory()
    ids = _seed(sessions, thesis_side="yes", current_probability=0.4)
    svc = LedgerService(sessions)
    bp = _blind(svc, ids, actor_id="agent-1", client_ref="run-1", p=0.6)
    saved = _agent_save(svc, ids, actor_id="agent-1", client_ref="run-2")
    assert saved.saved
    with sessions() as s:
        blind = s.get(ConvictionEvent, bp.conviction_event_id)
        assert blind.ledger_entry_id == saved.ledger_entry_id  # original prior


def test_different_actor_stays_independent_and_withheld():
    sessions = _session_factory()
    ids = _seed(sessions, thesis_side="yes", current_probability=0.4)
    svc = LedgerService(sessions)
    _blind(svc, ids, actor_id="agent-A", client_ref="r1", p=0.6)
    # Agent-B has no prior of its own -> still withheld.
    assert not svc.reveal_current_odds(
        ids["fit_card_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-B",
        client_ref="r1",
    ).revealed
    # Agent-B submits its OWN prior independently and then sees odds.
    assert _blind(svc, ids, actor_id="agent-B", client_ref="r2", p=0.7).status == (
        "created"
    )
    assert svc.reveal_current_odds(
        ids["fit_card_id"],
        client_type=ClientType.AGENT_MCP,
        actor_id="agent-B",
        client_ref="r2",
    ).revealed


# --- no-clean, idempotency, vocabulary ----------------------------------


def test_no_clean_save_has_no_market_and_no_odds():
    sessions = _session_factory()
    ids = _seed(
        sessions,
        thesis_side="unknown",
        recommended=None,
        fit_class="no_clean_expression",
        with_draft=True,
    )
    svc = LedgerService(sessions)
    out = svc.create_ledger_entry(
        ids["fit_card_id"],
        user_id=ids["user_id"],
        client_type=ClientType.HUMAN_UI,
        actor_id=str(ids["user_id"]),
        client_ref="s1",
        conviction_level=ConvictionLevel.EXPLORING,
        intended_exposure_bucket=ExposureBucket.USD_10,
        user_justification="No clean market exists; saving the thesis + draft.",
    )
    assert out.saved
    entry = svc.get_ledger_entry(out.ledger_entry_id)
    assert entry.linked_market_id is None
    assert entry.odds_at_entry is None and entry.odds_at_entry_side is None
    # The draft contract is back-linked to the saved entry.
    with sessions() as s:
        draft = s.get(DraftContract, ids["draft_id"])
        assert draft.ledger_entry_id == out.ledger_entry_id


def test_idempotent_per_user_and_thesis():
    sessions = _session_factory()
    ids = _seed(sessions)
    svc = LedgerService(sessions)
    kwargs = dict(
        user_id=ids["user_id"],
        client_type=ClientType.HUMAN_UI,
        actor_id=str(ids["user_id"]),
        client_ref="s1",
        conviction_level=ConvictionLevel.LEANING,
        intended_exposure_bucket=ExposureBucket.USD_100,
        user_justification="First save.",
    )
    first = svc.create_ledger_entry(ids["fit_card_id"], **kwargs)
    second = svc.create_ledger_entry(ids["fit_card_id"], **kwargs)
    assert first.saved and not second.saved
    assert second.status == "already_saved"
    assert second.ledger_entry_id == first.ledger_entry_id
    with sessions() as s:
        rows = s.scalars(select(LedgerEntry)).all()
        assert len(rows) == 1


def test_ledger_unique_constraint_blocks_duplicate_per_user_thesis():
    # The DB constraint that backs the idempotency above under concurrency: a
    # second row for the same (user, thesis) is rejected on a direct insert
    # that bypasses the service's check-then-insert (the racing-writer case).
    sessions = _session_factory()
    ids = _seed(sessions)

    def _row():
        return LedgerEntry(
            user_id=ids["user_id"],
            thesis_analysis_id=ids["thesis_analysis_id"],
            thesis_summary=SUMMARY,
            user_justification="direct insert",
            fit_class="direct",
            fit_card_id=ids["fit_card_id"],
            client_type="human_ui",
        )

    with sessions() as s:
        s.add(_row())
        s.commit()
    with sessions() as s:
        s.add(_row())
        with pytest.raises(IntegrityError):
            s.commit()


def test_vocabulary_boundary_no_trading_language():
    sessions = _session_factory()
    ids = _seed(sessions)
    svc = LedgerService(sessions)
    out = svc.create_ledger_entry(
        ids["fit_card_id"],
        user_id=ids["user_id"],
        client_type=ClientType.HUMAN_UI,
        actor_id=str(ids["user_id"]),
        client_ref="s1",
        conviction_level=ConvictionLevel.LEANING,
        intended_exposure_bucket=ExposureBucket.USD_100,
        user_justification="Demand looks soft into year end.",
    )
    entry = svc.get_ledger_entry(out.ledger_entry_id)
    # System-controlled fields carry no restricted (trading/advice) vocabulary.
    system_text = " ".join(
        [
            entry.thesis_summary,
            entry.fit_class.value,
            entry.attestation_status.value,
            entry.status.value,
            entry.odds_at_entry_side or "",
        ]
    )
    assert vocabulary_violations(system_text) == []
    # Exposure is framed as hypothetical risk (a $ amount), never an order.
    assert out is not None  # save succeeded with $-bucket framing
    with sessions() as s:
        ev = s.scalars(
            select(ConvictionEvent).where(
                ConvictionEvent.ledger_entry_id == out.ledger_entry_id
            )
        ).first()
        assert ev.intended_exposure_bucket == "$100"
