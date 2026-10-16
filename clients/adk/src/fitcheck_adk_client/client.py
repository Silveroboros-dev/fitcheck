"""Google ADK client for a separately installed FitCheck MCP server."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import os
from pathlib import Path
import re
import uuid

from google.genai import types
from mcp import StdioServerParameters

from fitcheck_adk_client.auth import CloudRunAuthError, remote_mcp_headers


CLEAN = (
    "Gemini is going to be ranked #1 chatbot on LMSYS Chatbot Arena by "
    "the end of 2026."
)
INSTRUCTION = """\
You are a FitCheck demo client consuming an MCP server. Use the MCP tools; do
not invent FitCheck results.

Canonical predecessor flow:
1. normalize_claim
2. preview_market_fit
3. submit_blind_prior
4. classify_market_fit
5. create_ledger_entry
6. get_ledger_entries

Keep current odds hidden until the blind prior is submitted. FitCheck
classifies expression fit only. Do not recommend financial action or position
sizing.
"""

_SERVER_ENV_PREFIX = "FITCHECK_SERVER_ENV_"
_SERVER_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_CLIENT_MANAGED_SERVER_ENV = frozenset(
    {"FITCHECK_API_KEY", "FITCHECK_MCP_TRANSPORT"}
)


def _require_api_key(environ: Mapping[str, str] | None = None) -> None:
    env = os.environ if environ is None else environ
    if env.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {"1", "true", "yes"}:
        return
    if not (env.get("GOOGLE_API_KEY") or env.get("GEMINI_API_KEY")):
        raise SystemExit(
            "fitcheck-adk-client requires GOOGLE_API_KEY or GEMINI_API_KEY "
            "(or GOOGLE_GENAI_USE_VERTEXAI=1 for Vertex auth)."
        )


def _stdio_server_environment(
    env: Mapping[str, str],
    *,
    api_key: str,
) -> dict[str, str]:
    """Build the explicit environment boundary for the local MCP server."""

    server_env: dict[str, str] = {}
    for name, value in env.items():
        if not name.startswith(_SERVER_ENV_PREFIX):
            continue
        server_name = name.removeprefix(_SERVER_ENV_PREFIX)
        if _SERVER_ENV_NAME.fullmatch(server_name) is None:
            raise SystemExit(
                "FITCHECK_SERVER_ENV_ names must end with a valid uppercase "
                "environment variable name."
            )
        if server_name in _CLIENT_MANAGED_SERVER_ENV:
            raise SystemExit(
                f"FITCHECK_SERVER_ENV_{server_name} cannot override the "
                "client-managed local server contract."
            )
        server_env[server_name] = value
    proposer = server_env.pop(
        "FITCHECK_PROPOSER",
        env.get("FITCHECK_PROPOSER", "fixture"),
    )
    server_env.update(
        {
            "FITCHECK_API_KEY": api_key,
            "FITCHECK_PROPOSER": proposer,
            "FITCHECK_MCP_TRANSPORT": "stdio",
        }
    )
    return server_env


def _connection_params(
    api_key: str,
    *,
    environ: Mapping[str, str] | None = None,
):
    from google.adk.tools.mcp_tool import (
        SseConnectionParams,
        StdioConnectionParams,
        StreamableHTTPConnectionParams,
    )

    env = os.environ if environ is None else environ
    url = env.get("FITCHECK_MCP_URL")
    header = env.get("FITCHECK_API_KEY_HEADER", "x-api-key")
    if url:
        transport = env.get("FITCHECK_MCP_CLIENT_TRANSPORT", "streamable-http")
        try:
            headers = remote_mcp_headers(
                api_key=api_key,
                api_key_header=header,
                endpoint_url=url,
                environ=env,
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

    server_python = env.get("FITCHECK_SERVER_PYTHON")
    if not server_python:
        raise SystemExit(
            "Local STDIO requires FITCHECK_SERVER_PYTHON to point to the "
            "separate FitCheck server environment."
        )
    server_root = str(Path(env.get("FITCHECK_SERVER_ROOT", os.getcwd())).resolve())
    server_env = _stdio_server_environment(env, api_key=api_key)
    return StdioConnectionParams(
        server_params=StdioServerParameters(
            command=server_python,
            args=["-m", "demo.server"],
            env=server_env,
            cwd=server_root,
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
    from google.adk.agents import LlmAgent
    from google.adk.runners import InMemoryRunner
    from google.adk.tools.mcp_tool import McpToolset

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
