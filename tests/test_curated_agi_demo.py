"""The packaged AGI reconstruction is a sealed read-only fixture, not acceptance."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.resources import files
import json
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect, text

from el.product.app import build_app
from el.product.api import ProductApi
from el.product.curated_demo import CuratedAgiCase, load_curated_agi_case
from el.product.demo_admission import DemoAdmission
from el.product.wiring import build_services, local_actor, make_session_factory


ROOT = Path(__file__).resolve().parents[1]


def test_packaged_case_is_ordered_curated_copy_and_canonically_sealed():
    resource = files("el.product.demo_fixtures").joinpath("agi2028.json")
    raw = json.loads(resource.read_text(encoding="utf-8"))
    envelope = load_curated_agi_case()
    assert envelope["case"] == raw
    assert envelope["sha256"] == sha256(json.dumps(
        raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()).hexdigest()
    assert [row["key"] for row in raw["markets"]] == ["rsi", "millennium", "agi"]
    assert [row["earlier_class"] for row in raw["markets"]] == ["not_an_expression"] * 3
    assert [row["current_class"] for row in raw["markets"]] == [
        "weak_proxy", "not_an_expression", "not_an_expression"
    ]
    assert raw["pool_outcome"] == "no_clean_expression"
    assert not {"accepted_state", "market_pool", "normalization_decision_id"} & set(raw)
    assert "project-authored summaries" in raw["provenance_note"]


@pytest.mark.parametrize("change", [
    lambda p: p["markets"].reverse(),
    lambda p: p["markets"][0].update(current_class="direct"),
    lambda p: p.update(pool_outcome="candidate_expressions"),
    lambda p: p.update(accepted_state={"acceptance_origin": "human_ui"}),
    lambda p: p["markets"][0].update(conditions_summary=" "),
])
def test_curated_schema_rejects_mixed_or_fabricated_evidence(change):
    case = deepcopy(load_curated_agi_case()["case"])
    change(case)
    with pytest.raises(ValueError):
        CuratedAgiCase.model_validate(case)


@pytest.mark.parametrize("hosted", [False, True])
def test_read_only_case_is_available_to_anonymous_fixture_without_mutating_rows(
    tmp_path, hosted,
):
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'demo.sqlite'}")
    services = build_services(mode="fixture", session_factory=sessions, anonymous_demo=True)
    admission = (DemoAdmission(datetime.now(timezone.utc) + timedelta(hours=1))
                 if hosted else None)
    app = build_app(services, demo_admission=admission)
    client = TestClient(app, base_url="https://demo.example" if hosted else "http://testserver",
                        client=("198.51.100.4", 50000))
    health = client.get("/api/health").json()
    assert health["historical_case"] is None
    assert health["curated_agi_case"]["sha256"] == load_curated_agi_case()["sha256"]

    def rows():
        with sessions.kw["bind"].connect() as conn:
            return {name: conn.execute(text(f'SELECT count(*) FROM "{name}"')).scalar()
                    for name in inspect(conn).get_table_names()}

    before = rows()
    for _ in range(2):
        response = client.get("/api/demo/agi2028")
        assert response.status_code == 200
        assert response.json() == {
            **load_curated_agi_case(), "session_ref": health["session_ref"]
        }
    assert rows() == before
    assert client.post("/api/demo/agi2028", headers={
        "Origin": "https://demo.example" if hosted else "http://testserver"
    }).status_code == 404
    assert client.get("/api/demo/history/agi2028").status_code == 404


def test_curated_case_is_absent_outside_anonymous_fixture(tmp_path):
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'demo.sqlite'}")
    services = build_services(mode="fixture", session_factory=sessions)
    client = TestClient(build_app(ProductApi(services, local_actor(sessions))))
    assert client.get("/api/demo/agi2028").status_code == 404
    assert client.get("/api/health").json().get("curated_agi_case") is None


def test_curated_agi_dom_versions_session_reset_and_stale_response(tmp_path):
    contract = (ROOT / "docs/agi2028-curated-demo-ui-contract-v1.md").read_text()
    assert "Curated reconstruction" in contract
    assert "/api/demo/agi2028" in contract
    path = tmp_path / "curated.json"
    path.write_text(json.dumps(load_curated_agi_case()))
    result = subprocess.run([
        "node", str(ROOT / "tests/product_console_curated_agi_dom.js"),
        str(ROOT / "el/product/static/product_console.html"), str(path),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
