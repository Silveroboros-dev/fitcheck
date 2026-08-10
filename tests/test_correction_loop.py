"""Correction-loop UI contract tests (docs/correction-loop-ui-contract-v0.md).

CC-1..CC-6. CC-7 (rendered browser evidence) is executed against the running
app and documented in the branch handoff, per the contract.
"""

import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from el.domain.tables import (
    Base,
    FitCard,
    LedgerEntry,
    MarketRecommendation,
    RejectedMarketRow,
    ReviewCandidate,
)
from el.product.api import ProductApi
from el.product.app import build_app
from el.product.wiring import build_services, local_actor

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "correction-loop-ui-contract-v0.md"
PAGE = ROOT / "el" / "product" / "static" / "product_console.html"
CLEAN = (
    "Gemini is going to be ranked #1 chatbot on LMSYS Chatbot Arena by "
    "the end of 2026."
)


@pytest.fixture()
def app_ctx():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    services = build_services(mode="fixture", session_factory=sessions)
    client = TestClient(build_app(ProductApi(services, local_actor(sessions))))
    return client, sessions


def _card(client):
    thesis = client.post("/api/thesis", json={"input_text": CLEAN}).json()
    tid = thesis["thesis_analysis_id"]
    client.post(f"/api/thesis/{tid}/blind-prior", json={"prior_probability": 0.3})
    return client.post(f"/api/thesis/{tid}/classify").json()


def _truth_state(sessions):
    """Byte-comparable snapshot of everything corrections must never touch."""
    with sessions() as s:
        return {
            "fit_cards": [
                (str(r.id), r.semantic_fit_class, r.recommended_market_id,
                 r.fit_confidence, str(r.provenance))
                for r in s.scalars(select(FitCard).order_by(FitCard.id))
            ],
            "recommendations": [
                (str(r.id), str(r.thesis_analysis_id))
                for r in s.scalars(
                    select(MarketRecommendation).order_by(MarketRecommendation.id)
                )
            ],
            "rejected_rows": [
                (str(r.id), r.market_id, r.reason)
                for r in s.scalars(
                    select(RejectedMarketRow).order_by(RejectedMarketRow.id)
                )
            ],
            "ledger": [
                (str(r.id), r.fit_class, r.odds_at_entry)
                for r in s.scalars(select(LedgerEntry).order_by(LedgerEntry.id))
            ],
        }


# --- CC-1 ---------------------------------------------------------------
def test_contract_exists_with_required_sections():
    text = CONTRACT.read_text(encoding="utf-8")
    for section in (
        "## The invariant",
        "## Honesty rules",
        "## Forbidden actions",
        "## Failure / empty states",
        "## Acceptance criteria",
    ):
        assert section in text, f"contract missing: {section}"
    for cc in range(1, 8):
        assert f"CC-{cc}:" in text


# --- CC-2 / CC-4 ---------------------------------------------------------
def test_correction_mints_pending_candidate_and_mutates_nothing(app_ctx):
    client, sessions = app_ctx
    card = _card(client)
    before = _truth_state(sessions)

    r = client.post(
        f"/api/fit-card/{card['fit_card_id']}/correct",
        json={"corrected_class": "weak_proxy", "note": "metric identity is lexical only"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["already_recorded"] is False

    with sessions() as s:
        rows = s.scalars(select(ReviewCandidate)).all()
        assert len(rows) == 1
        assert rows[0].source == "user_correction"
        assert rows[0].status == "pending"
        assert "weak_proxy" in rows[0].reviewer_notes

    assert _truth_state(sessions) == before  # CC-4: byte-level unchanged
    # And the card read-back via classify artifacts is untouched:
    with sessions() as s:
        db_card = s.get(FitCard, uuid.UUID(card["fit_card_id"]))
        assert db_card.semantic_fit_class == card["semantic_fit_class"]


# --- CC-3 ----------------------------------------------------------------
def test_reject_surfaced_market_ok_unsurfaced_market_422(app_ctx):
    client, sessions = app_ctx
    card = _card(client)
    surfaced = card["rejected_markets"][0]["market_id"]

    ok = client.post(
        f"/api/fit-card/{card['fit_card_id']}/reject-market",
        json={"market_id": surfaced, "reason": "stage mismatch on close read"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "pending"
    with sessions() as s:
        row = s.scalars(select(ReviewCandidate)).one()
        assert row.source == "user_rejection"

    bad = client.post(
        f"/api/fit-card/{card['fit_card_id']}/reject-market",
        json={"market_id": "mkt_never_surfaced", "reason": "x"},
    )
    assert bad.status_code == 422
    assert "not part of this card" in bad.json()["detail"]


# --- CC-5 ----------------------------------------------------------------
def test_identical_resubmission_dedupes(app_ctx):
    client, sessions = app_ctx
    card = _card(client)
    payload = {"corrected_class": "indirect", "note": "same note"}
    first = client.post(
        f"/api/fit-card/{card['fit_card_id']}/correct", json=payload
    ).json()
    second = client.post(
        f"/api/fit-card/{card['fit_card_id']}/correct", json=payload
    ).json()
    assert second["already_recorded"] is True
    assert second["review_candidate_id"] == first["review_candidate_id"]
    with sessions() as s:
        assert len(s.scalars(select(ReviewCandidate)).all()) == 1


def test_validation_and_ownership(app_ctx):
    client, _ = app_ctx
    card = _card(client)
    assert client.post(
        f"/api/fit-card/{card['fit_card_id']}/correct",
        json={"corrected_class": "somewhere_between", "note": "x"},
    ).status_code == 422
    assert client.post(
        f"/api/fit-card/{card['fit_card_id']}/correct",
        json={"corrected_class": "direct", "note": "   "},
    ).status_code == 422
    assert client.post(
        f"/api/fit-card/{uuid.uuid4()}/correct",
        json={"corrected_class": "direct", "note": "x"},
    ).status_code == 404


# --- CC-6 (page-level) -----------------------------------------------------
def test_page_carries_honest_correction_copy():
    html = PAGE.read_text(encoding="utf-8")
    assert "Disagree with this judgment?" in html
    assert "does not express my thesis" in html
    assert html.count("this card is unchanged") >= 2  # both ack variants
    assert 'id="reject-form" hidden' in html
    # corrections are chain state: cleared on thesis change
    assert '$("correct-class").value = ""' in html
    assert '$("correction-ack").hidden = true' in html


def test_page_still_free_of_restricted_vocabulary():
    lowered = PAGE.read_text(encoding="utf-8").lower()
    for phrase in ("risk-free", "you should trade", "place a trade", "wallet"):
        assert phrase not in lowered
    for word in ("buy", "sell", "edge"):
        assert not re.search(rf"\b{word}\b", lowered), word
