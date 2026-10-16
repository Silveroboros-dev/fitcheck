"""Visitor ownership and honest evidence states for the fixture-only demo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib.resources import files
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.exc import IntegrityError

from el.domain.tables import DemoSession, MarketAssessment, MarketDisplaySet
from el.product.app import build_app
from el.product.demo_sessions import COOKIE_NAME
from el.product.wiring import (
    MULTI_THESIS_FIXTURE, STABLECOIN_UI_REVISED_INPUT, _alembic_config,
    build_services, make_session_factory,
)
from el.sourceinterpretation.jobs import SourceInterpretationWorker

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture()
def demo(tmp_path):
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'demo.sqlite'}")

    @event.listens_for(sessions.kw["bind"], "checkout")
    def enforce_foreign_keys(connection, *_):
        connection.execute("PRAGMA foreign_keys = ON")

    services = build_services(mode="fixture", session_factory=sessions, anonymous_demo=True)
    app = build_app(services)
    return services, sessions, TestClient(app), TestClient(app)


def test_two_visitors_reset_expiry_and_origin_gate(demo):
    _, sessions, one, two = demo
    first = one.get("/api/health").json()
    first_cookie = one.cookies.get(COOKIE_NAME)
    second = two.get("/api/health").json()
    assert first["session_ref"] != second["session_ref"]
    assert first["founder_acceptance"] == {"enabled": False}
    assert "database_path" not in str(first)
    assert one.post("/api/demo/hormuz").status_code == 403
    assert one.post("/api/demo/hormuz", headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert one.post("/api/demo/hormuz", headers=ORIGIN).status_code == 200
    seeded = one.post("/api/demo/hormuz", headers=ORIGIN).json()
    revision = seeded["revision_id"]
    saved = one.get(f"/api/v3/thesis-revisions/{revision}/revisit").json()
    assert saved["latest"]["fixture_possible_impact"]["binding"]["revision_id"] == revision
    assert saved["latest"]["decision"] is None
    with sessions() as s:
        assessment = s.get(MarketAssessment, uuid.UUID(saved["activation"]["binding"]["market_assessment_id"]))
        display = s.get(MarketDisplaySet, uuid.UUID(saved["activation"]["binding"]["market_display_set_id"]))
        assert assessment.pair_class is None
        assert not display.assessment_complete
        assert display.assessed_count == 0
        assert display.displayed_count == 1
    thesis_id = saved["activation"]["binding"]["thesis_analysis_id"]
    assert one.get(f"/api/v3/theses/{thesis_id}/accepted-state").status_code == 404
    assert two.get(f"/api/v3/thesis-revisions/{revision}/revisit").status_code == 404
    packet = saved["latest"]["packet"]["id"]
    decision = {"action": "reaffirm",
                "expected_revision_digest": saved["activation"]["binding"]["revision_digest"],
                "expected_observation_id": saved["latest"]["observation"]["id"]}
    assert two.post(f"/api/v3/revisit-packets/{packet}/decision", json=decision,
                    headers=ORIGIN).status_code == 404
    assert one.get("/api/ledger").status_code == 404
    assert one.post("/api/fit-card/" + revision + "/correct", json={},
                    headers=ORIGIN).status_code == 404
    rotated = one.post("/api/demo/reset", headers=ORIGIN)
    assert rotated.status_code == 200
    assert rotated.json()["session_ref"] != first["session_ref"]
    stale = one.get("/api/health", headers={"Cookie": f"{COOKIE_NAME}={first_cookie}"})
    assert stale.status_code == 401
    assert stale.headers.get("set-cookie") is None
    assert one.get("/api/health").json()["session_ref"] == rotated.json()["session_ref"]
    assert one.get(f"/api/v3/thesis-revisions/{revision}/revisit").status_code == 404
    with sessions.begin() as s:
        active = s.get(DemoSession, uuid.UUID(rotated.json()["session_ref"]))
        active.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired = one.get("/api/health")
    assert expired.status_code == 401
    assert expired.headers.get("set-cookie") is None
    renewal = one.post("/api/demo/reset", headers=ORIGIN)
    assert renewal.status_code == 200
    renewed = one.get("/api/health")
    assert renewed.status_code == 200
    assert renewed.json()["session_ref"] != rotated.json()["session_ref"]
    with sessions.begin() as s:
        active = s.get(DemoSession, uuid.UUID(renewed.json()["session_ref"]))
        active.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert one.get("/").status_code == 200
    assert one.get("/api/health").json()["session_ref"] != renewed.json()["session_ref"]


def test_synthetic_market_sources_are_same_origin_allowlisted_assets(demo):
    _, sessions, one, _ = demo
    health = one.get("/api/health").json()
    with sessions.begin() as session:
        active = session.get(DemoSession, uuid.UUID(health["session_ref"]))
        active.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    names = (
        "synthetic-market-stablecoin-total-cap.json",
        "synthetic-market-stablecoin-unit-cap.json",
        "synthetic-market-treasury-ledger-payment.json",
        "synthetic-market-broker-tokenized-asset.json",
        "synthetic-market-broker-first-tokenized.json",
        "synthetic-market-broker-chain-token.json",
    )
    fixture_root = files("el.product.demo_fixtures").joinpath("markets")
    for name in names:
        response = one.get(f"/assets/demo-fixtures/markets/{name}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.headers["content-disposition"] == (
            f'inline; filename="{name}"'
        )
        assert response.content == fixture_root.joinpath(name).read_bytes()
        assert response.json()["source_url"].startswith("https://example.invalid/")

    assert one.get(
        "/assets/demo-fixtures/markets/synthetic-event-stablecoin-cap.json"
    ).status_code == 404
    assert one.get(
        "/assets/demo-fixtures/markets/not-declared.json"
    ).status_code == 404


def test_jobs_and_supported_market_are_owner_scoped(demo):
    services, sessions, one, two = demo
    one.get("/api/health")
    two.get("/api/health")
    payload = {"input_text": MULTI_THESIS_FIXTURE, "idempotency_key": "same-key"}
    job1 = one.post("/api/v3/source-interpretation-jobs", json=payload, headers=ORIGIN)
    job2 = two.post("/api/v3/source-interpretation-jobs", json=payload, headers=ORIGIN)
    assert job1.status_code == job2.status_code == 202
    assert job1.json()["job_id"] != job2.json()["job_id"]
    assert two.get(f"/api/v3/source-interpretation-jobs/{job1.json()['job_id']}").status_code == 404
    assert one.get("/api/v3/source-interpretation-jobs/by-idempotency/same-key").json()["job_id"] == job1.json()["job_id"]
    old = job1.json()["job_id"]
    one.post("/api/demo/reset", headers=ORIGIN)
    worker = SourceInterpretationWorker(
        jobs=services.jobs, source_interpretation=services.source_interpretation,
        session_factory=sessions, worker_id="demo-test-worker",
    )
    assert worker.run_once().status == "succeeded"
    assert worker.run_once().status == "succeeded"
    assert one.get(f"/api/v3/source-interpretation-jobs/{old}").status_code == 404
    assert one.get("/api/v3/source-interpretation-jobs/by-idempotency/same-key").status_code == 404
    own = two.get(f"/api/v3/source-interpretation-jobs/{job2.json()['job_id']}")
    assert own.status_code == 200
    interpretation = own.json()["interpretation"]
    candidate = interpretation["candidates"][0]
    chosen = two.post(
        f"/api/v3/source-interpretations/{interpretation['source_interpretation_id']}/choice",
        json={"selection_kind": "candidate",
              "source_thesis_candidate_id": candidate["source_thesis_candidate_id"]},
        headers=ORIGIN,
    )
    assert chosen.status_code == 200
    initial = two.post(
        f"/api/v3/source-thesis-candidates/{candidate['source_thesis_candidate_id']}/normalization-attempt",
        headers=ORIGIN,
    )
    assert initial.status_code == 200
    assert initial.json()["outcome"] == "clarification"
    revised = two.post(
        f"/api/v3/normalization-attempts/{initial.json()['normalization_attempt_id']}/revise",
        json={"input_text": STABLECOIN_UI_REVISED_INPUT,
              "expected_input_digest": initial.json()["input_digest"]},
        headers=ORIGIN,
    )
    assert revised.status_code == 200
    accepted = two.post(
        f"/api/v3/normalization-attempts/{revised.json()['normalization_attempt_id']}/accept",
        json={"expected_input_digest": revised.json()["input_digest"]},
        headers=ORIGIN,
    )
    assert accepted.status_code == 200
    thesis_id = accepted.json()["thesis_analysis_id"]
    pool = two.post(f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN)
    assert pool.status_code == 200, pool.text
    assert pool.json()["displayed_count"] == 3
    assert {card["market_id"] for card in pool.json()["candidate_markets"]} == {
        "synthetic-stablecoin-total-cap",
        "synthetic-stablecoin-unit-cap",
        "synthetic-treasury-ledger-payment",
    }
    assert one.post(f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN).status_code == 404
    display_id = pool.json()["market_display_set_id"]
    assert one.post(f"/api/v3/market-pools/{display_id}/choice", headers=ORIGIN,
                    json={"selection_kind": "none"}).status_code == 404


def test_supported_predecessor_requires_explicit_upgrade(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'prior.sqlite'}"
    monkeypatch.setenv("FITCHECK_DB_URL", url)
    config = _alembic_config()
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "18e9c4a7d2b5")
    with pytest.raises(RuntimeError, match="migration is not current"):
        make_session_factory(url)
    command.upgrade(config, "head")
    factory = make_session_factory(url)
    with factory() as s:
        assert s.execute(text("SELECT version_num FROM alembic_version")).scalar() == ScriptDirectory.from_config(config).get_current_head()


def test_populated_demo_downgrade_refuses_before_sqlite_ddl(tmp_path):
    url = f"sqlite:///{tmp_path / 'populated-downgrade.sqlite'}"
    sessions = make_session_factory(url)
    services = build_services(
        mode="fixture", session_factory=sessions, anonymous_demo=True
    )
    with TestClient(build_app(services)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/demo/hormuz", headers=ORIGIN).status_code == 200

    engine = sessions.kw["bind"]
    config = _alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        # Private head has one later revision; this is a no-op in the public
        # projection, where the anonymous-demo revision is the head.
        command.downgrade(config, "4f7c2e9a6b13")
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(
            RuntimeError,
            match=(
                "predecessor-incompatible anonymous demo rows exist in "
                "market_display_sets, market_assessments, fit_cards"
            ),
        ):
            command.downgrade(config, "18e9c4a7d2b5")

    with engine.connect() as connection:
        table_names = set(inspect(connection).get_table_names())
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "4f7c2e9a6b13"
        assert "demo_sessions" in table_names
        assert connection.scalar(text("SELECT count(*) FROM demo_sessions")) == 1
        assert connection.scalar(
            text(
                "SELECT count(*) FROM market_display_sets "
                "WHERE assessed_count = 0 AND displayed_count = 1"
            )
        ) == 1
        assert connection.scalar(
            text(
                "SELECT count(*) FROM market_assessments WHERE pair_class IS NULL"
            )
        ) == 1
        assert connection.scalar(
            text("SELECT count(*) FROM fit_cards WHERE semantic_fit_class IS NULL")
        ) == 1
        assert not {
            name for name in table_names if name.startswith("_alembic_tmp_")
        }
        assert next(
            column
            for column in inspect(connection).get_columns("market_assessments")
            if column["name"] == "pair_class"
        )["nullable"]
        assert next(
            column
            for column in inspect(connection).get_columns("fit_cards")
            if column["name"] == "semantic_fit_class"
        )["nullable"]


def _run_offline_alembic(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment["FITCHECK_DB_URL"] = "postgresql+psycopg://u:p@localhost/db"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            *arguments,
            "--sql",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("starting_revision", ["4f7c2e9a6b13", "head"])
def test_demo_downgrade_offline_sql_refuses_before_preflight_or_ddl(
    starting_revision: str,
):
    completed = _run_offline_alembic(
        "downgrade", f"{starting_revision}:18e9c4a7d2b5"
    )
    expected_error = (
        "RuntimeError: cannot downgrade 4f7c2e9a6b13: offline downgrade SQL "
        "generation is unavailable because a live data preflight is required"
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert expected_error in completed.stderr
    assert "AttributeError" not in output
    assert completed.stdout == ""


def test_offline_guard_allows_unrelated_ranges_to_reach_sql_generation():
    upgrade = _run_offline_alembic("upgrade", "base:head")
    # Some older batch migrations have their own offline reflection limit.
    # This guard must still let an upgrade reach its existing SQL behavior.
    assert "CREATE TABLE" in upgrade.stdout
    assert "offline downgrade SQL generation is unavailable" not in (
        upgrade.stdout + upgrade.stderr
    )

    earlier_downgrade = _run_offline_alembic(
        "downgrade", "18e9c4a7d2b5:6a01d9c2b7e4"
    )
    assert earlier_downgrade.returncode == 0, (
        earlier_downgrade.stdout + earlier_downgrade.stderr
    )
    assert "DROP TABLE" in earlier_downgrade.stdout

    head_to_demo = _run_offline_alembic(
        "downgrade", "head:4f7c2e9a6b13"
    )
    assert head_to_demo.returncode == 0, (
        head_to_demo.stdout + head_to_demo.stderr
    )
    current_head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    if current_head != "4f7c2e9a6b13":
        assert "DROP TABLE" in head_to_demo.stdout


def test_empty_demo_migration_downgrades_to_predecessor(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty-downgrade.sqlite'}")
    config = _alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "4f7c2e9a6b13")
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "18e9c4a7d2b5")

    with engine.connect() as connection:
        table_names = set(inspect(connection).get_table_names())
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "18e9c4a7d2b5"
        assert "demo_sessions" not in table_names
        assert not {
            name for name in table_names if name.startswith("_alembic_tmp_")
        }


def test_anonymous_mode_rejects_live_modes_before_database_setup(monkeypatch):
    monkeypatch.setenv("FITCHECK_ANONYMOUS_DEMO", "1")

    def should_not_open_database():
        raise AssertionError("database setup ran before the fixture-only guard")

    monkeypatch.setattr("el.product.wiring.make_session_factory", should_not_open_database)
    for mode in ("gemini", "vertex-canary"):
        with pytest.raises(ValueError, match="anonymous demo requires fixture mode"):
            build_services(mode=mode)


def test_reconstructed_graph_exceptions_are_database_narrow(demo):
    _, sessions, one, _ = demo
    one.get("/api/health")
    seeded = one.post("/api/demo/hormuz", headers=ORIGIN).json()
    saved = one.get(f"/api/v3/thesis-revisions/{seeded['revision_id']}/revisit").json()
    binding = saved["activation"]["binding"]
    ids = {name: uuid.UUID(binding[name]).hex for name in (
        "market_assessment_id", "market_display_set_id"
    )}
    with sessions() as s:
        ids["fit_card_id"] = s.get(
            MarketAssessment, uuid.UUID(binding["market_assessment_id"])
        ).fit_card_id.hex

    # The one unassessed display link is allowed only in the labelled
    # reconstruction. Ordinary fit/pair classes and denominators stay intact.
    invalid_updates = (
        ("UPDATE fit_cards SET provenance = '{}' WHERE id = :id", ids["fit_card_id"]),
        ("UPDATE market_assessments SET pair_class = 'direct' WHERE id = :id",
         ids["market_assessment_id"]),
        ("UPDATE market_assessments SET authority = 'normal' WHERE id = :id",
         ids["market_assessment_id"]),
        ("UPDATE market_display_sets SET display_policy_version = 'ordinary' WHERE id = :id",
         ids["market_display_set_id"]),
        ("UPDATE market_display_sets SET assessment_complete = TRUE WHERE id = :id",
         ids["market_display_set_id"]),
    )
    for statement, row_id in invalid_updates:
        with sessions() as s:
            with pytest.raises(IntegrityError):
                s.execute(text(statement), {"id": row_id})
            s.rollback()
