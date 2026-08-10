"""Reusable wiring for the MCP tool surface — out of ``demo/`` so the
production entrypoint (``el.mcp.__main__``) does not import demo code.

Holds the production-relevant assembly: the Gemini-proposer tool surface and
the api-key principal seeder. The demo's fixture-only wiring stays in
``demo/build.py`` and re-exports these for backward compatibility.

The public distribution wires ``build_market_provider`` only to its frozen
fixture (path via ``FITCHECK_SNAPSHOT``). Its live-provider adapter is not
included, and selecting ``MARKET_PROVIDER=polydata`` fails closed.
"""

import os
import uuid
from pathlib import Path

from sqlalchemy import select

from el.domain.enums import ClientType
from el.domain.tables import ApiClient, User
from el.draftcontract.service import DraftContractService
from el.extraction.service import ExtractionService
from el.fitgate.service import FitService
from el.ledger.service import LedgerService
from el.marketstructure.service import MarketStructureService
from el.mcp.auth import Principal, hash_api_key, resolve_principal
from el.mcp.tools import McpTools
from el.models.adapter import GeminiProposer
from el.models.draft_adapter import GeminiDraftProposer
from el.models.market_adapter import GeminiMarketStructureProposer
from el.retrieval.provider import build_market_provider
from el.retrieval.service import RetrievalService

# Repo-relative default; overridable so the deploy image can place the
# snapshot outside the test tree.
DEFAULT_SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "retrieval"
    / "frozen_snapshot_phase0.json"
)


def snapshot_path() -> Path:
    return Path(os.environ.get("FITCHECK_SNAPSHOT") or DEFAULT_SNAPSHOT)


def market_structure_top_n() -> int:
    """Top-N cap for Gemini market-structure extraction. The deterministic gate
    only needs the highest-ranked fixture candidates structured, and the cap
    keeps structured_count and Gemini spend bounded. Default 15; a
    non-positive or unparseable value falls back to 15 rather than silently
    disabling the bound."""
    raw = os.environ.get("MARKET_STRUCTURE_TOP_N", "15")
    try:
        n = int(raw)
    except ValueError:
        return 15
    return n if n > 0 else 15


def cap_initial() -> int:
    raw = os.environ.get("MARKET_STRUCTURE_CAP_INITIAL", "5")
    try:
        n = int(raw)
    except ValueError:
        return 5
    return n if n > 0 else 5


def cap_expanded() -> int:
    raw = os.environ.get("MARKET_STRUCTURE_CAP_EXPANDED", "20")
    try:
        n = int(raw)
    except ValueError:
        return 20
    return n if n > 0 else 20


def build_gemini_tools(session_factory) -> McpTools:
    """Build the MCP tool surface with Gemini proposers and fixture retrieval.

    Gemini calls occur in extraction, market-structure normalization, and
    draft-contract generation. Market retrieval remains on the frozen public
    fixture; live-provider integration is outside this distribution."""
    extraction = ExtractionService(GeminiProposer(), session_factory)
    retrieval = RetrievalService(
        build_market_provider(fixture_path=str(snapshot_path())), session_factory
    )
    market_structure = MarketStructureService(
        GeminiMarketStructureProposer(),
        session_factory,
        top_n=market_structure_top_n(),
    )
    fit = FitService(
        market_structure,
        session_factory,
        cap_initial=cap_initial(),
        cap_expanded=cap_expanded(),
    )
    draft = DraftContractService(GeminiDraftProposer(), session_factory)
    ledger = LedgerService(session_factory)
    return McpTools(
        extraction=extraction,
        retrieval=retrieval,
        fit=fit,
        draft=draft,
        ledger=ledger,
        session_factory=session_factory,
    )


def seed_principal(
    session_factory, raw_key: str, *, client_type: str = ClientType.AGENT_MCP.value
) -> Principal:
    """Create or reuse an API key and return its resolved Principal. Used to
    provision agent clients (the demo seeds one; a deploy provisions real
    keys out-of-band)."""
    key_hash = hash_api_key(raw_key)
    with session_factory() as session:
        existing = session.scalars(
            select(ApiClient).where(ApiClient.key_hash == key_hash)
        ).first()
        if existing is None:
            user = User(email=f"client-{uuid.uuid4()}@fitcheck.local")
            session.add(user)
            session.flush()
            session.add(
                ApiClient(
                    user_id=user.id,
                    key_hash=key_hash,
                    client_type=client_type,
                    rate_limit_tier="default",
                )
            )
            session.commit()

    with session_factory() as session:
        return resolve_principal(session, raw_key)
