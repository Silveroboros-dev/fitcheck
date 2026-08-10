import pytest

from demo.client_scripted import run


def test_scripted_demo_stdio_fixture_flow(monkeypatch):
    monkeypatch.delenv("FITCHECK_MCP_URL", raising=False)
    # The subprocess inherits os.environ; a stray DATABASE_URL would conflict
    # with FITCHECK_DB_URL under the fail-closed resolver and halt the server.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FITCHECK_PROPOSER", "fixture")
    monkeypatch.setenv("FITCHECK_DB_URL", "sqlite://")
    monkeypatch.setenv("FITCHECK_API_KEY", "test-demo-key")
    # Cloud Run auth belongs only to direct HTTP clients. Even contradictory
    # remote settings must not affect the self-contained stdio path.
    monkeypatch.setenv("FITCHECK_CLOUD_RUN_ID_TOKEN", "not a valid token")
    monkeypatch.setenv("FITCHECK_CLOUD_RUN_AUDIENCE", "not-a-service-url")
    pytest.importorskip("mcp.client.stdio")

    import asyncio

    asyncio.run(run())


def test_demo_server_never_returns_configured_key_as_log_metadata(monkeypatch):
    from demo.server import _api_key_config

    monkeypatch.setenv("FITCHECK_API_KEY", "sensitive-configured-key")
    key, log_value = _api_key_config()

    assert key == "sensitive-configured-key"
    assert log_value == "configured"
    assert key not in log_value


def test_demo_server_describes_generated_key_without_logging_it(monkeypatch):
    from demo.server import _api_key_config

    monkeypatch.delenv("FITCHECK_API_KEY", raising=False)
    key, log_value = _api_key_config()

    assert key.startswith("demo-")
    assert log_value == "generated"
    assert key not in log_value


def test_demo_server_startup_metadata_excludes_database_credentials():
    from demo.server import _runtime_log_line

    sensitive_url = "postgresql://fitcheck:secret-password@example.test/fitcheck"
    line = _runtime_log_line(
        transport="streamable-http",
        proposer="fixture",
        database_backend="postgresql",
    )

    assert "db_backend=postgresql" in line
    assert sensitive_url not in line
    assert "secret-password" not in line
