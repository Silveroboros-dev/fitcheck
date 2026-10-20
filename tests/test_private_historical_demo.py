"""Private historical evidence stays frozen and separate from visitor decisions."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect, text

from el.product.app import build_app, load_historical_demo_case
from el.product.demo_launcher import _child_environment, main
from el.product.wiring import build_services, make_session_factory

ROOT = Path(__file__).resolve().parents[1]


def sealed(payload):
    return {"case": payload, "sha256": sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()).hexdigest()}


@pytest.fixture
def history():
    uid = lambda: str(uuid.uuid4())
    thesis, decision, pool_id = uid(), uid(), uid()
    cards = [{
        "market_assessment_id": uid(), "candidate_set_member_id": uid(),
        "market_id": f"synthetic-research-{rank}", "market_title": f"Research {rank}?",
        "resolution_conditions": f"Synthetic announcement rule {rank}.",
        "pair_class": "not_an_expression", "retrieval_rank": rank, "display_rank": rank,
        "what_it_captures": "A synthetic announcement.",
        "what_it_misses": "A full research cycle.",
        "horizon_match": "poor", "resolution_risk": "medium",
        "fit_confidence": None, "authority": "deterministic_only",
        "snapshot_id": "synthetic-history", "rules_capture_id": uid(),
        "frozen_demo_evidence": None,
    } for rank in range(1, 4)]
    accepted = {
        "thesis_analysis_id": thesis, "normalization_decision_id": decision,
        "accepted_thesis_summary": "Autonomous research by 2028.",
        "accepted_thesis_wording": "A full research cycle is publicly demonstrated by 2028.",
        "accepted_at": "2026-10-20T13:00:00+00:00",
        "acceptance_origin": "human_ui", "restored_source": None,
        "latest_market_display_set_id": pool_id,
    }
    pool = {
        "market_display_set_id": pool_id, "fit_card_id": uid(),
        "thesis_analysis_id": thesis,
        "accepted_thesis_summary": accepted["accepted_thesis_summary"],
        "accepted_thesis_wording": accepted["accepted_thesis_wording"],
        "candidate_set_id": uid(),
        "retrieval_scope": {
            "provider": "synthetic-history-test", "scope_kind": "frozen_snapshot",
            "query_text": None, "query_binding": None,
            "snapshot_id": "synthetic-history", "retrieval_id": "test-retrieval",
            "returned_count": 3, "lifecycle_eligible_count": 3,
            "lifecycle_excluded_count": 0, "gate_eligible_count": 3,
        },
        "snapshot_id": "synthetic-history", "snapshot_as_of": "2026-10-20T00:00:00+00:00",
        "display_policy_version": "test-v1", "assessed_count": 3, "target_count": 3,
        "displayed_count": 3, "assessment_complete": True,
        "system_pool_outcome": "no_clean_expression", "incomplete_reasons": [],
        "candidate_markets": cards,
    }
    return sealed({
        "schema_version": "private-historical-demo-v1", "case_id": "agi2028",
        "title": "Synthetic history test", "recorded_at": "2026-10-20T14:00:00+00:00",
        "original_input": "Autonomous research by 2028",
        "acceptance_note": "The original human accepted a public criterion.",
        "provenance_note": "Synthetic test evidence, no current quotes.",
        "accepted_state": accepted, "market_pool": pool,
    })


def successor_history(history):
    current = deepcopy(history["case"])
    current["schema_version"] = "private-historical-demo-v2"
    current["presentation"] = {
        "original_input_en": "By December 31, 2028, an AI system capable of independently conducting a full cycle of AI research will exist.",
        "acceptance_note_en": "We accept public launch or demonstration as the criterion.",
    }
    current["predecessor"] = deepcopy(history)
    current["provenance_note"] = "Successor proposal over saved rules; original run retained."
    pool = current["market_pool"]
    pool["market_display_set_id"] = str(uuid.uuid4())
    pool["fit_card_id"] = str(uuid.uuid4())
    pool["candidate_set_id"] = str(uuid.uuid4())
    current["accepted_state"]["latest_market_display_set_id"] = pool["market_display_set_id"]
    for card in pool["candidate_markets"]:
        card["market_assessment_id"] = str(uuid.uuid4())
        card["candidate_set_member_id"] = str(uuid.uuid4())
    related = pool["candidate_markets"][0]
    accepted = current["accepted_state"]
    wording = accepted["accepted_thesis_wording"]
    rules = related["resolution_conditions"]
    related["pair_class"] = "weak_proxy"
    related["authority"] = "source_bound_relation_proposal"
    related["relatedness_basis"] = {
        "schema_version": "source-bound-relatedness-v1",
        "predecessor_pair_class": "not_an_expression",
        "authority": "source_bound_relation_proposal",
        "proposal": {
            "proposal_id": str(uuid.uuid4()), "proposer_id": "local-demo-agent",
            "thesis_analysis_id": accepted["thesis_analysis_id"],
            "accepted_wording_sha256": sha256(wording.encode()).hexdigest(),
            "market_id": related["market_id"], "snapshot_id": related["snapshot_id"],
            "rules_capture_id": related["rules_capture_id"],
            "rules_sha256": sha256(rules.encode()).hexdigest(),
            "claim_quote": "A full research cycle",
            "rules_quote": "Synthetic announcement rule 1.",
            "rationale": "The announcement concerns related research, but it does not settle the full cycle thesis.",
        },
    }
    return sealed(current)


def test_history_absent_by_default_and_immutable_when_enabled(tmp_path, history):
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'demo.sqlite'}")
    services = build_services(mode="fixture", session_factory=sessions, anonymous_demo=True)
    default = TestClient(build_app(services))
    assert default.get("/api/health").json()["historical_case"] is None
    assert default.get("/api/demo/history/agi2028").status_code == 404
    app = build_app(services, historical_case=history)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    assert TestClient(app).get("/api/health").status_code == 403
    assert TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.1", 50000)).get("/api/demo/history/agi2028").status_code == 403
    health = client.get("/api/health").json()
    assert health["historical_case"]["sha256"] == history["sha256"]

    def rows():
        with sessions.kw["bind"].connect() as conn:
            return {name: conn.execute(text(f'SELECT count(*) FROM "{name}"')).scalar()
                    for name in inspect(conn).get_table_names()}

    before = rows()
    for _ in range(2):
        response = client.get("/api/demo/history/agi2028")
        assert response.status_code == 200
        assert response.json() == {**history, "session_ref": health["session_ref"]}
    assert rows() == before
    accepted = history["case"]["accepted_state"]
    pool = history["case"]["market_pool"]
    assert client.get(f'/api/v3/theses/{accepted["thesis_analysis_id"]}/accepted-state').status_code == 404
    assert client.get(f'/api/v3/market-pools/{pool["market_display_set_id"]}').status_code == 404
    assert client.post("/api/demo/history/agi2028", headers={"Origin": "http://127.0.0.1"}).status_code == 404
    assert client.get("/api/demo/history/other").status_code == 404
    # Startup captured a private immutable copy, not the caller's mutable dict.
    history["case"]["title"] = "changed afterwards"
    assert client.get("/api/demo/history/agi2028").json()["case"]["title"] != "changed afterwards"


@pytest.mark.parametrize("change", [
    lambda p: p["accepted_state"].update(thesis_analysis_id=str(uuid.uuid4())),
    lambda p: p["accepted_state"].update(latest_market_display_set_id=str(uuid.uuid4())),
    lambda p: p["market_pool"].update(accepted_thesis_wording="Different thesis"),
    lambda p: p["market_pool"].update(assessment_complete=False),
    lambda p: p["market_pool"].update(assessed_count=2),
    lambda p: p["market_pool"]["retrieval_scope"].update(snapshot_id="other"),
    lambda p: p["market_pool"]["candidate_markets"][0].update(snapshot_id="other"),
    lambda p: p["market_pool"]["candidate_markets"][0].update(pair_class="direct"),
    lambda p: p["market_pool"]["candidate_markets"][0].update(authority="human_reviewed"),
    lambda p: p["market_pool"]["candidate_markets"][0].update(relatedness_basis={}),
    lambda p: p["market_pool"]["candidate_markets"][1].update(
        market_assessment_id=p["market_pool"]["candidate_markets"][0]["market_assessment_id"]),
])
def test_history_rejects_cross_panel_or_incomplete_evidence(tmp_path, history, change):
    payload = deepcopy(history["case"])
    change(payload)
    path = tmp_path / "case.json"
    path.write_text(json.dumps(sealed(payload)))
    with pytest.raises(ValueError):
        load_historical_demo_case(path, sealed(payload)["sha256"])


def test_digest_and_launcher_boundary(tmp_path, history, monkeypatch):
    path = tmp_path / "case.json"
    path.write_text(json.dumps(history))
    pin = history["sha256"]
    assert load_historical_demo_case(path, pin) == history
    history["case"]["original_input"] = "tampered"
    path.write_text(json.dumps(history))
    with pytest.raises(ValueError, match="digest mismatch"):
        load_historical_demo_case(path, pin)
    path.write_text(json.dumps(sealed(history["case"])))
    with pytest.raises(ValueError, match="digest mismatch"):
        load_historical_demo_case(path, pin)
    monkeypatch.setenv("FITCHECK_PRIVATE_HISTORY_FILE", "/unrequested/private/case.json")
    assert "FITCHECK_PRIVATE_HISTORY_FILE" not in _child_environment("sqlite://")
    with pytest.raises(SystemExit):
        main(["--host", "0.0.0.0", "--historical-case", str(path),
              "--historical-case-sha256", pin])


def test_v2_preserves_predecessor_and_serves_successor_read_only(tmp_path, history):
    successor = successor_history(history)
    path = tmp_path / "successor.json"
    path.write_text(json.dumps(successor))
    assert load_historical_demo_case(path, successor["sha256"]) == successor
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'demo.sqlite'}")
    services = build_services(mode="fixture", session_factory=sessions, anonymous_demo=True)
    client = TestClient(build_app(services, historical_case=successor),
                        base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    health = client.get("/api/health").json()
    assert health["historical_case"]["schema_version"] == "private-historical-demo-v2"
    before = client.get("/api/demo/history/agi2028").json()
    assert before["case"]["predecessor"] == history
    assert before["case"]["market_pool"]["candidate_markets"][0]["pair_class"] == "weak_proxy"
    with sessions.kw["bind"].connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM normalization_decisions")).scalar()
    assert count == 0
    assert client.get("/api/demo/history/agi2028").json() == before


@pytest.mark.parametrize("change", [
    lambda p: p["accepted_state"].update(normalization_decision_id=str(uuid.uuid4())),
    lambda p: p["market_pool"].update(snapshot_id="other"),
    lambda p: p["market_pool"]["candidate_markets"][0].update(
        resolution_conditions="Different rule bytes"),
    lambda p: p["market_pool"]["candidate_markets"][0].update(
        rules_capture_id=str(uuid.uuid4())),
    lambda p: p["market_pool"]["candidate_markets"][0]["relatedness_basis"]["proposal"].update(
        rules_quote="Invented excerpt"),
    lambda p: p["market_pool"]["candidate_markets"][0]["relatedness_basis"]["proposal"].update(
        rationale="   "),
    lambda p: p["market_pool"]["candidate_markets"][0]["relatedness_basis"]["proposal"].update(
        rationale="You should buy this market."),
    lambda p: p["market_pool"]["candidate_markets"][0]["relatedness_basis"].update(
        authority="human_reviewed"),
    lambda p: p["market_pool"]["candidate_markets"][0].update(
        relatedness_basis=None),
])
def test_v2_rejects_mixed_or_unbound_successor(history, change, tmp_path):
    payload = successor_history(history)["case"]
    change(payload)
    envelope = sealed(payload)
    path = tmp_path / "tampered-successor.json"
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError):
        load_historical_demo_case(path, envelope["sha256"])


def test_v2_rejects_changed_predecessor_even_when_outer_digest_is_recomputed(history, tmp_path):
    payload = successor_history(history)["case"]
    payload["predecessor"]["case"]["market_pool"]["candidate_markets"][0][
        "resolution_conditions"] = "Changed predecessor rules"
    envelope = sealed(payload)
    path = tmp_path / "tampered-predecessor.json"
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="predecessor digest mismatch"):
        load_historical_demo_case(path, envelope["sha256"])


def test_historical_demo_dom_and_contract(tmp_path, history):
    contract = (ROOT / "docs/private-historical-demo-ui-contract-v1.md").read_text()
    assert all(f"AC-H{i}" in contract for i in range(1, 8))
    path = tmp_path / "case.json"
    path.write_text(json.dumps(history))
    result = subprocess.run([
        "node", str(ROOT / "tests/product_console_history_dom.js"),
        str(ROOT / "el/product/static/product_console.html"), str(path),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

    successor = successor_history(history)
    path.write_text(json.dumps(successor))
    result = subprocess.run([
        "node", str(ROOT / "tests/product_console_history_dom.js"),
        str(ROOT / "el/product/static/product_console.html"), str(path),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
