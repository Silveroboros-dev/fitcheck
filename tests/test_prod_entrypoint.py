"""Production entrypoint (el.mcp.__main__) smoke: it constructs the Cloud Run
server with the required binding and does NOT create_all (Alembic owns the
prod schema)."""

from sqlalchemy import create_engine, inspect

from el.mcp.__main__ import build


def test_build_uses_prod_binding_and_no_create_all(monkeypatch, tmp_path):
    db = tmp_path / "prod.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FITCHECK_DB_URL", f"sqlite:///{db}")
    monkeypatch.setenv("PORT", "8080")
    # Gemini proposers are lazy (client built per-call), so construction needs
    # no real key/network; set one defensively.
    monkeypatch.setenv("GEMINI_API_KEY", "unused-construction-is-lazy")

    server = build()

    # Constraint 2: 0.0.0.0:$PORT, stateless.
    assert server.settings.host == "0.0.0.0"
    assert server.settings.port == 8080
    assert server.settings.stateless_http is True

    # Constraint 1: NO create_all — a fresh connection sees an empty database
    # (the entrypoint must not create schema; migrations do).
    engine = create_engine(f"sqlite:///{db}")
    assert inspect(engine).get_table_names() == []
