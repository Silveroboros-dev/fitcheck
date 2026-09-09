"""Fail-closed DB-URL resolver (el.domain.db.database_url) — the single
resolver shared by the MCP server, the demo, and Alembic migrations."""

import pytest

from el.domain import db
from el.domain.db import DEFAULT_URL, database_url


def _clear_db_env(monkeypatch):
    monkeypatch.delenv("FITCHECK_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_neither_set_defaults_to_core(monkeypatch):
    # Rule 5: neither set -> the core default; the same default migrations fall
    # back to, so the unconfigured case can't split.
    _clear_db_env(monkeypatch)
    assert database_url() == DEFAULT_URL == "sqlite:///fitcheck.db"


def test_only_fitcheck_set(monkeypatch):
    # Rule 2.
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("FITCHECK_DB_URL", "sqlite:///local.db")
    assert database_url() == "sqlite:///local.db"


def test_only_database_set(monkeypatch):
    # Rule 3.
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://h/db")
    assert database_url() == "postgresql+psycopg://h/db"


def test_both_set_equal(monkeypatch):
    # Rule 4: agreement is fine.
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("FITCHECK_DB_URL", "postgresql+psycopg://h/db")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://h/db")
    assert database_url() == "postgresql+psycopg://h/db"


def test_both_set_differ_fails_closed(monkeypatch):
    # Rule 1: disagreement halts (fail-closed), naming both vars without ever
    # copying their potentially credentialed values into startup/migration logs.
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("FITCHECK_DB_URL", "sensitive-fitcheck-value")
    monkeypatch.setenv("DATABASE_URL", "sensitive-database-value")
    with pytest.raises(SystemExit) as exc:
        database_url()
    msg = str(exc.value)
    assert "FITCHECK_DB_URL" in msg and "DATABASE_URL" in msg
    assert "sensitive-fitcheck-value" not in msg
    assert "sensitive-database-value" not in msg


def test_blank_counts_as_unset(monkeypatch):
    # A whitespace value is treated as unset: neither wins nor conflicts.
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("FITCHECK_DB_URL", "  ")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///real.db")
    assert database_url() == "sqlite:///real.db"


def test_make_engine_pre_pings_pooled_connections(monkeypatch):
    observed = {}
    sentinel = object()

    def fake_create_engine(url, **kwargs):
        observed["url"] = url
        observed["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(db, "create_engine", fake_create_engine)

    assert db.make_engine("postgresql+psycopg://host/db") is sentinel
    assert observed == {
        "url": "postgresql+psycopg://host/db",
        "kwargs": {"pool_pre_ping": True},
    }
