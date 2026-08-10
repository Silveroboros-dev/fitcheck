"""FastMCP transport smoke (step 7 C2).

Tool LOGIC is covered exhaustively in test_mcp_tools; this asserts the thin
transport wires up — all 10 tools register, and one round-trips through
FastMCP's call_tool with an injected (stub) principal resolver.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace

from test_mcp_tools import CLEAN, _principal, _sessions, _tools

from el.domain.tables import ApiClient, User
from el.mcp.auth import hash_api_key
from el.mcp.contracts import NotFound
from el.mcp.server import (
    TOOL_NAMES,
    _fail,
    build_server,
    header_principal_resolver,
)


def _server():
    sessions = _sessions()
    tools = _tools(sessions)
    principal = _principal(sessions)
    # Stub resolver: the transport's job is dispatch; auth resolution is
    # exercised in test_mcp_auth. Here we inject a fixed principal.
    return build_server(tools, lambda ctx: principal), sessions


def test_server_registers_all_ten_tools():
    mcp, _ = _server()
    names = sorted(t.name for t in asyncio.run(mcp.list_tools()))
    assert names == sorted(TOOL_NAMES)
    assert len(names) == 10


def test_normalize_claim_round_trips_through_transport():
    mcp, _ = _server()
    result = asyncio.run(mcp.call_tool("normalize_claim", {"input_text": CLEAN}))
    # FastMCP serializes the dict return as a TextContent JSON block.
    payload = result[0] if isinstance(result, (list, tuple)) else result
    text = getattr(payload, "text", None)
    data = json.loads(text) if text else payload
    assert "thesis_analysis_id" in data
    assert data["normalized_claim_summary"]


def test_header_resolver_parses_bearer_token():
    # P2a: parse "Authorization: Bearer <key>", not the whole header string.
    sessions = _sessions()
    with sessions() as s:
        u = User(email=f"u-{uuid.uuid4()}@example.com")
        s.add(u)
        s.flush()
        s.add(
            ApiClient(
                user_id=u.id,
                key_hash=hash_api_key("sekret"),
                client_type="agent_mcp",
                rate_limit_tier="default",
            )
        )
        s.commit()
    resolver = header_principal_resolver(sessions)
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"authorization": "Bearer sekret"})
        )
    )
    principal = resolver(ctx)
    assert principal.client_type.value == "agent_mcp"


def test_fail_maps_errors_to_typed_codes():
    # P2b: UUID/enum ValueErrors -> invalid_argument; McpError keeps its code.
    assert "invalid_argument" in str(_fail(ValueError("not a uuid")))
    assert "not_found" in str(_fail(NotFound("ledger_entry")))
