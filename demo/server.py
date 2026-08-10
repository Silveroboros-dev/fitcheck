"""Runnable FitCheck MCP demo server.

Default mode is stdio + fixture proposers so the demo is deterministic and
does not spend model tokens. Streamable HTTP is the deploy-compatible path;
SSE is exposed only as compatibility glue for clients that force it.
"""

import os
import sys
import uuid

from sqlalchemy.orm import sessionmaker

from demo.build import build_fixture_tools, build_gemini_tools, seed_principal
from el.domain.db import database_url, make_engine
from el.domain.tables import Base
from el.mcp.server import build_server, header_principal_resolver

VALID_PROPOSERS = {"fixture", "gemini"}
VALID_TRANSPORTS = {"stdio", "streamable-http", "sse"}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


def _api_key_config() -> tuple[str, str]:
    """Return the runtime key and a log-safe description of its source."""

    configured = os.environ.get("FITCHECK_API_KEY")
    if configured:
        return configured, "configured"
    return f"demo-{uuid.uuid4()}", "generated"


def _runtime_log_line(
    *, transport: str, proposer: str, database_backend: str, api_key_source: str
) -> str:
    """Build startup metadata from fields that cannot contain credentials."""

    return (
        "fitcheck-demo "
        f"transport={transport} proposer={proposer} "
        f"db_backend={database_backend} api_key_source={api_key_source}"
    )


def main() -> None:
    # database_url() is the shared fail-closed resolver (FITCHECK_DB_URL /
    # DATABASE_URL must agree); the demo create_all's its own schema below.
    db_url = database_url()
    proposer = _env("FITCHECK_PROPOSER", "fixture")
    transport = _env("FITCHECK_MCP_TRANSPORT", "stdio")
    api_key, api_key_source = _api_key_config()
    header = _env("FITCHECK_API_KEY_HEADER", "x-api-key")

    if proposer not in VALID_PROPOSERS:
        raise SystemExit(
            f"FITCHECK_PROPOSER must be one of {sorted(VALID_PROPOSERS)}"
        )
    if transport not in VALID_TRANSPORTS:
        raise SystemExit(
            f"FITCHECK_MCP_TRANSPORT must be one of {sorted(VALID_TRANSPORTS)}"
        )

    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    tools = build_gemini_tools(sessions) if proposer == "gemini" else build_fixture_tools(sessions)
    principal = seed_principal(sessions, api_key)

    if transport == "stdio":
        resolve = lambda ctx: principal
    else:
        resolve = header_principal_resolver(sessions, header=header)

    print(
        _runtime_log_line(
            transport=transport,
            proposer=proposer,
            database_backend=engine.dialect.name,
            api_key_source=api_key_source,
        ),
        file=sys.stderr,
        flush=True,
    )
    if transport == "streamable-http":
        print(
            "fitcheck-demo streamable_http_url=http://127.0.0.1:8000/mcp",
            file=sys.stderr,
            flush=True,
        )
    elif transport == "sse":
        print(
            "fitcheck-demo sse_url=http://127.0.0.1:8000/sse",
            file=sys.stderr,
            flush=True,
        )

    mcp = build_server(tools, resolve, name="fitcheck-demo")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
