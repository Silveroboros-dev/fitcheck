"""Product-UI wiring: services, local DB, and the single local human actor.

Two modes (docs/agent-guided-ui-contract-v0.md §UI Modes):

- ``fixture`` (default): deterministic fixture proposers + the frozen
  Phase-0 snapshot. No credentials, no live calls. Mirrors the demo's
  fixture assembly but stays demo-independent (production surfaces do not
  import ``demo``).
- ``gemini``: live Gemini proposers over the same frozen fixture provider,
  mirroring ``el.mcp.wiring.build_gemini_tools``. Live-provider integration
  is not included in the public distribution.

Local identity: this surface is a single-user local tool. One ``User`` row
is get-or-created; transient loop objects are scoped by
``agent_client_id = human_ui:<user_id>`` and the odds lock binds to
``(client_type=human_ui, actor_id=user:<user_id>)`` — honest human_ui
provenance, never the MCP path.
"""

import json
import os
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.enums import ClientType
from el.domain.structures import MarketStructure
from el.domain.tables import Base, User
from el.draftcontract.service import DraftContractService
from el.extraction.service import ExtractionService
from el.fitgate.service import FitService
from el.ledger.service import LedgerService
from el.marketstructure.service import MarketStructureService
from el.mcp.wiring import (
    cap_expanded,
    cap_initial,
    market_structure_top_n,
    snapshot_path,
)
from el.models.adapter import ExtractionProposal, FixtureProposer, GeminiProposer
from el.models.draft_adapter import (
    FixtureDraftProposer,
    GeminiDraftProposer,
    ProposedDraft,
)
from el.models.market_adapter import (
    FixtureMarketStructureProposer,
    GeminiMarketStructureProposer,
)
from el.retrieval.provider import FixtureMarketProvider, build_market_provider
from el.retrieval.service import RetrievalService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(
    os.environ.get("FITCHECK_FIXTURES_DIR", PROJECT_ROOT / "tests" / "fixtures")
)
CLAIMS_DIR = FIXTURES_DIR / "claims"
MARKET_GOLDENS = FIXTURES_DIR / "markets" / "golden_market_structures.json"

LOCAL_USER_EMAIL = "local-product-ui@fitcheck.local"
CLIENT_REF = "product_ui"
LOCAL_UI_DEFAULT_URL = "sqlite:///fitcheck_product.db"


@dataclass(frozen=True)
class HumanActor:
    """The authenticated-equivalent identity for the local human surface."""

    user_id: uuid.UUID
    client_type: ClientType
    actor_id: str
    agent_client_id: str


@dataclass(frozen=True)
class ProductServices:
    mode: str
    extraction: ExtractionService
    retrieval: RetrievalService
    fit: FitService
    draft: DraftContractService
    ledger: LedgerService
    session_factory: sessionmaker[Session]


def database_url() -> str:
    """Resolve only the product UI's explicit local-database setting.

    The single-user fixture surface must not inherit the process-wide
    ``DATABASE_URL`` used by the MCP service or Alembic. A developer may have
    that variable pointed at a shared database while launching the documented
    local fixture command; inheriting it here would run ``create_all`` and
    write the synthetic local actor into that database.
    """

    configured = (os.environ.get("FITCHECK_UI_DB_URL") or "").strip()
    return configured or LOCAL_UI_DEFAULT_URL


def make_session_factory(url: str | None = None) -> sessionmaker[Session]:
    engine = create_engine(url or database_url())
    # Local single-user tool: create_all like the demo server. A deploy would
    # run alembic instead — this surface is not deployable as-is (contract
    # §Forbidden: no auth system in v0).
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def local_actor(session_factory: sessionmaker[Session]) -> HumanActor:
    with session_factory() as session:
        user = session.scalars(
            select(User).where(User.email == LOCAL_USER_EMAIL)
        ).first()
        if user is None:
            user = User(email=LOCAL_USER_EMAIL)
            session.add(user)
            session.commit()
        user_id = user.id
    return HumanActor(
        user_id=user_id,
        client_type=ClientType.HUMAN_UI,
        actor_id=f"user:{user_id}",
        agent_client_id=f"human_ui:{user_id}",
    )


def _claim_fixtures() -> dict[str, ExtractionProposal]:
    fixtures: dict[str, ExtractionProposal] = {}
    for path in sorted(CLAIMS_DIR.glob("*.json")):
        raw = json.loads(path.read_text())
        fixtures[raw["input_text"]] = ExtractionProposal.model_validate(
            raw["proposal"]
        )
    return fixtures


def _market_goldens() -> dict[str, MarketStructure]:
    raw = json.loads(MARKET_GOLDENS.read_text())
    return {
        row["market_id"]: MarketStructure.model_validate(row)
        for row in raw["structures"]
    }


def _fixture_draft_map(
    claims: dict[str, ExtractionProposal],
) -> dict[str, ProposedDraft]:
    """Golden drafts keyed by claim summary, so the no-clean path can show a
    generated draft candidate in fixture mode (the Acme M&A fixtures classify
    no-clean over the Phase-0 pool). Missing keys fail gracefully — the
    service reports no-draft and the card stays valid."""
    drafts: dict[str, ProposedDraft] = {}
    by_metric = {
        "acquisition of a competing company": ProposedDraft(
            proposed_title=(
                "Will Acme announce a definitive agreement for the "
                "acquisition of a competitor on or before December 31, 2026?"
            ),
            proposed_resolution_logic=(
                "Resolves YES if Acme announces a definitive agreement for "
                "the acquisition of a competitor (a competing company), as "
                "disclosed in a regulatory filing, on or before "
                "December 31, 2026."
            ),
            resolution_source="regulatory filings (e.g. SEC EDGAR)",
            resolution_source_class="filing",
            resolution_deadline=date(2026, 12, 31),
            subject_entity="Acme",
            event_stage="announced",
            category="business",
            time_horizon="by end of 2026",
        ),
        "divestiture of the cloud business unit": ProposedDraft(
            proposed_title=(
                "Will Acme announce a definitive agreement for the "
                "divestiture of the cloud business unit on or before "
                "December 31, 2026?"
            ),
            proposed_resolution_logic=(
                "Resolves YES if Acme announces a definitive agreement for "
                "the divestiture of the cloud business unit, as disclosed in "
                "a regulatory filing, on or before December 31, 2026."
            ),
            resolution_source="regulatory filings (e.g. SEC EDGAR)",
            resolution_source_class="filing",
            resolution_deadline=date(2026, 12, 31),
            subject_entity="Acme",
            event_stage="announced",
            category="business",
            time_horizon="by end of 2026",
        ),
    }
    for proposal in claims.values():
        structure = proposal.structure
        if structure is None:
            continue
        draft = by_metric.get(getattr(structure.metric, "what", None))
        if draft is not None:
            drafts[structure.claim_summary] = draft
    return drafts


def build_services(
    mode: str | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> ProductServices:
    mode = mode or os.environ.get("FITCHECK_UI_MODE", "fixture")
    sessions = session_factory or make_session_factory()
    if mode == "fixture":
        claims = _claim_fixtures()
        extraction = ExtractionService(FixtureProposer(claims), sessions)
        retrieval = RetrievalService(
            FixtureMarketProvider.from_path(snapshot_path()), sessions
        )
        market_structure = MarketStructureService(
            FixtureMarketStructureProposer(_market_goldens()), sessions
        )
        fit = FitService(market_structure, sessions)
        draft = DraftContractService(
            FixtureDraftProposer(_fixture_draft_map(claims)), sessions
        )
    elif mode == "gemini":
        extraction = ExtractionService(GeminiProposer(), sessions)
        retrieval = RetrievalService(
            build_market_provider(fixture_path=str(snapshot_path())), sessions
        )
        market_structure = MarketStructureService(
            GeminiMarketStructureProposer(),
            sessions,
            top_n=market_structure_top_n(),
        )
        fit = FitService(
            market_structure,
            sessions,
            cap_initial=cap_initial(),
            cap_expanded=cap_expanded(),
        )
        draft = DraftContractService(GeminiDraftProposer(), sessions)
    else:
        raise ValueError(f"unknown FITCHECK_UI_MODE: {mode!r}")
    return ProductServices(
        mode=mode,
        extraction=extraction,
        retrieval=retrieval,
        fit=fit,
        draft=draft,
        ledger=LedgerService(sessions),
        session_factory=sessions,
    )
