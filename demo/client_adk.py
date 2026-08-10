"""Google ADK Gemini client for the FitCheck MCP demo server."""

import asyncio
import os
import sys
import uuid
from pathlib import Path

from google.genai import types
from mcp import StdioServerParameters

from demo.cloud_run_auth import CloudRunAuthError, remote_mcp_headers

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLEAN = (
    "Gemini is going to be ranked #1 chatbot on LMSYS Chatbot Arena by "
    "the end of 2026."
)

INSTRUCTION = """\
You are a FitCheck demo client consuming a remote MCP server. Use the MCP
tools; do not invent FitCheck results.

Canonical flow:
1. normalize_claim
2. preview_market_fit
3. submit_blind_prior
4. classify_market_fit
5. create_ledger_entry
6. get_ledger_entries

Protocol:
- preview_market_fit must keep current odds hidden.
- submit_blind_prior must happen before classify_market_fit.
- classify_market_fit may then show thesis-side odds.
- FitCheck classifies expression fit only: direct, indirect, weak_proxy, or
  no_clean_expression.
- Do not recommend financial action or position sizing.
"""


def _require_api_key() -> None:
    vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower()
    if vertex in {"1", "true", "yes"}:
        return
    if not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")):
        raise SystemExit(
            "client_adk requires GOOGLE_API_KEY or GEMINI_API_KEY "
            "(or GOOGLE_GENAI_USE_VERTEXAI=1 for Vertex auth)."
        )


def _connection_params(api_key: str):
    from google.adk.tools.mcp_tool import (
        SseConnectionParams,
        StdioConnectionParams,
        StreamableHTTPConnectionParams,
    )

    url = os.environ.get("FITCHECK_MCP_URL")
    header = os.environ.get("FITCHECK_API_KEY_HEADER", "x-api-key")
    if url:
        transport = os.environ.get(
            "FITCHECK_MCP_CLIENT_TRANSPORT", "streamable-http"
        )
        try:
            headers = remote_mcp_headers(
                api_key=api_key,
                api_key_header=header,
                endpoint_url=url,
            )
        except CloudRunAuthError as exc:
            raise SystemExit(f"Remote MCP authentication error: {exc}") from exc
        if transport == "streamable-http":
            return StreamableHTTPConnectionParams(url=url, headers=headers)
        if transport == "sse":
            return SseConnectionParams(url=url, headers=headers)
        raise SystemExit(
            "FITCHECK_MCP_CLIENT_TRANSPORT must be streamable-http or sse"
        )

    env = {
        **os.environ,
        "FITCHECK_API_KEY": api_key,
        "FITCHECK_PROPOSER": os.environ.get("FITCHECK_PROPOSER", "fixture"),
        "FITCHECK_MCP_TRANSPORT": "stdio",
    }
    return StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=["-m", "demo.server"],
            env=env,
            cwd=str(PROJECT_ROOT),
        )
    )


def _event_text(event) -> str | None:
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) if content is not None else None
    if not parts:
        return None
    chunks = [getattr(part, "text", None) for part in parts]
    text = "".join(chunk for chunk in chunks if chunk)
    return text or None


async def run() -> None:
    try:
        from google.adk.agents import LlmAgent
        from google.adk.runners import InMemoryRunner
        from google.adk.tools.mcp_tool import McpToolset
    except ImportError as exc:
        raise SystemExit(
            "client_adk requires the demo extra: pip install -e '.[demo]'"
        ) from exc

    _require_api_key()
    api_key = os.environ.get("FITCHECK_API_KEY") or f"demo-{uuid.uuid4()}"
    model = os.environ.get("FITCHECK_ADK_MODEL", "gemini-2.5-flash")
    toolset = McpToolset(connection_params=_connection_params(api_key))
    agent = LlmAgent(
        model=model,
        name="fitcheck_demo",
        instruction=INSTRUCTION,
        tools=[toolset],
    )
    runner = InMemoryRunner(agent=agent, app_name="fitcheck-demo")
    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=(
                    "Run the canonical FitCheck flow for this thesis and "
                    "summarize the Market Fit Card and saved ledger entry: "
                    f"{CLEAN}"
                )
            )
        ],
    )

    try:
        async for event in runner.run_async(
            user_id="demo-user",
            session_id=f"demo-{uuid.uuid4()}",
            new_message=message,
        ):
            text = _event_text(event)
            if text:
                print(text)
    finally:
        await runner.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
