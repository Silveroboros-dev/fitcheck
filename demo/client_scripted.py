"""Deterministic MCP demo client.

Runs the canonical FitCheck flow with no LLM. By default it spawns the local
demo server over stdio. Set FITCHECK_MCP_URL to call an already-running
Streamable HTTP server instead.
"""

import asyncio
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from demo.cloud_run_auth import CloudRunAuthError, remote_mcp_headers

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLEAN = (
    "Gemini is going to be ranked #1 chatbot on LMSYS Chatbot Arena by "
    "the end of 2026."
)


def _json_payload(result: Any) -> Any:
    content = getattr(result, "content", result)
    if isinstance(content, (list, tuple)):
        if not content:
            return None
        content = content[0]
    text = getattr(content, "text", None)
    if text is None:
        return content
    return json.loads(text)


def _print_step(name: str, payload: Any) -> None:
    print(f"\n== {name} ==")
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


@asynccontextmanager
async def _stdio_session(api_key: str):
    env = {
        **os.environ,
        "FITCHECK_API_KEY": api_key,
        "FITCHECK_PROPOSER": os.environ.get("FITCHECK_PROPOSER", "fixture"),
        "FITCHECK_MCP_TRANSPORT": "stdio",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "demo.server"],
        env=env,
        cwd=str(PROJECT_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@asynccontextmanager
async def _http_session(api_key: str, url: str, transport: str):
    try:
        headers = remote_mcp_headers(
            api_key=api_key,
            api_key_header=os.environ.get("FITCHECK_API_KEY_HEADER", "x-api-key"),
            endpoint_url=url,
        )
    except CloudRunAuthError as exc:
        raise SystemExit(f"Remote MCP authentication error: {exc}") from exc
    if transport == "sse":
        async with sse_client(url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


@asynccontextmanager
async def _session(api_key: str):
    url = os.environ.get("FITCHECK_MCP_URL")
    if url:
        transport = os.environ.get("FITCHECK_MCP_CLIENT_TRANSPORT", "streamable-http")
        if transport not in {"streamable-http", "sse"}:
            raise SystemExit(
                "FITCHECK_MCP_CLIENT_TRANSPORT must be streamable-http or sse"
            )
        async with _http_session(api_key, url, transport) as session:
            yield session
    else:
        async with _stdio_session(api_key) as session:
            yield session


async def run() -> None:
    api_key = os.environ.get("FITCHECK_API_KEY") or f"demo-{uuid.uuid4()}"
    async with _session(api_key) as session:
        tools = await session.list_tools()
        tool_names = sorted(tool.name for tool in tools.tools)
        _print_step("list_tools", tool_names)

        norm = _json_payload(
            await session.call_tool("normalize_claim", {"input_text": CLEAN})
        )
        _print_step("normalize_claim", norm)

        preview = _json_payload(
            await session.call_tool(
                "preview_market_fit",
                {"thesis_analysis_id": norm["thesis_analysis_id"]},
            )
        )
        _print_step("preview_market_fit", preview)
        assert preview["current_odds"] is None, "preview must withhold odds"

        prior = _json_payload(
            await session.call_tool(
                "submit_blind_prior",
                {
                    "thesis_analysis_id": norm["thesis_analysis_id"],
                    "prior_probability": 0.7,
                    "prior_confidence": "medium",
                    "prior_reason": "Leaderboard momentum before seeing odds.",
                },
            )
        )
        _print_step("submit_blind_prior", prior)

        card = _json_payload(
            await session.call_tool(
                "classify_market_fit",
                {"thesis_analysis_id": norm["thesis_analysis_id"]},
            )
        )
        _print_step("classify_market_fit", card)
        assert card["current_odds"] is not None, "classify must reveal odds"

        saved = _json_payload(
            await session.call_tool(
                "create_ledger_entry",
                {
                    "fit_card_id": card["fit_card_id"],
                    "conviction_level": "leaning",
                    "intended_exposure_bucket": "$100",
                    "user_justification": "Strong leaderboard momentum.",
                },
            )
        )
        _print_step("create_ledger_entry", saved)

        entries = _json_payload(await session.call_tool("get_ledger_entries", {}))
        if not isinstance(entries, list):
            entries = [entries]
        _print_step("get_ledger_entries", entries)
        assert entries, "saved ledger entry should be listed"

        # Submit ONE synthetic correction so the demo also produces Step-8 fuel
        # (a review_candidate), not just a saved ledger entry. Tagged SYNTHETIC
        # so a real review queue never reads it as organic agent evidence.
        synthetic_note = "[SYNTHETIC SMOKE] demo-generated; not user evidence"
        correction = _json_payload(
            await session.call_tool(
                "correct_fit",
                {
                    "fit_card_id": card["fit_card_id"],
                    "corrected_class": "indirect",
                    "notes": synthetic_note,
                },
            )
        )
        _print_step("correct_fit", correction)
        assert correction["source"] == "agent_correction", (
            "an MCP agent correction must be tagged agent_correction"
        )
        assert correction["review_candidate_id"], "a review candidate must exist"
        assert correction["status"] == "pending", "a new candidate is pending review"

        # Idempotency (P1): an identical re-submission returns the SAME
        # candidate id — no duplicate fuel.
        again = _json_payload(
            await session.call_tool(
                "correct_fit",
                {
                    "fit_card_id": card["fit_card_id"],
                    "corrected_class": "indirect",
                    "notes": synthetic_note,
                },
            )
        )
        assert again["review_candidate_id"] == correction["review_candidate_id"], (
            "a repeated identical correction must be idempotent"
        )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
