"""Engine/session helpers.

DATABASE_URL drives everything: Postgres in prod, SQLite for dev/tests.
FITCHECK_DB_URL is an accepted alias; the two must never disagree (see
``database_url``), so the MCP server and Alembic migrations can never
split-brain onto different databases.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_URL = "sqlite:///fitcheck.db"


def database_url() -> str:
    """The single, fail-closed DB-URL resolver shared by the core, the MCP
    server, the demo, and Alembic migrations — so nothing can point at a
    different database than the migrations ran against.

    Policy:
      - FITCHECK_DB_URL and DATABASE_URL both set and DIFFER -> fail closed
        (SystemExit), rather than silently let server and migrations diverge.
      - either one alone -> use it.
      - both set and equal -> use it.
      - neither set -> ``DEFAULT_URL``.
    A blank/whitespace value counts as unset.
    """
    fitcheck = (os.environ.get("FITCHECK_DB_URL") or "").strip()
    database = (os.environ.get("DATABASE_URL") or "").strip()
    if fitcheck and database and fitcheck != database:
        raise SystemExit(
            "FITCHECK_DB_URL and DATABASE_URL are both set but differ "
            f"({fitcheck!r} vs {database!r}). They must name the SAME database "
            "(the server and Alembic migrations share it). Unset one, or set "
            "them equal."
        )
    return fitcheck or database or DEFAULT_URL


def make_engine(url: str | None = None):
    # Cloud SQL maintenance and configuration changes can invalidate pooled
    # sockets while a long-lived Cloud Run instance remains healthy. Validate
    # a connection before handing it to a request so SQLAlchemy transparently
    # replaces stale connections instead of pinning the service in an
    # OperationalError loop until the container is restarted.
    return create_engine(url or database_url(), pool_pre_ping=True)


def make_session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=make_engine(url), expire_on_commit=False)
