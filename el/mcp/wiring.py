"""Reusable wiring for the MCP tool surface — out of ``demo/`` so the
production entrypoint (``el.mcp.__main__``) does not import demo code.

Holds the production-relevant assembly: the Gemini-proposer tool surface and
the api-key principal seeder. The demo's fixture-only wiring stays in
``demo/build.py`` and re-exports these for backward compatibility.

The public market universe is fixture-only: ``build_market_provider`` accepts
the frozen Phase-0 snapshot (path via ``FITCHECK_SNAPSHOT``) and fails closed
for a live-provider selection. Evals construct ``FixtureMarketProvider``
directly on the frozen snapshot.
"""

import os
import uuid
from pathlib import Path

from sqlalchemy import select

from el.domain.enums import ClientType
from el.domain.tables import ApiClient, User
from el.draftcontract.service import DraftContractService
from el.extraction.service import ExtractionService
from el.extraction.gate_v2 import (
    GATE_POLICY_VERSION as LOOP1_V2_GATE_POLICY_VERSION,
)
from el.fitgate.service import FitService
from el.jobs import JobStore
from el.ledger.service import LedgerService
from el.marketpool.service import MarketPoolService
from el.marketstructure.service import MarketStructureService
from el.mcp.auth import Principal, hash_api_key, resolve_principal
from el.mcp.tools import McpTools
from el.mcp.v3_tools import McpV3Tools
from el.models.adapter_v2 import GeminiProposerV2
from el.models.draft_adapter import GeminiDraftProposer
from el.models.market_adapter import GeminiMarketStructureProposer
from el.models.source_adapter import GeminiSourceInterpreter
from el.retrieval.provider import build_market_provider
from el.retrieval.service import RetrievalService
from el.sourceinterpretation.jobs import SourceInterpretationJobService
from el.sourceinterpretation.service import SourceInterpretationService

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
    """Top-N cap for Gemini market-structure extraction. The live universe can
    be thousands of markets, but the deterministic gate only needs the
    highest-ranked candidates structured — capping here keeps structured_count
    and Gemini spend bounded regardless of universe size. Default 15; a
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
    """Build the MCP tool surface with Gemini proposers and frozen retrieval.

    Gemini calls occur in extraction, market-structure normalization, and
    draft-contract generation. The public market universe remains a frozen
    checked-in fixture; selecting a live provider fails closed."""
    extraction = ExtractionService(
        GeminiProposerV2(),
        session_factory,
        gate_policy_version=LOOP1_V2_GATE_POLICY_VERSION,
    )
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


def _source_job_service(jobs, source_interpretation, session_factory):
    """Build the durable source-job adapter shared by submitters and workers."""
    return SourceInterpretationJobService(
        jobs=jobs,
        source_interpretation=source_interpretation,
        session_factory=session_factory,
    )


def build_gemini_v3_source_worker_services(session_factory):
    """Return the exact Gemini-v3 services required by an MCP job worker.

    Keeping this composition alongside ``build_gemini_v3_tools`` prevents a
    worker from silently substituting fixture pins for queued MCP work.
    """
    source_interpretation = SourceInterpretationService(
        GeminiSourceInterpreter(), session_factory
    )
    return JobStore(session_factory), source_interpretation


def build_fixture_v3_source_worker_services(session_factory):
    """Return deterministic source-job services for the public MCP smoke path."""
    # The fixture source map is owned by the product composition; importing it
    # locally keeps production MCP wiring independent of the fixture surface.
    from el.product.wiring import build_services

    services = build_services(mode="fixture", session_factory=session_factory)
    return services.jobs, services.source_interpretation


def build_gemini_v3_tools(session_factory) -> McpV3Tools:
    """Build the additive v3 surface with the existing durable services.

    Source interpretation is submitted to ``SourceInterpretationJobService``;
    this wiring does not run a worker or wait for Gemini inside the submit
    request. Normalization and market-pool operations retain their established
    bounded synchronous service behavior.
    """

    jobs, source_interpretation = build_gemini_v3_source_worker_services(
        session_factory
    )
    source_interpretation_jobs = _source_job_service(
        jobs, source_interpretation, session_factory
    )
    extraction = ExtractionService(
        GeminiProposerV2(),
        session_factory,
        gate_policy_version=LOOP1_V2_GATE_POLICY_VERSION,
    )
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
    market_pool = MarketPoolService(retrieval, fit, session_factory)
    return McpV3Tools(
        source_interpretation=source_interpretation,
        source_interpretation_jobs=source_interpretation_jobs,
        extraction=extraction,
        market_pool=market_pool,
        session_factory=session_factory,
    )


def build_fixture_v3_tools(session_factory) -> McpV3Tools:
    """Build the deterministic public v3 MCP reference surface.

    It is deliberately limited to checked-in synthetic source and frozen
    market fixtures.  This builder exists for offline smoke tests; production
    ``el.mcp`` always uses ``build_gemini_v3_tools``.
    """
    jobs, source_interpretation = build_fixture_v3_source_worker_services(
        session_factory
    )
    from el.product.wiring import build_services

    services = build_services(
        mode="fixture", session_factory=session_factory
    )
    return McpV3Tools(
        source_interpretation=services.source_interpretation,
        source_interpretation_jobs=services.source_interpretation_jobs,
        extraction=services.extraction,
        market_pool=services.market_pool,
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
