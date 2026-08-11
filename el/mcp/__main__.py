"""Production MCP server entrypoint: ``python -m el.mcp``.

Streamable HTTP on ``0.0.0.0:$PORT``, stateless (Cloud Run routes to
0.0.0.0:$PORT and autoscales across instances with no shared session state),
Gemini proposers, api-key header auth. Alembic owns the production schema —
this entrypoint deliberately does NOT ``create_all`` (entrypoint.sh runs
``alembic upgrade head`` first). The DB URL is resolved fail-closed by
``el.domain.db.database_url()``.
"""

import os

from sqlalchemy.orm import sessionmaker

from el.domain.db import make_engine
from el.mcp.rate_limit import SqlUsageLimiter
from el.mcp.server import build_server, header_principal_resolver
from el.mcp.wiring import build_gemini_tools


def build():
    """Construct (but do not run) the production server. NO create_all —
    Alembic owns the schema. Split out so it can be smoke-tested without
    binding a socket."""
    engine = make_engine()  # database_url(): fail-closed FITCHECK_DB_URL/DATABASE_URL
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    tools = build_gemini_tools(sessions)
    limiter = SqlUsageLimiter(sessions)
    header = os.environ.get("FITCHECK_API_KEY_HEADER", "x-api-key")
    resolve = header_principal_resolver(sessions, header=header)
    return build_server(
        tools,
        resolve,
        limiter.consume,
        name="fitcheck",
        host="0.0.0.0",
        port=int(os.environ["PORT"]),
        stateless_http=True,
    )


def main() -> None:
    build().run(transport="streamable-http")


if __name__ == "__main__":
    main()
