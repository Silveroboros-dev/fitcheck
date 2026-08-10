from __future__ import annotations

import pytest

from demo import cloud_run_auth


def test_remote_headers_preserve_api_key_only_default():
    assert cloud_run_auth.remote_mcp_headers(
        api_key="fitcheck-key",
        api_key_header="x-api-key",
        endpoint_url="http://127.0.0.1:8000/mcp",
        environ={},
    ) == {"x-api-key": "fitcheck-key"}


def test_remote_headers_add_explicit_cloud_run_id_token():
    assert cloud_run_auth.remote_mcp_headers(
        api_key="fitcheck-key",
        api_key_header="x-api-key",
        endpoint_url="https://fitcheck-abc-uc.a.run.app/mcp",
        environ={"FITCHECK_CLOUD_RUN_ID_TOKEN": "google-id-token"},
    ) == {
        "x-api-key": "fitcheck-key",
        "Authorization": "Bearer google-id-token",
    }


def test_remote_headers_fetch_id_token_for_exact_service_audience(monkeypatch):
    seen = []
    monkeypatch.setattr(
        cloud_run_auth,
        "_fetch_google_id_token",
        lambda audience: seen.append(audience) or "minted-token",
    )

    headers = cloud_run_auth.remote_mcp_headers(
        api_key="fitcheck-key",
        api_key_header="x-api-key",
        endpoint_url="https://fitcheck-abc-uc.a.run.app/mcp",
        environ={
            "FITCHECK_CLOUD_RUN_AUDIENCE": "https://fitcheck-abc-uc.a.run.app"
        },
    )

    assert seen == ["https://fitcheck-abc-uc.a.run.app"]
    assert headers["Authorization"] == "Bearer minted-token"


@pytest.mark.parametrize(
    "audience",
    [
        "http://fitcheck-abc-uc.a.run.app",
        "https://fitcheck-abc-uc.a.run.app/",
        "https://fitcheck-abc-uc.a.run.app/mcp",
        "https://fitcheck-abc-uc.a.run.app?query=yes",
        "https://user@fitcheck-abc-uc.a.run.app",
        "not-a-url",
    ],
)
def test_remote_headers_reject_non_service_url_audience(audience):
    with pytest.raises(
        cloud_run_auth.CloudRunAuthError,
        match="must be the HTTPS Cloud Run service URL",
    ):
        cloud_run_auth.remote_mcp_headers(
            api_key="fitcheck-key",
            api_key_header="x-api-key",
            endpoint_url="https://fitcheck-abc-uc.a.run.app/mcp",
            environ={"FITCHECK_CLOUD_RUN_AUDIENCE": audience},
        )


def test_remote_headers_reject_ambiguous_token_source():
    with pytest.raises(cloud_run_auth.CloudRunAuthError, match="Set only one"):
        cloud_run_auth.remote_mcp_headers(
            api_key="fitcheck-key",
            api_key_header="x-api-key",
            endpoint_url="https://fitcheck-abc-uc.a.run.app/mcp",
            environ={
                "FITCHECK_CLOUD_RUN_ID_TOKEN": "explicit-token",
                "FITCHECK_CLOUD_RUN_AUDIENCE": (
                    "https://fitcheck-abc-uc.a.run.app"
                ),
            },
        )


def test_remote_headers_reject_authorization_api_key_collision():
    with pytest.raises(
        cloud_run_auth.CloudRunAuthError,
        match="FITCHECK_API_KEY_HEADER cannot be Authorization",
    ):
        cloud_run_auth.remote_mcp_headers(
            api_key="fitcheck-key",
            api_key_header="Authorization",
            endpoint_url="https://fitcheck-abc-uc.a.run.app/mcp",
            environ={"FITCHECK_CLOUD_RUN_ID_TOKEN": "google-id-token"},
        )


@pytest.mark.parametrize("token", ["Bearer token", "token with-space", "token\n"])
def test_remote_headers_reject_malformed_explicit_token(token):
    with pytest.raises(cloud_run_auth.CloudRunAuthError):
        cloud_run_auth.remote_mcp_headers(
            api_key="fitcheck-key",
            api_key_header="x-api-key",
            endpoint_url="https://fitcheck-abc-uc.a.run.app/mcp",
            environ={"FITCHECK_CLOUD_RUN_ID_TOKEN": token},
        )


def test_remote_headers_reject_id_token_over_plain_http():
    with pytest.raises(
        cloud_run_auth.CloudRunAuthError,
        match="requires FITCHECK_MCP_URL to be an HTTPS URL",
    ):
        cloud_run_auth.remote_mcp_headers(
            api_key="fitcheck-key",
            api_key_header="x-api-key",
            endpoint_url="http://127.0.0.1:8000/mcp",
            environ={"FITCHECK_CLOUD_RUN_ID_TOKEN": "google-id-token"},
        )


def test_remote_headers_reject_audience_for_different_service():
    with pytest.raises(
        cloud_run_auth.CloudRunAuthError,
        match="must exactly match the origin of FITCHECK_MCP_URL",
    ):
        cloud_run_auth.remote_mcp_headers(
            api_key="fitcheck-key",
            api_key_header="x-api-key",
            endpoint_url="https://fitcheck-abc-uc.a.run.app/mcp",
            environ={
                "FITCHECK_CLOUD_RUN_AUDIENCE": "https://other-abc-uc.a.run.app"
            },
        )


def test_scripted_remote_client_reports_auth_configuration_error(monkeypatch):
    import asyncio

    from demo.client_scripted import _http_session

    monkeypatch.setenv("FITCHECK_CLOUD_RUN_ID_TOKEN", "explicit-token")
    monkeypatch.setenv(
        "FITCHECK_CLOUD_RUN_AUDIENCE", "https://fitcheck-abc-uc.a.run.app"
    )

    async def connect():
        async with _http_session(
            "fitcheck-key",
            "https://fitcheck-abc-uc.a.run.app/mcp",
            "streamable-http",
        ):
            pass

    with pytest.raises(SystemExit, match="Remote MCP authentication error"):
        asyncio.run(connect())


def test_adk_remote_client_reports_auth_configuration_error(monkeypatch):
    pytest.importorskip("google.adk")
    from demo.client_adk import _connection_params

    monkeypatch.setenv(
        "FITCHECK_MCP_URL", "https://fitcheck-abc-uc.a.run.app/mcp"
    )
    monkeypatch.setenv("FITCHECK_CLOUD_RUN_ID_TOKEN", "explicit-token")
    monkeypatch.setenv(
        "FITCHECK_CLOUD_RUN_AUDIENCE", "https://fitcheck-abc-uc.a.run.app"
    )

    with pytest.raises(SystemExit, match="Remote MCP authentication error"):
        _connection_params("fitcheck-key")


def test_adk_remote_client_sends_app_key_and_google_token(monkeypatch):
    pytest.importorskip("google.adk")
    from demo.client_adk import _connection_params

    monkeypatch.setenv(
        "FITCHECK_MCP_URL", "https://fitcheck-abc-uc.a.run.app/mcp"
    )
    monkeypatch.setenv("FITCHECK_CLOUD_RUN_ID_TOKEN", "google-id-token")
    monkeypatch.delenv("FITCHECK_CLOUD_RUN_AUDIENCE", raising=False)

    params = _connection_params("fitcheck-key")

    assert params.headers == {
        "x-api-key": "fitcheck-key",
        "Authorization": "Bearer google-id-token",
    }
