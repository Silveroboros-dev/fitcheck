"""Credential-free contracts for the isolated public ADK client."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.adk.tools import mcp_tool

from fitcheck_adk_client import auth, client


@pytest.mark.parametrize(
    ("transport", "expected_type"),
    [
        ("streamable-http", mcp_tool.StreamableHTTPConnectionParams),
        ("sse", mcp_tool.SseConnectionParams),
    ],
)
def test_remote_connection_uses_real_adk_types(transport, expected_type):
    params = client._connection_params(
        "synthetic-key",
        environ={
            "FITCHECK_MCP_URL": "https://example.invalid/mcp",
            "FITCHECK_MCP_CLIENT_TRANSPORT": transport,
            "FITCHECK_API_KEY_HEADER": "x-fitcheck-key",
        },
    )

    assert isinstance(params, expected_type)
    serialized = params.model_dump(mode="json", include={"url", "headers"})
    assert serialized == {
        "url": "https://example.invalid/mcp",
        "headers": {"x-fitcheck-key": "synthetic-key"},
    }
    assert expected_type.model_validate(serialized).headers == params.headers


def test_stdio_uses_explicit_server_environment_without_client_secrets(tmp_path):
    params = client._connection_params(
        "synthetic-key",
        environ={
            "FITCHECK_SERVER_PYTHON": "/synthetic/server-venv/bin/python",
            "FITCHECK_SERVER_ROOT": str(tmp_path),
            "FITCHECK_PROPOSER": "fixture",
            "FITCHECK_SERVER_ENV_DATABASE_URL": "sqlite:///server.db",
            "FITCHECK_SERVER_ENV_GEMINI_API_KEY": "server-gemini-key",
            "FITCHECK_SERVER_ENV_FITCHECK_PROPOSER": "gemini",
            "GOOGLE_API_KEY": "client-google-key",
            "GEMINI_API_KEY": "client-gemini-key",
            "FITCHECK_CLOUD_RUN_ID_TOKEN": "client-id-token",
            "GOOGLE_APPLICATION_CREDENTIALS": "/client/adc.json",
            "PYTHONPATH": "/client/imports",
            "UNRELATED_SECRET": "client-secret",
        },
    )

    assert isinstance(params, mcp_tool.StdioConnectionParams)
    server = params.model_dump(mode="json")["server_params"]
    assert server["command"] == "/synthetic/server-venv/bin/python"
    assert server["args"] == ["-m", "demo.server"]
    assert server["cwd"] == str(tmp_path)
    assert server["env"] == {
        "DATABASE_URL": "sqlite:///server.db",
        "GEMINI_API_KEY": "server-gemini-key",
        "FITCHECK_API_KEY": "synthetic-key",
        "FITCHECK_PROPOSER": "gemini",
        "FITCHECK_MCP_TRANSPORT": "stdio",
    }


def test_stdio_requires_separate_server_python():
    with pytest.raises(SystemExit, match="FITCHECK_SERVER_PYTHON"):
        client._connection_params("synthetic-key", environ={})


@pytest.mark.parametrize(
    "managed_name",
    ["FITCHECK_API_KEY", "FITCHECK_MCP_TRANSPORT"],
)
def test_stdio_rejects_overriding_client_managed_server_contract(
    tmp_path, managed_name
):
    with pytest.raises(SystemExit, match="client-managed local server contract"):
        client._connection_params(
            "synthetic-key",
            environ={
                "FITCHECK_SERVER_PYTHON": "/synthetic/server-venv/bin/python",
                "FITCHECK_SERVER_ROOT": str(tmp_path),
                f"FITCHECK_SERVER_ENV_{managed_name}": "override",
            },
        )


def test_remote_cloud_run_auth_rejects_ambiguous_credentials():
    with pytest.raises(SystemExit, match="Remote MCP authentication error"):
        client._connection_params(
            "synthetic-key",
            environ={
                "FITCHECK_MCP_URL": "https://example.invalid/mcp",
                "FITCHECK_CLOUD_RUN_ID_TOKEN": "explicit-token",
                "FITCHECK_CLOUD_RUN_AUDIENCE": "https://example.invalid",
            },
        )


def test_remote_cloud_run_token_preserves_application_key():
    params = client._connection_params(
        "synthetic-key",
        environ={
            "FITCHECK_MCP_URL": "https://example.invalid/mcp",
            "FITCHECK_CLOUD_RUN_ID_TOKEN": "google-id-token",
        },
    )

    assert params.headers == {
        "x-api-key": "synthetic-key",
        "Authorization": "Bearer google-id-token",
    }


def test_cloud_run_audience_uses_exact_endpoint_origin(monkeypatch):
    seen = []
    monkeypatch.setattr(
        auth,
        "_fetch_google_id_token",
        lambda audience: seen.append(audience) or "minted-token",
    )

    headers = auth.remote_mcp_headers(
        api_key="synthetic-key",
        api_key_header="x-api-key",
        endpoint_url="https://example.invalid/mcp",
        environ={"FITCHECK_CLOUD_RUN_AUDIENCE": "https://example.invalid"},
    )

    assert seen == ["https://example.invalid"]
    assert headers["Authorization"] == "Bearer minted-token"


def test_event_text_joins_text_parts_only():
    event = SimpleNamespace(
        content=SimpleNamespace(
            parts=[
                SimpleNamespace(text="first"),
                SimpleNamespace(text=None),
                SimpleNamespace(text=" second"),
            ]
        )
    )

    assert client._event_text(event) == "first second"
