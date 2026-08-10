"""MCP auth — api-key principal resolution (session-6 binding constraint 3).

Every MCP call is authenticated; the api key maps to an actor_id. agent_mcp
and api are the only MCP client types — human_ui is NOT an MCP client. The
resolved actor_id is what the blind-prior odds lock is scoped to, so a stolen
or swapped client_ref cannot stand in for the authenticated principal.
"""

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from el.domain.enums import ClientType
from el.domain.tables import ApiClient

_MCP_CLIENT_TYPES = frozenset({ClientType.AGENT_MCP, ClientType.API})


def hash_api_key(raw_key: str) -> str:
    """sha256 hex of the presented key; compared to ApiClient.key_hash."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    client_type: ClientType
    # The authenticated identity the odds lock is scoped to. = str(ApiClient.id)
    # for both agent_mcp and api (the api-key row IS the principal).
    actor_id: str
    agent_client_id: str
    rate_limit_tier: str


class AuthError(Exception):
    """Missing/invalid key, or a non-MCP client type."""


def resolve_principal(session: Session, raw_api_key: str | None) -> Principal:
    if not raw_api_key:
        raise AuthError("missing api key")
    row = session.scalars(
        select(ApiClient).where(ApiClient.key_hash == hash_api_key(raw_api_key))
    ).first()
    if row is None:
        raise AuthError("invalid api key")
    if row.revoked_at is not None:
        # Revoked/expired keys are rejected regardless of client_type — a found
        # row is not enough, the key must still be live.
        raise AuthError("api key revoked")
    client_type = ClientType(row.client_type)
    if client_type not in _MCP_CLIENT_TYPES:
        # human_ui is not the MCP path (constraint 3).
        raise AuthError(f"client_type {client_type.value} is not an MCP client")
    actor_id = str(row.id)
    return Principal(
        user_id=row.user_id,
        client_type=client_type,
        actor_id=actor_id,
        agent_client_id=actor_id,
        rate_limit_tier=row.rate_limit_tier,
    )
