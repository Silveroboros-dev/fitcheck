"""Rehearse the exact legacy-to-MCP-head path in disposable PostgreSQL 16.

This opt-in test creates and drops a random schema in FITCHECK_POSTGRES_TEST_URL.
It never migrates the database's public schema or an existing FitCheck table.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from el.mcp.schema_preflight import SchemaNotReady, require_schema_head
from el.product.app import build_app
from el.product.wiring import build_services


REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "a4d8c2e6f0b1"
TARGET_REVISION = "6a01d9c2b7e4"
NEW_TABLES = {
    "normalization_attempts",
    "normalization_decisions",
    "market_assessments",
    "market_display_sets",
    "market_display_items",
    "market_choices",
    "source_interpretations",
    "source_thesis_candidates",
    "source_candidate_choices",
    "source_interpretation_requests",
}
PUBLIC_ONLY_MCP_USAGE_MIGRATION = (
    REPO_ROOT
    / "migrations"
    / "versions"
    / "e6c7a8b9d0f1_mcp_usage_buckets.py"
)


def _expected_new_tables() -> set[str]:
    expected = set(NEW_TABLES)
    if PUBLIC_ONLY_MCP_USAGE_MIGRATION.is_file():
        expected.add("mcp_usage_buckets")
    return expected


def _disposable_ci_url(raw_url: str) -> URL:
    url = make_url(raw_url)
    if (
        url.get_backend_name() != "postgresql"
        or url.database != "fitcheck_ci"
        or url.host not in {"127.0.0.1", "localhost"}
        or bool(url.query)
    ):
        raise ValueError(
            "migration rehearsal requires local PostgreSQL database fitcheck_ci"
        )
    return url


def _upgrade(connection: Connection, revision: str) -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


def _downgrade(connection: Connection, revision: str) -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.attributes["connection"] = connection
    command.downgrade(config, revision)


@pytest.mark.parametrize(
    "raw_url",
    [
        "postgresql+psycopg://127.0.0.1/production",
        "postgresql+psycopg://db.example/fitcheck_ci",
        (
            "postgresql+psycopg://127.0.0.1/fitcheck_ci"
            "?host=/cloudsql/foreign-instance"
        ),
        "sqlite:///fitcheck_ci",
    ],
)
def test_migration_rehearsal_rejects_non_disposable_url(raw_url: str) -> None:
    with pytest.raises(ValueError, match="local PostgreSQL database fitcheck_ci"):
        _disposable_ci_url(raw_url)


def test_populated_postgres_16_legacy_schema_reaches_mcp_head() -> None:
    raw_url = (os.environ.get("FITCHECK_POSTGRES_TEST_URL") or "").strip()
    if not raw_url:
        pytest.skip("set FITCHECK_POSTGRES_TEST_URL for the disposable PG gate")
    base_url = _disposable_ci_url(raw_url)

    schema = f"fitcheck_pg_migration_{uuid.uuid4().hex}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    engine = None
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        engine = create_engine(
            base_url,
            connect_args={
                "options": (
                    f"-csearch_path={schema} "
                    "-clock_timeout=5000 -cstatement_timeout=15000"
                )
            },
        )
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            version = int(connection.scalar(text("SHOW server_version_num")))
        assert 160000 <= version < 170000

        with engine.begin() as connection:
            _upgrade(connection, BASE_REVISION)
        original_tables = set(inspect(engine).get_table_names())
        now = datetime.now(timezone.utc)
        job_id = uuid.uuid4()
        analysis_id = uuid.uuid4()
        candidate_set_id = uuid.uuid4()
        snapshot_id = "synthetic-legacy-snapshot"
        payload = {"provider": "fixture", "cutoff": "synthetic"}
        pins = {"normalization_policy_version": "legacy-v1"}
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO jobs (id, created_at, updated_at, job_type, "
                    "owner_client_type, owner_actor_id, idempotency_key, "
                    "payload_hash, payload_hash_version, payload, "
                    "pinned_manifest, status, priority, attempt_count, "
                    "max_attempts, available_at, correlation_id) "
                    "VALUES (:id, :now, :now, 'market_universe_refresh', "
                    "'system', 'migration-test', 'legacy-job', :hash, 1, "
                    "CAST(:payload AS jsonb), CAST(:pins AS jsonb), "
                    "'queued', 100, 0, 3, :now, 'synthetic-correlation')"
                ),
                {
                    "id": job_id,
                    "now": now,
                    "hash": "0" * 64,
                    "payload": json.dumps(payload),
                    "pins": json.dumps(pins),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO market_snapshots "
                    "(id, venue_id, as_of_ts, retrieval_id) "
                    "VALUES (:id, 'fixture', :now, 'legacy-retrieval')"
                ),
                {"id": snapshot_id, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO thesis_analyses "
                    "(id, created_at, input_text, extracted_structure, "
                    "schema_version, normalized_claim_summary, client_type) "
                    "VALUES (:id, :now, 'synthetic claim', CAST(:structure AS jsonb), "
                    "1, 'synthetic claim', 'user')"
                ),
                {"id": analysis_id, "now": now, "structure": "{}"},
            )
            connection.execute(
                text(
                    "INSERT INTO candidate_sets "
                    "(id, created_at, thesis_analysis_id, snapshot_id, "
                    "retrieval_id, job_id) "
                    "VALUES (:id, :now, :analysis, :snapshot, "
                    "'legacy-retrieval', :job)"
                ),
                {
                    "id": candidate_set_id,
                    "now": now,
                    "analysis": analysis_id,
                    "snapshot": snapshot_id,
                    "job": job_id,
                },
            )

        with engine.begin() as connection:
            _upgrade(connection, TARGET_REVISION)

        assert (
            set(inspect(engine).get_table_names()) - original_tables
            == _expected_new_tables()
        )
        assert {col["name"] for col in inspect(engine).get_columns("jobs")} >= {
            "external_effect_started_at",
            "external_effect_attempt_id",
            "accepted_traceparent",
        }
        assert "retrieval_scope" in {
            col["name"] for col in inspect(engine).get_columns("candidate_sets")
        }
        assert "ck_jobs_external_effect_binding" in {
            check["name"] for check in inspect(engine).get_check_constraints("jobs")
        }
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == TARGET_REVISION
            )
            job = connection.execute(
                text(
                    "SELECT status, payload, pinned_manifest, "
                    "external_effect_started_at, external_effect_attempt_id, "
                    "accepted_traceparent FROM jobs WHERE id = :id"
                ),
                {"id": job_id},
            ).one()
            candidate = connection.execute(
                text(
                    "SELECT thesis_analysis_id, snapshot_id, retrieval_id, "
                    "job_id, retrieval_scope FROM candidate_sets WHERE id = :id"
                ),
                {"id": candidate_set_id},
            ).one()
        assert job == ("queued", payload, pins, None, None, None)
        assert candidate == (
            analysis_id,
            snapshot_id,
            "legacy-retrieval",
            job_id,
            None,
        )
        with pytest.raises(SchemaNotReady, match="revision differs"):
            require_schema_head(engine)
        with engine.begin() as connection:
            _upgrade(connection, "head")
        require_schema_head(engine)

        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE jobs SET external_effect_started_at = :now "
                    "WHERE id = :id"
                ),
                {"now": now, "id": job_id},
            )
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT external_effect_started_at FROM jobs WHERE id = :id"
                ),
                {"id": job_id},
            ) is None
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.connect() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


def test_populated_anonymous_demo_downgrade_refuses_before_postgres_ddl() -> None:
    raw_url = (os.environ.get("FITCHECK_POSTGRES_TEST_URL") or "").strip()
    if not raw_url:
        pytest.skip("set FITCHECK_POSTGRES_TEST_URL for the disposable PG gate")
    base_url = _disposable_ci_url(raw_url)

    schema = f"fitcheck_pg_demo_downgrade_{uuid.uuid4().hex}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    engine = None
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        engine = create_engine(
            base_url,
            connect_args={
                "options": (
                    f"-csearch_path={schema} "
                    "-clock_timeout=5000 -cstatement_timeout=15000"
                )
            },
        )
        with engine.begin() as connection:
            _upgrade(connection, "head")

        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        services = build_services(
            mode="fixture", session_factory=sessions, anonymous_demo=True
        )
        with TestClient(build_app(services)) as client:
            assert client.get("/api/health").status_code == 200
            response = client.post(
                "/api/demo/hormuz", headers={"Origin": "http://testserver"}
            )
            assert response.status_code == 200, response.text

        with engine.begin() as connection:
            _downgrade(connection, "4f7c2e9a6b13")
        with pytest.raises(
            RuntimeError,
            match=(
                "predecessor-incompatible anonymous demo rows exist in "
                "market_display_sets, market_assessments, fit_cards"
            ),
        ), engine.begin() as connection:
            _downgrade(connection, "18e9c4a7d2b5")

        with engine.connect() as connection:
            table_names = set(inspect(connection).get_table_names())
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "4f7c2e9a6b13"
            assert "demo_sessions" in table_names
            assert connection.scalar(
                text("SELECT count(*) FROM demo_sessions")
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM market_display_sets "
                    "WHERE assessed_count = 0 AND displayed_count = 1"
                )
            ) == 1
            assert not {
                name for name in table_names if name.startswith("_alembic_tmp_")
            }
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.connect() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()
