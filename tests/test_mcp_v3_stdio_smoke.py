"""Offline real-stdio MCP v3.1 transport smoke with a separate fixture worker."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from el.mcp.server import V3_TOOL_NAMES
from el.product.wiring import MULTI_THESIS_FIXTURE

ROOT = Path(__file__).resolve().parents[1]


def _payload(result):
    return json.loads(result.content[0].text)


async def _stdio_call(environment, tool_name, arguments):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "demo.server"],
        env=environment,
        cwd=str(ROOT),
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool(tool_name, arguments)
    return {tool.name for tool in listed.tools}, _payload(result)


def test_fixture_stdio_submit_worker_poll_uses_real_mcp_transport(tmp_path):
    pytest.importorskip("mcp.client.stdio")
    database_url = f"sqlite+pysqlite:///{tmp_path / 'stdio-v3.sqlite3'}"
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment.pop("FITCHECK_UI_DB_URL", None)
    environment.update(
        {
            "FITCHECK_DB_URL": database_url,
            "FITCHECK_API_KEY": "stdio-fixture-test-key",
            "FITCHECK_PROPOSER": "fixture",
            "FITCHECK_MCP_TRANSPORT": "stdio",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT),
        }
    )

    names, queued = asyncio.run(
        _stdio_call(
            environment,
            "v3_submit_source_interpretation",
            {
                "input_text": MULTI_THESIS_FIXTURE,
                "source_url": None,
                "idempotency_key": "stdio-v3-source",
            },
        )
    )
    assert set(V3_TOOL_NAMES) <= names
    assert queued["status"] == "queued"

    worker = subprocess.run(
        [
            sys.executable,
            "-m",
            "el.sourceinterpretation.worker",
            "--surface",
            "mcp-fixture",
            "--once",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert worker.returncode == 0, worker.stderr
    assert '"status": "succeeded"' in worker.stdout

    _, completed = asyncio.run(
        _stdio_call(
            environment,
            "v3_get_source_interpretation_job",
            {"job_id": queued["job_id"]},
        )
    )
    assert completed["status"] == "succeeded"
    assert completed["interpretation"]["outcome"] == "candidates"
    assert len(completed["interpretation"]["candidates"]) == 2
