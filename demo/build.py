"""Demo wiring helpers.

This package is intentionally a composition layer only: it reuses the same
services and MCP tools tested under ``tests/test_mcp_tools.py`` and adds no
domain behavior. The reusable (production) wiring — Gemini tools + principal
seeding — now lives in ``el.mcp.wiring`` and is re-exported here so existing
demo imports keep working; only the fixture-only assembly is demo-local.
"""

import json
import os
from datetime import date
from pathlib import Path

from el.domain.structures import MarketStructure
from el.draftcontract.service import DraftContractService
from el.extraction.service import ExtractionService
from el.fitgate.service import FitService
from el.ledger.service import LedgerService
from el.marketstructure.service import MarketStructureService
from el.mcp.tools import McpTools

# Re-exported from the shared wiring module (production-grade, non-demo).
from el.mcp.wiring import (
    build_fixture_v3_tools,
    build_gemini_tools,
    build_gemini_v3_tools,
    seed_principal,
)
from el.models.adapter import ExtractionProposal, FixtureProposer
from el.models.draft_adapter import FixtureDraftProposer, ProposedDraft
from el.models.market_adapter import FixtureMarketStructureProposer
from el.retrieval.provider import FixtureMarketProvider
from el.retrieval.service import RetrievalService

__all__ = [
    "build_fixture_tools",
    "build_fixture_v3_tools",
    "build_gemini_tools",
    "build_gemini_v3_tools",
    "seed_principal",
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(
    os.environ.get("FITCHECK_FIXTURES_DIR", PROJECT_ROOT / "tests" / "fixtures")
)
CLAIMS_DIR = FIXTURES_DIR / "claims"
SNAPSHOT = FIXTURES_DIR / "retrieval" / "frozen_snapshot_phase0.json"
MARKET_GOLDENS = FIXTURES_DIR / "markets" / "golden_market_structures.json"

NO_CLEAN_SUMMARY = "Zzcorp wins the underwater basket weaving cup in 2026."


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


def _good_draft() -> ProposedDraft:
    return ProposedDraft(
        proposed_title=(
            "Will Zzcorp win the underwater basket weaving world championship "
            "on or before December 31, 2026?"
        ),
        proposed_resolution_logic=(
            "Resolves YES if Zzcorp is declared champion at the 2026 world "
            "championship by the organizing federation."
        ),
        resolution_source="World Underwater Basket Weaving Federation results",
        resolution_source_class="official",
        resolution_deadline=date(2026, 12, 31),
        subject_entity="Zzcorp",
        event_stage="measured",
        category="sports",
        time_horizon="by end of 2026",
    )


def build_fixture_tools(session_factory) -> McpTools:
    """Build a deterministic, no-key MCP tool surface over frozen fixtures."""
    extraction = ExtractionService(FixtureProposer(_claim_fixtures()), session_factory)
    retrieval = RetrievalService(
        FixtureMarketProvider.from_path(SNAPSHOT), session_factory
    )
    market_structure = MarketStructureService(
        FixtureMarketStructureProposer(_market_goldens()), session_factory
    )
    fit = FitService(market_structure, session_factory)
    draft = DraftContractService(
        FixtureDraftProposer({NO_CLEAN_SUMMARY: _good_draft()}), session_factory
    )
    ledger = LedgerService(session_factory)
    return McpTools(
        extraction=extraction,
        retrieval=retrieval,
        fit=fit,
        draft=draft,
        ledger=ledger,
        session_factory=session_factory,
    )
