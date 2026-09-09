"""Atomic, cross-instance MCP usage admission backed by the application DB.

The limiter deliberately uses two fixed, weighted windows rather than an
event log or process-local counters. PostgreSQL is the production authority;
SQLite implements the same statement for deterministic local tests and the
fixture demo. Each client retains exactly two rows regardless of call volume.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from el.domain.tables import McpUsageBucket
from el.mcp.auth import Principal
from el.mcp.contracts import McpError

MINUTE_SCOPE = "weighted_60s"
DAY_SCOPE = "weighted_utc_day"


@dataclass(frozen=True)
class TierPolicy:
    weighted_60s: int
    weighted_utc_day: int


TIER_POLICIES: Mapping[str, TierPolicy] = {
    "default": TierPolicy(weighted_60s=60, weighted_utc_day=600),
}

# Weights are protective units, not token counts or currency. Every tool costs
# at least one unit, so the weighted minute window also caps raw request rate.
TOOL_COST_UNITS: Mapping[str, int] = {
    "normalize_claim": 10,
    "preview_market_fit": 20,
    "draft_contract_preview": 10,
    "submit_blind_prior": 1,
    "classify_market_fit": 20,
    "create_ledger_entry": 2,
    "get_ledger_entry": 1,
    "get_ledger_entries": 2,
    "correct_fit": 2,
    "reject_market": 2,
    # V3 source-to-market journey. These are fixed protective units, reviewed
    # together with the registered server surface; omitting a tool fails closed.
    "v3_submit_source_interpretation": 10,
    "v3_get_source_interpretation_job": 1,
    "v3_get_source_interpretation_job_by_idempotency": 1,
    "v3_choose_source_candidate": 2,
    "v3_propose_selected_normalization": 10,
    "v3_revise_normalization": 10,
    "v3_accept_normalization": 2,
    "v3_reject_normalization": 2,
    "v3_assess_market_pool": 20,
    "v3_choose_market": 2,
}


class RateLimitExceeded(McpError):
    code = "rate_limited"

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"usage limit reached; retry after {retry_after_seconds}s")


class UsageLimiterUnavailable(McpError):
    code = "temporarily_unavailable"

    def __init__(self):
        super().__init__("usage accounting is temporarily unavailable")


Clock = Callable[[Session], datetime]


class SqlUsageLimiter:
    """Consume weighted MCP units atomically before a tool is invoked."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        policies: Mapping[str, TierPolicy] = TIER_POLICIES,
        tool_costs: Mapping[str, int] = TOOL_COST_UNITS,
        clock: Clock | None = None,
    ):
        self._sessions = session_factory
        self._policies = policies
        self._tool_costs = tool_costs
        self._clock = clock or self._database_now

    def consume(self, principal: Principal, tool_name: str) -> None:
        policy = self._policies.get(principal.rate_limit_tier)
        cost = self._tool_costs.get(tool_name)
        if policy is None or not self._valid_positive(cost):
            # A typo or unratified tier/tool must never become an implicit
            # unlimited tier.
            raise UsageLimiterUnavailable()

        scopes = (
            (MINUTE_SCOPE, 60, policy.weighted_60s),
            (DAY_SCOPE, 86_400, policy.weighted_utc_day),
        )
        if any(
            not self._valid_positive(limit) or cost > limit
            for _, _, limit in scopes
        ):
            raise UsageLimiterUnavailable()

        try:
            with self._sessions() as session:
                with session.begin():
                    now = self._aware(self._clock(session))
                    # Stable ordering prevents two callers from locking the
                    # minute/day rows in opposite order on PostgreSQL.
                    for scope, period_seconds, limit in sorted(scopes):
                        window_start = self._window_start(now, period_seconds)
                        allowed = self._charge_scope(
                            session,
                            principal=principal,
                            scope=scope,
                            window_start=window_start,
                            now=now,
                            cost=cost,
                            limit=limit,
                        )
                        if not allowed:
                            retry_after = max(
                                1,
                                math.ceil(
                                    (
                                        window_start
                                        + timedelta(seconds=period_seconds)
                                        - now
                                    ).total_seconds()
                                ),
                            )
                            raise RateLimitExceeded(retry_after)
        except RateLimitExceeded:
            # Raising inside session.begin() rolls back a charge made to an
            # earlier scope in the same admission decision.
            raise
        except UsageLimiterUnavailable:
            raise
        except (SQLAlchemyError, RuntimeError, TypeError, ValueError):
            # Do not surface SQL, connection data, clock values, or policy
            # internals to an MCP client. A failed accounting write denies the
            # tool call rather than failing open.
            raise UsageLimiterUnavailable() from None

    @staticmethod
    def _charge_scope(
        session: Session,
        *,
        principal: Principal,
        scope: str,
        window_start: datetime,
        now: datetime,
        cost: int,
        limit: int,
    ) -> bool:
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            insert = postgresql_insert(McpUsageBucket)
        elif dialect == "sqlite":
            insert = sqlite_insert(McpUsageBucket)
        else:
            raise RuntimeError(f"unsupported usage-limiter dialect: {dialect}")

        table = McpUsageBucket.__table__
        statement = insert.values(
            api_client_id=principal.api_client_id,
            scope=scope,
            window_started_at=window_start,
            used_units=cost,
            updated_at=now,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.api_client_id, table.c.scope],
            set_={
                "window_started_at": window_start,
                "used_units": case(
                    (table.c.window_started_at < window_start, cost),
                    else_=table.c.used_units + cost,
                ),
                "updated_at": now,
            },
            where=or_(
                table.c.window_started_at < window_start,
                and_(
                    table.c.window_started_at == window_start,
                    table.c.used_units <= limit - cost,
                ),
            ),
        ).returning(table.c.used_units)
        return session.execute(statement).scalar_one_or_none() is not None

    @staticmethod
    def _window_start(now: datetime, period_seconds: int) -> datetime:
        epoch = int(now.timestamp())
        return datetime.fromtimestamp(
            epoch - (epoch % period_seconds), tz=timezone.utc
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if not isinstance(value, datetime):
            raise RuntimeError("database did not return a timestamp")
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _database_now(session: Session) -> datetime:
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            clock = func.clock_timestamp()
        elif dialect == "sqlite":
            clock = func.now()
        else:
            raise RuntimeError(f"unsupported usage-limiter dialect: {dialect}")
        value = session.scalar(select(clock))
        if not isinstance(value, datetime):
            raise RuntimeError("database did not return a timestamp")
        return value

    @staticmethod
    def _valid_positive(value: object) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
        )
