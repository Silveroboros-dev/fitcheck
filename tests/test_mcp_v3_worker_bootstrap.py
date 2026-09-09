"""The production MCP worker must share core DB and Gemini-v3 execution pins."""

from __future__ import annotations

import uuid

from el.domain.tables import Base
from el.mcp.wiring import build_gemini_v3_tools, seed_principal
from el.sourceinterpretation.worker import build_worker
from el.product.wiring import MULTI_THESIS_FIXTURE, make_session_factory


def test_mcp_worker_matches_queued_gemini_job_before_any_model_call(
    tmp_path, monkeypatch
):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mcp-worker.db'}"
    monkeypatch.setenv("FITCHECK_DB_URL", db_url)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FITCHECK_UI_DB_URL", raising=False)

    # This replaces Alembic only inside the disposable unit-test database.
    sessions = make_session_factory(db_url)
    Base.metadata.create_all(sessions.kw["bind"])
    principal = seed_principal(sessions, f"worker-bootstrap-{uuid.uuid4()}")
    submitted_tools = build_gemini_v3_tools(sessions)
    queued = submitted_tools.submit_source_interpretation(
        principal,
        input_text=MULTI_THESIS_FIXTURE,
        source_url=None,
        idempotency_key="mcp-worker-bootstrap",
    )

    worker = build_worker(
        surface="mcp", worker_id="mcp-bootstrap-test", lease_seconds=90
    )
    assert str(worker._sessions.kw["bind"].url) == db_url
    assert worker._jobs.get(queued.job_id).id == queued.job_id
    assert worker._source.execution_pins() == submitted_tools._source.execution_pins()
