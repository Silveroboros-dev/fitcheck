"""Bounded admission for the public PostgreSQL fixture deployment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.orm import Session

WRITE_DEADLINE_ENV = "FITCHECK_DEMO_WRITE_DEADLINE_UTC"
# One explicitly dated hosted-pilot exception; ordinary callers retain 48 hours.
WEEKLY_PILOT_DEADLINE = datetime(2026, 10, 25, 18, 30, tzinfo=timezone.utc)
MAX_REQUEST_BYTES = 128 * 1024
MAX_ACTIVE_SESSIONS = 100
MAX_LIFETIME_SESSIONS = 1000
MAX_ACTIVE_SOURCE_JOBS = 25
MAX_LIFETIME_SOURCE_JOBS = 250


class DemoAdmissionDenied(RuntimeError):
    def __init__(self, reason: str, status_code: int = 429):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


@lru_cache(maxsize=1)
def _supported_sources() -> frozenset[str]:
    from el.product.wiring import _claim_fixtures, _source_fixtures
    return frozenset(_source_fixtures(_claim_fixtures()))


@dataclass(frozen=True)
class DemoAdmission:
    write_deadline: datetime

    def __post_init__(self) -> None:
        if (self.write_deadline.tzinfo is None
            or self.write_deadline.utcoffset() != timedelta(0)):
            raise ValueError("fixture write deadline must be UTC")

    @classmethod
    def from_env(cls, *, allow_weekly_pilot: bool = False) -> "DemoAdmission":
        raw = os.environ.get(WRITE_DEADLINE_ENV)
        if not raw:
            raise ValueError(f"{WRITE_DEADLINE_ENV} is required")
        try:
            deadline = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            admission = cls(deadline)
            if (deadline > datetime.now(timezone.utc) + timedelta(hours=48)
                and not (allow_weekly_pilot is True
                         and deadline == WEEKLY_PILOT_DEADLINE)):
                raise ValueError("fixture write deadline exceeds 48 hours")
            return admission
        except ValueError:
            raise ValueError(f"{WRITE_DEADLINE_ENV} must be a UTC timestamp") from None

    def require_writable(self, now: datetime | None = None) -> None:
        if (now or datetime.now(timezone.utc)) >= self.write_deadline:
            raise DemoAdmissionDenied("write_window_closed", 403)

    def require_source(self, input_text: str, source_url: str | None) -> None:
        if source_url is not None or input_text not in _supported_sources():
            raise DemoAdmissionDenied("unsupported_fixture_source", 422)

    def lock(self, session: Session) -> None:
        """Serialize all public fixture admissions across web processes.

        The lock is transaction scoped. Counts and inserts must use this same
        session, and the caller must commit or roll back before releasing it.
        """
        if session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(172945719, 1)"))
        elif session.bind.dialect.name == "sqlite":
            # Offline tests use a disposable SQLite database. Cloud admission
            # requires PostgreSQL; SQLite is not a production concurrency gate.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        else:
            raise RuntimeError("fixture admission requires PostgreSQL")

    def require_transaction_writable(self, session: Session) -> datetime:
        if session.bind.dialect.name == "postgresql":
            now = session.execute(text("SELECT clock_timestamp()")).scalar_one()
        else:
            now = datetime.now(timezone.utc)
        self.require_writable(now)
        return now
