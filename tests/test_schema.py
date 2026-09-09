"""Migration ↔ model parity, and round-trip persistence on SQLite."""

import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from el.domain.tables import (
    ActiveMarketUniverse,
    Base,
    ConvictionEvent,
    MarketUniverseSnapshot,
    ThesisAnalysis,
)
from el.jobs import JobStore

REPO = Path(__file__).resolve().parents[1]


def test_metadata_creates_on_sqlite(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    tables = set(Base.metadata.tables)
    # The v3.1 migrations add durable source interpretation, explicit
    # normalization, and bounded market-pool projections to the PR3 schema.
    assert {
        "source_interpretation_requests",
        "source_interpretations",
        "source_thesis_candidates",
        "source_candidate_choices",
        "normalization_attempts",
        "normalization_decisions",
        "market_rules_captures",
        "market_assessments",
        "market_display_sets",
        "market_display_items",
        "market_choices",
    } <= tables
    assert len(tables) == 34


def test_alembic_upgrade_matches_models(tmp_path):
    """alembic upgrade head must reproduce the model metadata exactly:
    a follow-up autogenerate against the migrated DB must be empty."""
    db = tmp_path / "mig.db"
    # Run alembic via the current interpreter, not a hardcoded .venv path,
    # so the suite is portable across environments and CI.
    env = {"DATABASE_URL": f"sqlite:///{db}", "PATH": "/usr/bin:/bin"}
    alembic_cmd = [sys.executable, "-m", "alembic"]
    up = subprocess.run(
        [*alembic_cmd, "upgrade", "head"], cwd=REPO, env=env,
        capture_output=True, text=True,
    )
    assert up.returncode == 0, up.stderr
    check = subprocess.run(
        [*alembic_cmd, "check"], cwd=REPO, env=env,
        capture_output=True, text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr

    # The expand migration must also remain mechanically reversible while no
    # production cutover depends on its new columns.
    down = subprocess.run(
        [*alembic_cmd, "downgrade", "f2a4c6e8b1d3"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert down.returncode == 0, down.stdout + down.stderr
    up_again = subprocess.run(
        [*alembic_cmd, "upgrade", "head"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert up_again.returncode == 0, up_again.stdout + up_again.stderr
    check_again = subprocess.run(
        [*alembic_cmd, "check"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert check_again.returncode == 0, check_again.stdout + check_again.stderr


def test_blind_prior_roundtrip_before_ledger_entry(tmp_path):
    """The temporal rule end-to-end: a blind-prior event persists with no
    ledger entry in existence, anchored on the analysis."""
    engine = create_engine(f"sqlite:///{tmp_path}/rt.db")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ta = ThesisAnalysis(
            input_text="hot take",
            extracted_structure={"schema_version": 1},
            normalized_claim_summary="claim",
            client_type="agent_mcp",
        )
        s.add(ta)
        s.flush()
        s.add(
            ConvictionEvent(
                thesis_analysis_id=ta.id,
                ledger_entry_id=None,
                prior_type="blind",
                market_context_seen=False,
                prior_probability=0.62,
                client_type="agent_mcp",
            )
        )
        s.commit()
        ev = s.scalars(select(ConvictionEvent)).one()
        assert ev.ledger_entry_id is None
        assert ev.odds_revealed_at is None
        assert isinstance(ev.thesis_analysis_id, uuid.UUID)


def test_active_snapshot_pointer_enforces_provider_and_venue_identity(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/fk.db")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    submitted = JobStore(sessions).submit_or_get(
        job_type="market_universe_refresh",
        owner_client_type="system",
        owner_actor_id="schema-test",
        idempotency_key="snapshot",
        payload={},
        now=datetime(2026, 8, 6, tzinfo=timezone.utc),
    )
    snapshot_id = f"mu_{'0' * 64}"
    with sessions() as session:
        session.add(
            MarketUniverseSnapshot(
                id=snapshot_id,
                provider="polydata",
                venue="polymarket",
                cutoff_utc=datetime(2026, 8, 6, tzinfo=timezone.utc),
                content_sha256="1" * 64,
                membership_sha256="2" * 64,
                artifact_uri="file:///tmp/universe.jsonl",
                artifact_format="jsonl-v1",
                artifact_sha256="3" * 64,
                artifact_bytes=1,
                row_count=1,
                unique_market_count=1,
                open_market_count=1,
                normalization_policy_version="normalization-v1",
                validation_policy_version="validation-v1",
                source_versions={},
                manifest={},
                created_by_job_id=submitted.job_id,
            )
        )
        session.commit()
        session.add(
            ActiveMarketUniverse(
                provider="other-provider",
                venue="polymarket",
                snapshot_id=snapshot_id,
                generation=1,
                promoted_by_job_id=submitted.job_id,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
