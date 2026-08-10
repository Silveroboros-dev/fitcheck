"""MCP api-key auth resolution (session-6 constraint 3)."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from el.domain.enums import ClientType
from el.domain.tables import ApiClient, Base, User
from el.mcp.auth import AuthError, Principal, hash_api_key, resolve_principal


def _sessions():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_client(sessions, *, key: str, client_type: str, revoked_at=None):
    with sessions() as s:
        user = User(email=f"u-{uuid.uuid4()}@example.com")
        s.add(user)
        s.flush()
        client = ApiClient(
            user_id=user.id,
            key_hash=hash_api_key(key),
            client_type=client_type,
            rate_limit_tier="default",
            revoked_at=revoked_at,
        )
        s.add(client)
        s.flush()
        result = (user.id, client.id)
        s.commit()
        return result


def test_resolves_agent_mcp_principal():
    sessions = _sessions()
    user_id, client_id = _seed_client(sessions, key="secret-key", client_type="agent_mcp")
    with sessions() as s:
        p = resolve_principal(s, "secret-key")
    assert isinstance(p, Principal)
    assert p.client_type is ClientType.AGENT_MCP
    assert p.user_id == user_id
    assert p.actor_id == str(client_id)  # api-key row IS the actor identity
    assert p.agent_client_id == str(client_id)


def test_api_client_type_resolves():
    sessions = _sessions()
    _seed_client(sessions, key="api-key", client_type="api")
    with sessions() as s:
        p = resolve_principal(s, "api-key")
    assert p.client_type is ClientType.API


def test_missing_key_rejected():
    sessions = _sessions()
    with sessions() as s:
        with pytest.raises(AuthError):
            resolve_principal(s, None)
        with pytest.raises(AuthError):
            resolve_principal(s, "")


def test_invalid_key_rejected():
    sessions = _sessions()
    _seed_client(sessions, key="secret-key", client_type="agent_mcp")
    with sessions() as s:
        with pytest.raises(AuthError):
            resolve_principal(s, "wrong-key")


def test_human_ui_is_not_an_mcp_client():
    sessions = _sessions()
    _seed_client(sessions, key="human-key", client_type="human_ui")
    with sessions() as s:
        with pytest.raises(AuthError, match="not an MCP client"):
            resolve_principal(s, "human-key")


def test_revoked_key_rejected():
    # A found row is not enough — a revoked key must still be rejected, before
    # the client_type check (revocation applies to any MCP client type).
    sessions = _sessions()
    _seed_client(
        sessions,
        key="revoked-key",
        client_type="agent_mcp",
        revoked_at=datetime.now(timezone.utc),
    )
    with sessions() as s:
        with pytest.raises(AuthError, match="revoked"):
            resolve_principal(s, "revoked-key")
