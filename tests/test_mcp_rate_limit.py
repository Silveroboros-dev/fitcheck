"""SQLite contract tests for atomic, bounded MCP usage admission."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from el.domain.tables import ApiClient, Base, McpUsageBucket, User
from el.mcp.auth import hash_api_key, resolve_principal
from el.mcp.rate_limit import (
    DAY_SCOPE,
    MINUTE_SCOPE,
    TOOL_COST_UNITS,
    RateLimitExceeded,
    SqlUsageLimiter,
    TierPolicy,
    UsageLimiterUnavailable,
)
from el.mcp.server import ALL_TOOL_NAMES

UTC = timezone.utc


def _harness(tmp_path, *, tier: str = "test"):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'usage.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_safety(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    raw_key = f"key-{uuid.uuid4()}"
    with sessions.begin() as session:
        user = User(email=f"u-{uuid.uuid4()}@example.test")
        session.add(user)
        session.flush()
        session.add(
            ApiClient(
                user_id=user.id,
                key_hash=hash_api_key(raw_key),
                client_type="agent_mcp",
                rate_limit_tier=tier,
            )
        )
    with sessions() as session:
        principal = resolve_principal(session, raw_key)
    return engine, sessions, principal


def _limiter(sessions, now, *, minute=10, day=10):
    return SqlUsageLimiter(
        sessions,
        policies={"test": TierPolicy(minute, day)},
        tool_costs={"probe": 1},
        clock=lambda _session: now[0],
    )


def test_tool_weights_cover_the_registered_surface():
    assert frozenset(ALL_TOOL_NAMES) == frozenset(TOOL_COST_UNITS)
    assert all(
        isinstance(cost, int) and cost > 0
        for cost in TOOL_COST_UNITS.values()
    )


def test_parallel_calls_never_overshoot_either_window(tmp_path):
    engine, sessions, principal = _harness(tmp_path)
    now = [datetime(2026, 8, 11, 12, 0, 5, tzinfo=UTC)]
    limiter = _limiter(sessions, now, minute=10, day=10)
    workers = 32
    barrier = threading.Barrier(workers)

    def consume(_index: int) -> bool:
        barrier.wait(timeout=10)
        try:
            limiter.consume(principal, "probe")
            return True
        except RateLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        allowed = list(pool.map(consume, range(workers)))

    assert sum(allowed) == 10
    with sessions() as session:
        rows = session.scalars(
            select(McpUsageBucket).where(
                McpUsageBucket.api_client_id == principal.api_client_id
            )
        ).all()
    assert {row.scope: row.used_units for row in rows} == {
        MINUTE_SCOPE: 10,
        DAY_SCOPE: 10,
    }
    engine.dispose()


def test_later_scope_denial_rolls_back_earlier_charge(tmp_path):
    engine, sessions, principal = _harness(tmp_path)
    now = [datetime(2026, 8, 11, 12, 0, 5, tzinfo=UTC)]
    limiter = _limiter(sessions, now, minute=2, day=1)

    limiter.consume(principal, "probe")
    with pytest.raises(RateLimitExceeded):
        limiter.consume(principal, "probe")

    with sessions() as session:
        rows = session.scalars(
            select(McpUsageBucket).where(
                McpUsageBucket.api_client_id == principal.api_client_id
            )
        ).all()
    assert {row.scope: row.used_units for row in rows} == {
        MINUTE_SCOPE: 1,
        DAY_SCOPE: 1,
    }
    engine.dispose()


def test_window_rollover_overwrites_rows_instead_of_growing_storage(tmp_path):
    engine, sessions, principal = _harness(tmp_path)
    now = [datetime(2026, 8, 11, 0, 0, 1, tzinfo=UTC)]
    limiter = _limiter(sessions, now, minute=1, day=1_000)

    for _ in range(100):
        limiter.consume(principal, "probe")
        now[0] += timedelta(seconds=61)

    with sessions() as session:
        row_count = session.scalar(
            select(func.count())
            .select_from(McpUsageBucket)
            .where(McpUsageBucket.api_client_id == principal.api_client_id)
        )
    assert row_count == 2
    engine.dispose()


def test_unknown_tier_tool_and_clock_failure_all_fail_closed(tmp_path):
    engine, sessions, principal = _harness(tmp_path)
    now = [datetime(2026, 8, 11, 12, 0, 5, tzinfo=UTC)]
    limiter = _limiter(sessions, now)

    with pytest.raises(UsageLimiterUnavailable):
        limiter.consume(replace(principal, rate_limit_tier="unknown"), "probe")
    with pytest.raises(UsageLimiterUnavailable):
        limiter.consume(principal, "unknown_tool")

    broken = SqlUsageLimiter(
        sessions,
        policies={"test": TierPolicy(10, 10)},
        tool_costs={"probe": 1},
        clock=lambda _session: (_ for _ in ()).throw(
            RuntimeError("clock down")
        ),
    )
    with pytest.raises(UsageLimiterUnavailable, match="temporarily unavailable"):
        broken.consume(principal, "probe")

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(McpUsageBucket)) == 0
    engine.dispose()


def test_future_stored_window_does_not_reset_backwards(tmp_path):
    engine, sessions, principal = _harness(tmp_path)
    now = [datetime(2026, 8, 11, 12, 1, 5, tzinfo=UTC)]
    limiter = _limiter(sessions, now, minute=10, day=10)
    limiter.consume(principal, "probe")

    now[0] -= timedelta(minutes=2)
    with pytest.raises(RateLimitExceeded):
        limiter.consume(principal, "probe")
    engine.dispose()
