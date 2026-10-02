"""Product-UI wiring: services, local DB, and the single local human actor.

Two modes (docs/agent-guided-ui-contract-v0.md §UI Modes):

- ``fixture`` (default): deterministic fixture proposers + the frozen
  Phase-0 snapshot. No credentials, no live calls. Mirrors the demo's
  fixture assembly but stays demo-independent (production surfaces do not
  import ``demo``).
- ``gemini``: live Gemini proposers + env-selected market provider,
  mirroring ``el.mcp.wiring.build_gemini_tools``.

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

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.db import make_engine
from el.domain.enums import ClientType
from el.domain.structures import ExtractedStructure, MarketStructure
from el.domain.tables import Base, User
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
from el.mcp.wiring import (
    cap_expanded,
    cap_initial,
    market_structure_top_n,
    snapshot_path,
)
from el.models.adapter import (
    ExtractionProposal,
)
from el.models.adapter_v2 import (
    EXTRACTION_PROMPT_POLICY_VERSION_V2_READY,
    CandidateProposalV2,
    ClarificationProposalV2,
    ExtractionProposalV2,
    FixtureProposerV2,
    GeminiProposerV2,
)
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
from el.sourceinterpretation.service import SourceInterpretationService
from el.sourceinterpretation.jobs import SourceInterpretationJobService
from el.models.source_adapter import (
    FixtureSourceInterpreter,
    GeminiSourceInterpreter,
    SourceInterpretationProposal,
    SourceThesisCandidateProposal,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(
    os.environ.get("FITCHECK_FIXTURES_DIR", PROJECT_ROOT / "tests" / "fixtures")
)
CLAIMS_DIR = FIXTURES_DIR / "claims"
MARKET_GOLDENS = FIXTURES_DIR / "markets" / "golden_market_structures.json"

LOCAL_USER_EMAIL = "local-product-ui@fitcheck.local"
CLIENT_REF = "product_ui"
LOCAL_UI_DEFAULT_URL = "sqlite:///fitcheck_product.db"

MULTI_THESIS_FIXTURE = (
    "Harbor settlement tokens will increase demand for Northland reserve "
    "notes. Orchard Exchange will support autonomous settlement across five "
    "asset categories."
)
HARBOR_CANDIDATE = (
    "Harbor settlement tokens will increase demand for Northland reserve "
    "notes."
)
ORCHARD_CANDIDATE = (
    "Orchard Exchange will support autonomous settlement across five asset "
    "categories."
)
HARBOR_CLARIFICATION_QUESTION = (
    "By when, and by what observable amount, do you expect Harbor tokens to "
    "increase demand for reserve notes?"
)
HARBOR_CLARIFICATION_ANSWER = (
    "By December 31, 2028, Harbor token issuers will collectively reserve at "
    "least 200 million Northland notes, measured by the fictional Harbor "
    "Registry."
)
HARBOR_REVISED_INPUT = (
    f"{HARBOR_CANDIDATE}\n\nHuman clarification: "
    f"{HARBOR_CLARIFICATION_ANSWER}"
)
HARBOR_UI_REVISED_INPUT = (
    f"{HARBOR_CANDIDATE} Human clarification: "
    f"{HARBOR_CLARIFICATION_ANSWER}"
)
ORCHARD_CLARIFICATION_QUESTION = (
    "What observable criterion would show Orchard Exchange supports that "
    "settlement, and by when?"
)
ORCHARD_CLARIFICATION_ANSWER = (
    "By December 31, 2028, Orchard Exchange will support settlement across at "
    "least five asset categories and publish an interface for autonomous "
    "systems."
)
ORCHARD_REVISED_INPUT = (
    f"{ORCHARD_CANDIDATE}\n\nHuman clarification: "
    f"{ORCHARD_CLARIFICATION_ANSWER}"
)
ORCHARD_UI_REVISED_INPUT = (
    f"{ORCHARD_CANDIDATE} Human clarification: "
    f"{ORCHARD_CLARIFICATION_ANSWER}"
)

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
    source_interpretation: SourceInterpretationService
    extraction: ExtractionService
    retrieval: RetrievalService
    fit: FitService
    market_pool: MarketPoolService
    draft: DraftContractService
    ledger: LedgerService
    jobs: JobStore
    source_interpretation_jobs: SourceInterpretationJobService
    session_factory: sessionmaker[Session]


def database_url() -> str:
    """Resolve only the product UI's explicit local-database setting.

    The single-user fixture UI must not inherit a core ``DATABASE_URL`` or
    ``FITCHECK_DB_URL``. An MCP job worker has an explicit ``--surface mcp``
    or ``mcp-fixture`` mode for that shared core database.
    """
    configured = (os.environ.get("FITCHECK_UI_DB_URL") or "").strip()
    return configured or LOCAL_UI_DEFAULT_URL


def make_session_factory(url: str | None = None) -> sessionmaker[Session]:
    engine = make_engine(url or database_url())
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


def _claim_fixtures_v2(
    fixtures: dict[str, ExtractionProposal],
) -> dict[str, ExtractionProposalV2]:
    """Project checked-in semantic fixtures onto the active v2 envelope.

    The fixture and live product paths must exercise the same gate policy.
    This adapter adds no semantics: the exact fixture input is the selected
    quote, while an existing fixture question becomes the blocking branch.
    """

    projected: dict[str, ExtractionProposalV2] = {}
    for input_text, proposal in fixtures.items():
        if proposal.clarifying_question:
            projected[input_text] = ExtractionProposalV2(
                mode="clarification",
                candidate=None,
                clarification=ClarificationProposalV2(
                    blocking_ambiguities=(
                        proposal.structure.ambiguities
                        or ["The primary claim needs clarification"]
                    ),
                    question=proposal.clarifying_question,
                ),
            )
        else:
            projected[input_text] = ExtractionProposalV2(
                mode="candidate",
                candidate=CandidateProposalV2(
                    structure=proposal.structure,
                    selected_source_quote=input_text,
                ),
                clarification=None,
            )
    return projected


def _source_fixtures(
    claims: dict[str, ExtractionProposal],
) -> dict[str, SourceInterpretationProposal]:
    fixtures = {
        input_text: SourceInterpretationProposal(
            mode="candidates",
            candidate_items=[
                SourceThesisCandidateProposal(
                    selected_source_quote=input_text,
                    claim_summary=proposal.structure.claim_summary,
                )
            ],
            refusal_reasons=[],
        )
        for input_text, proposal in claims.items()
    }
    fixtures[MULTI_THESIS_FIXTURE] = SourceInterpretationProposal(
        mode="candidates",
        candidate_items=[
            SourceThesisCandidateProposal(
                selected_source_quote=HARBOR_CANDIDATE,
                claim_summary=(
                    "Harbor settlement tokens increase demand for Northland "
                    "reserve notes"
                ),
            ),
            SourceThesisCandidateProposal(
                selected_source_quote=ORCHARD_CANDIDATE,
                claim_summary=(
                    "Orchard Exchange supports autonomous settlement across "
                    "five asset categories"
                ),
            ),
        ],
        refusal_reasons=[],
    )
    return fixtures


def _add_multi_normalization_fixtures(
    fixtures: dict[str, ExtractionProposalV2],
) -> None:
    fixtures[HARBOR_CANDIDATE] = ExtractionProposalV2(
        mode="clarification",
        candidate=None,
        clarification=ClarificationProposalV2(
            blocking_ambiguities=[
                "The magnitude and time horizon of the demand increase are missing"
            ],
            question=HARBOR_CLARIFICATION_QUESTION,
        ),
    )
    fixtures[ORCHARD_CANDIDATE] = ExtractionProposalV2(
        mode="clarification",
        candidate=None,
        clarification=ClarificationProposalV2(
            blocking_ambiguities=[
                "The meaning of success and its time horizon are missing"
            ],
            question=ORCHARD_CLARIFICATION_QUESTION,
        ),
    )
    fixtures[HARBOR_REVISED_INPUT] = ExtractionProposalV2(
        mode="candidate",
        candidate=CandidateProposalV2(
            structure=ExtractedStructure(
                claim_summary=(
                    "Harbor token issuers collectively reserve at least 200 "
                    "million Northland notes by end of 2028"
                ),
                entities=[
                    {"name": "Harbor token issuers", "role": "subject"},
                    {"name": "Northland notes", "role": "object"},
                    {"name": "Harbor Registry", "role": "source"},
                ],
                event_stage="measured",
                metric={
                    "what": (
                        "collective reserve of Northland notes of at least 200 "
                        "million"
                    ),
                    "measured_by": "Harbor Registry",
                    "objective": True,
                },
                horizon={
                    "window_end": date(2028, 12, 31),
                    "timezone": "UTC",
                    "precision": "day",
                },
                mechanism={
                    "asserted_causal_chain": (
                        "Harbor token issuance increases demand for Northland "
                        "notes"
                    ),
                    "is_composite": False,
                },
                stance="increase",
                resolution_source_class="official",
                ambiguities=[],
                contractible_version=(
                    "Harbor token issuers collectively report a reserve of at "
                    "least 200 million Northland notes on 2028-12-31."
                ),
            ),
            selected_source_quote=HARBOR_CLARIFICATION_ANSWER,
        ),
        clarification=None,
    )
    fixtures[ORCHARD_REVISED_INPUT] = ExtractionProposalV2(
        mode="candidate",
        candidate=CandidateProposalV2(
            structure=ExtractedStructure(
                claim_summary=(
                    "Orchard Exchange supports settlement across at least five "
                    "asset categories and an autonomous-systems interface by "
                    "end of 2028"
                ),
                entities=[{"name": "Orchard Exchange", "role": "subject"}],
                event_stage="launched",
                metric={
                    "what": (
                        "settlement across at least five asset categories and an "
                        "interface for autonomous systems"
                    ),
                    "measured_by": "Orchard Exchange documentation",
                    "objective": True,
                },
                horizon={
                    "window_end": date(2028, 12, 31),
                    "timezone": "UTC",
                    "precision": "day",
                },
                mechanism={"asserted_causal_chain": None, "is_composite": False},
                stance="yes",
                resolution_source_class="official",
                ambiguities=[],
                contractible_version=(
                    "Orchard Exchange supports settlement across at least five "
                    "asset categories and publishes an interface for autonomous "
                    "systems on 2028-12-31."
                ),
            ),
            selected_source_quote=ORCHARD_CLARIFICATION_ANSWER,
        ),
        clarification=None,
    )
    # Historical replay keys retain their original delimiter; browser-created
    # clarification input uses one literal space before the label.
    fixtures[HARBOR_UI_REVISED_INPUT] = fixtures[HARBOR_REVISED_INPUT]
    fixtures[ORCHARD_UI_REVISED_INPUT] = fixtures[ORCHARD_REVISED_INPUT]


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
        normalization_fixtures = _claim_fixtures_v2(claims)
        _add_multi_normalization_fixtures(normalization_fixtures)
        proposer = FixtureProposerV2(normalization_fixtures)
        # This is a deterministic projection of historical fixture outputs,
        # not evidence that Gemini v2.1 produced those bytes. Keep its
        # provenance distinct while exercising the same v2 envelope and gate.
        proposer.prompt_policy_version = (
            "fixture-projection-of-"
            f"{EXTRACTION_PROMPT_POLICY_VERSION_V2_READY}"
        )
        extraction = ExtractionService(
            proposer,
            sessions,
            gate_policy_version=LOOP1_V2_GATE_POLICY_VERSION,
        )
        source_interpretation = SourceInterpretationService(
            FixtureSourceInterpreter(_source_fixtures(claims)), sessions
        )
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
        source_interpretation = SourceInterpretationService(
            GeminiSourceInterpreter(), sessions
        )
        extraction = ExtractionService(
            GeminiProposerV2(),
            sessions,
            gate_policy_version=LOOP1_V2_GATE_POLICY_VERSION,
        )
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
    market_pool = MarketPoolService(retrieval, fit, sessions)
    jobs = JobStore(sessions)
    return ProductServices(
        mode=mode,
        source_interpretation=source_interpretation,
        extraction=extraction,
        retrieval=retrieval,
        fit=fit,
        market_pool=market_pool,
        draft=draft,
        ledger=LedgerService(sessions),
        jobs=jobs,
        source_interpretation_jobs=SourceInterpretationJobService(
            jobs=jobs,
            source_interpretation=source_interpretation,
            session_factory=sessions,
        ),
        session_factory=sessions,
    )
