"""Product-UI API tests (agent-guided-ui-contract-v0 AC-2..AC-7, AC-9).

Fixture mode over an in-memory DB: no live model calls, no credentials.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from el.domain.tables import Base
from el.product.api import ProductApi
from el.product.app import build_app
from el.product.wiring import build_services, local_actor

CLEAN = (
    "Gemini is going to be ranked #1 chatbot on LMSYS Chatbot Arena by "
    "the end of 2026."
)
# Raw fixture input text (user-quoted source; the normalized summary and all
# system copy stay A7-clean — "acquires", never trading vocabulary).
NO_CLEAN = "Acme will buy a competitor in 2026."


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    services = build_services(mode="fixture", session_factory=sessions)
    actor = local_actor(sessions)
    return TestClient(build_app(ProductApi(services, actor)))


def _run_to_card(client, text, prior=0.3):
    thesis_r = client.post("/api/thesis", json={"input_text": text})
    assert thesis_r.status_code == 200, thesis_r.text
    thesis = thesis_r.json()
    tid = thesis["thesis_analysis_id"]
    r = client.post(
        f"/api/thesis/{tid}/blind-prior", json={"prior_probability": prior}
    )
    assert r.status_code == 200, r.text
    card = client.post(f"/api/thesis/{tid}/classify")
    assert card.status_code == 200, card.text
    return thesis, card.json()


# --- AC-2: happy path -------------------------------------------------------
def test_happy_path_paste_to_saved_ledger_entry(client):
    thesis, card = _run_to_card(client, CLEAN)
    assert card["thesis_analysis_id"] == thesis["thesis_analysis_id"]
    assert card["semantic_fit_class"] in (
        "direct",
        "indirect",
        "weak_proxy",
        "no_clean_expression",
    )
    assert card["recommended_market_id"] is not None
    assert card["recommended_market"]["title"]
    assert card["current_odds"] is not None  # prior locked -> odds revealed

    saved = client.post(
        f"/api/fit-card/{card['fit_card_id']}/ledger",
        json={
            "conviction_level": "leaning",
            "intended_exposure_bucket": "$50",
            "user_justification": "fixture happy-path record",
        },
    )
    assert saved.status_code == 200, saved.text
    entry = saved.json()
    assert entry["odds_at_entry"] is not None
    assert entry["attestation_status"] == "attested"  # human_ui save
    assert entry["client_type"] == "human_ui"

    detail = client.get(f"/api/ledger/{entry['id']}")
    assert detail.status_code == 200
    assert detail.json()["thesis_analysis_id"] == thesis["thesis_analysis_id"]
    listing = client.get("/api/ledger").json()["entries"]
    assert [e["id"] for e in listing] == [entry["id"]]


# --- AC-3: candidate evidence separated from verdict -------------------------
def test_candidate_evidence_is_metadata_not_verdict(client):
    _, card = _run_to_card(client, CLEAN)
    ev = card["candidate_evidence"]
    assert ev["snapshot_id"]
    assert ev["candidate_count"] > 0
    # The verdict fields live outside candidate_evidence; evidence carries no
    # fit class of its own.
    assert "semantic_fit_class" not in ev


# --- AC-4: identity binding / stale prevention ------------------------------
def test_classify_binds_to_requesting_thesis(client):
    thesis_a, card_a = _run_to_card(client, CLEAN)
    thesis_b, card_b = _run_to_card(client, NO_CLEAN)
    assert card_b["thesis_analysis_id"] == thesis_b["thesis_analysis_id"]
    assert card_b["thesis_analysis_id"] != thesis_a["thesis_analysis_id"]
    assert card_a["fit_card_id"] != card_b["fit_card_id"]


def test_unknown_ids_are_not_found(client):
    rid = uuid.uuid4()
    assert client.post(f"/api/thesis/{rid}/classify").status_code == 404
    assert client.post(f"/api/fit-card/{rid}/draft-preview").status_code == 404
    assert client.get(f"/api/ledger/{rid}").status_code == 404


# --- blind-prior gate --------------------------------------------------------
def test_classify_without_prior_is_blocked_with_typed_error(client):
    thesis = client.post("/api/thesis", json={"input_text": CLEAN}).json()
    r = client.post(f"/api/thesis/{thesis['thesis_analysis_id']}/classify")
    assert r.status_code == 409
    assert r.json()["error"] == "blind_prior_required"


# --- AC-5: no-clean is first-class -------------------------------------------
def test_no_clean_path_with_draft_preview_and_save(client):
    _, card = _run_to_card(client, NO_CLEAN)
    assert card["semantic_fit_class"] == "no_clean_expression"
    assert card["recommended_market_id"] is None
    assert card["current_odds"] is None
    assert card["draft_contract_recommended"] is True

    draft = client.post(f"/api/fit-card/{card['fit_card_id']}/draft-preview")
    assert draft.status_code == 200
    body = draft.json()
    assert body["generated"] is True
    assert body["label"] == "shape-valid draft candidate"
    assert body["proposed_title"]
    assert body["resolution_deadline"] == "2026-12-31"

    saved = client.post(
        f"/api/fit-card/{card['fit_card_id']}/ledger",
        json={
            "conviction_level": "exploring",
            "intended_exposure_bucket": "$10",
            "user_justification": "no clean expression — recording the view anyway",
        },
    )
    assert saved.status_code == 200, saved.text
    entry = saved.json()
    assert entry["linked_market_id"] is None
    assert entry["odds_at_entry"] is None


# --- AC-6: strict save gate ---------------------------------------------------
def test_save_blocked_until_required_fields_present(client):
    _, card = _run_to_card(client, CLEAN)
    r = client.post(
        f"/api/fit-card/{card['fit_card_id']}/ledger",
        json={
            "conviction_level": "leaning",
            "intended_exposure_bucket": "$50",
            "user_justification": "   ",
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] in ("save_rejected", "invalid_argument")
    if body["error"] == "save_rejected":
        assert body["violations"]


# --- AC-7: explicit unavailable state -----------------------------------------
def test_fixture_mode_unknown_text_is_explicit_unavailable(client):
    r = client.post(
        "/api/thesis", json={"input_text": "Totally novel thesis nobody fixtured."}
    )
    assert r.status_code == 503
    assert r.json()["error"] == "service_unavailable"
    assert "fixture" in r.json()["detail"]


# --- AC-9: the surface cannot mutate verdicts or review/golden truth ----------
def test_route_surface_has_no_forbidden_mutations(client):
    paths = {route.path for route in client.app.routes if hasattr(route, "path")}
    expected = {
        "/",
        "/api/health",
        "/api/thesis",
        "/api/thesis/{thesis_analysis_id}/blind-prior",
        "/api/thesis/{thesis_analysis_id}/classify",
        "/api/fit-card/{fit_card_id}/draft-preview",
        "/api/fit-card/{fit_card_id}/ledger",
        # correction-loop contract v0: intake-only endpoints (pending
        # review candidates; never verdict/golden/ledger mutation).
        "/api/fit-card/{fit_card_id}/correct",
        "/api/fit-card/{fit_card_id}/reject-market",
        "/api/ledger",
        "/api/ledger/{ledger_entry_id}",
    }
    assert expected <= paths
    forbidden_fragments = ("review", "golden", "export", "promote", "correct")
    for path in paths - expected:
        for fragment in forbidden_fragments:
            assert fragment not in path, f"forbidden surface: {path}"


def test_product_module_does_not_import_review_or_promotion():
    import el.product.api as api_mod
    import el.product.app as app_mod
    import el.product.wiring as wiring_mod

    for mod in (api_mod, app_mod, wiring_mod):
        source = open(mod.__file__, encoding="utf-8").read()
        assert "el.review" not in source
        assert "promotion" not in source
