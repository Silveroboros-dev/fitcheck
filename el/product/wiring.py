"""Product-UI wiring: services, local DB, and the single local human actor.

Runtime modes (docs/agent-guided-ui-contract-v0.md §UI Modes):

- ``fixture`` (default): deterministic fixture proposers + the frozen
  Phase-0 snapshot. No credentials, no live calls. Mirrors the demo's
  fixture assembly but stays demo-independent (production surfaces do not
  import ``demo``).
- ``gemini``: live Gemini proposers + env-selected market provider,
  mirroring ``el.mcp.wiring.build_gemini_tools``.
- ``vertex-canary``: explicit local three-slot Vertex operation over frozen
  market retrieval; predecessor draft generation remains fixture-only.

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
from importlib.resources import files
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from el.domain.enums import ClientType
from el.domain.structures import ExtractedStructure, MarketStructure
from el.domain.tables import Base, User
from el.draftcontract.service import DraftContractService
from el.extraction.service import ExtractionService
from el.extraction.gate_v2 import (
    GATE_POLICY_VERSION as LOOP1_V2_GATE_POLICY_VERSION,
)
from el.fitgate.service import FitService
from el.fitgate.m1_subject_only import ordinary_discovery_fit_policy
from el.jobs import JobStore
from el.ledger.service import LedgerService
from el.marketpool.service import MarketPoolService
from el.marketstructure.service import MarketStructureService
from el.mcp.wiring import (
    build_gemini_source_interpretation,
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
from el.models.market_adapter_variants import VertexCanaryMarketStructureProposer
from el.retrieval.provider import (
    FixtureMarketProvider,
    MarketProvider,
    build_market_provider,
)
from el.retrieval.gamma_config import (
    load_configured_gamma_provider,
    load_lazy_configured_gamma_provider,
)
from el.retrieval.service import RetrievalService
from el.sourceinterpretation.service import SourceInterpretationService
from el.sourceinterpretation.jobs import SourceInterpretationJobService
from el.models.source_adapter import (
    FixtureSourceInterpreter,
    GeminiSourceInterpreter,
    SourceInterpretationProposal,
    SourceThesisCandidateProposal,
)
from el.models.vertex_canary import VertexCanaryOperation, operation_from_env

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_FIXTURES = files("el.product.demo_fixtures")
FIXTURES_DIR = Path(
    os.environ.get("FITCHECK_FIXTURES_DIR", PACKAGE_FIXTURES)
)
CLAIMS_DIR = FIXTURES_DIR / "claims"
MARKET_GOLDENS = FIXTURES_DIR / "markets" / "golden_market_structures.json"

LOCAL_USER_EMAIL = "local-product-ui@fitcheck.local"
CLIENT_REF = "product_ui"
LOCAL_UI_DEFAULT_URL = "sqlite:///fitcheck_product.db"

MULTI_THESIS_FIXTURE = (
    "Stablecoins will increase global demand for U.S. dollars and U.S. "
    "Treasury debt. Robinhood will win by bringing every asset onchain and "
    "enabling machine trading."
)
STABLECOIN_CANDIDATE = (
    "Stablecoins will increase global demand for U.S. dollars and U.S. "
    "Treasury debt."
)
ROBINHOOD_CANDIDATE = (
    "Robinhood will win by bringing every asset onchain and enabling machine "
    "trading."
)
STABLECOIN_CLARIFICATION_QUESTION = (
    "By when, and by what observable amount, do you expect stablecoins to "
    "increase demand for dollars or Treasury debt?"
)
STABLECOIN_CLARIFICATION_ANSWER = (
    "By December 31, 2027, stablecoin issuers will collectively hold at least "
    "$200 billion in U.S. Treasury securities, measured by public issuer "
    "attestations."
)
STABLECOIN_REVISED_INPUT = (
    f"{STABLECOIN_CANDIDATE}\n\nHuman clarification: "
    f"{STABLECOIN_CLARIFICATION_ANSWER}"
)
STABLECOIN_UI_REVISED_INPUT = (
    f"{STABLECOIN_CANDIDATE} Human clarification: "
    f"{STABLECOIN_CLARIFICATION_ANSWER}"
)
ROBINHOOD_CLARIFICATION_QUESTION = (
    "What observable criterion would count as Robinhood succeeding, and by "
    "when?"
)
ROBINHOOD_CLARIFICATION_ANSWER = (
    "By December 31, 2027, Robinhood will support onchain trading for at least "
    "five asset classes and publish an API for autonomous trading systems."
)
ROBINHOOD_REVISED_INPUT = (
    f"{ROBINHOOD_CANDIDATE}\n\nHuman clarification: "
    f"{ROBINHOOD_CLARIFICATION_ANSWER}"
)
ROBINHOOD_UI_REVISED_INPUT = (
    f"{ROBINHOOD_CANDIDATE} Human clarification: "
    f"{ROBINHOOD_CLARIFICATION_ANSWER}"
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
    source_interpretation_jobs: SourceInterpretationJobService
    jobs: JobStore
    extraction: ExtractionService
    retrieval: RetrievalService
    fit: FitService
    market_pool: MarketPoolService
    draft: DraftContractService
    ledger: LedgerService
    session_factory: sessionmaker[Session]


def database_url() -> str:
    explicit_ui_url = (os.environ.get("FITCHECK_UI_DB_URL") or "").strip()
    if explicit_ui_url:
        return explicit_ui_url
    # Public local UI never inherits an ambient service DB URL. MCP uses the
    # separate core resolver; the one-command demo supplies an explicit URL.
    return LOCAL_UI_DEFAULT_URL


def _alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(files("migrations")))
    return config


def _migration_heads() -> set[str]:
    return set(ScriptDirectory.from_config(_alembic_config()).get_heads())


def _bootstrap_empty_database(engine) -> None:
    config = _alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def make_session_factory(url: str | None = None) -> sessionmaker[Session]:
    engine = create_engine(url or database_url())
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if not existing_tables:
        # Zero setup remains available, but the schema is created by the same
        # migration chain used everywhere else and is stamped at its head.
        _bootstrap_empty_database(engine)
    else:
        if "alembic_version" not in existing_tables:
            engine.dispose()
            raise RuntimeError(
                "FitCheck database is non-empty but has no Alembic version; "
                "startup made no changes. Running `alembic upgrade head` "
                "against existing unstamped tables is unsafe. Back up the "
                "database and use an explicit legacy adoption or recovery "
                "procedure."
            )
        with engine.connect() as connection:
            current_revisions = {
                row[0]
                for row in connection.execute(
                    text("SELECT version_num FROM alembic_version")
                )
            }
        expected_heads = _migration_heads()
        if current_revisions != expected_heads:
            engine.dispose()
            current = ", ".join(sorted(current_revisions)) or "none"
            expected = ", ".join(sorted(expected_heads))
            raise RuntimeError(
                "FitCheck database migration is not current; run "
                "`python -m alembic upgrade head` before startup, with "
                "FITCHECK_DB_URL set to the same database URL used by the "
                "product "
                f"(current: {current}; expected: {expected})"
            )

        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
        expected_tables = set(Base.metadata.tables)
        gaps = [
            *(
                f"missing table {name}"
                for name in sorted(expected_tables - existing_tables)
            ),
        ]
        for table_name in sorted(expected_tables & existing_tables):
            actual_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            expected_columns = set(Base.metadata.tables[table_name].columns.keys())
            gaps.extend(
                f"missing column {table_name}.{name}"
                for name in sorted(expected_columns - actual_columns)
            )
        if gaps:
            engine.dispose()
            preview = "; ".join(gaps[:4])
            if len(gaps) > 4:
                preview += f"; and {len(gaps) - 4} more"
            raise RuntimeError(
                "FitCheck database claims the current migration head but its "
                "schema is incomplete or mixed; startup made no changes. "
                "Restore a valid backup or use an explicit recovery "
                f"procedure ({preview})"
            )
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
                selected_source_quote=STABLECOIN_CANDIDATE,
                claim_summary=(
                    "Stablecoins increase global demand for U.S. dollars and "
                    "U.S. Treasury debt"
                ),
            ),
            SourceThesisCandidateProposal(
                selected_source_quote=ROBINHOOD_CANDIDATE,
                claim_summary=(
                    "Robinhood succeeds by bringing every asset onchain and "
                    "enabling machine trading"
                ),
            ),
        ],
        refusal_reasons=[],
    )
    return fixtures


def _add_multi_normalization_fixtures(
    fixtures: dict[str, ExtractionProposalV2],
) -> None:
    fixtures[STABLECOIN_CANDIDATE] = ExtractionProposalV2(
        mode="clarification",
        candidate=None,
        clarification=ClarificationProposalV2(
            blocking_ambiguities=[
                "The magnitude and time horizon of the demand increase are missing"
            ],
            question=STABLECOIN_CLARIFICATION_QUESTION,
        ),
    )
    fixtures[ROBINHOOD_CANDIDATE] = ExtractionProposalV2(
        mode="clarification",
        candidate=None,
        clarification=ClarificationProposalV2(
            blocking_ambiguities=[
                "The meaning of success and its time horizon are missing"
            ],
            question=ROBINHOOD_CLARIFICATION_QUESTION,
        ),
    )
    fixtures[STABLECOIN_REVISED_INPUT] = ExtractionProposalV2(
        mode="candidate",
        candidate=CandidateProposalV2(
            structure=ExtractedStructure(
                claim_summary=(
                    "Stablecoin issuers collectively hold at least $200 billion "
                    "in U.S. Treasury securities by end of 2027"
                ),
                entities=[
                    {"name": "stablecoin issuers", "role": "subject"},
                    {"name": "U.S. Treasury securities", "role": "object"},
                    {"name": "public issuer attestations", "role": "source"},
                ],
                event_stage="measured",
                metric={
                    "what": (
                        "collective holdings of U.S. Treasury securities of at "
                        "least $200 billion"
                    ),
                    "measured_by": "public issuer attestations",
                    "objective": True,
                },
                horizon={
                    "window_end": date(2027, 12, 31),
                    "timezone": "UTC",
                    "precision": "day",
                },
                mechanism={
                    "asserted_causal_chain": (
                        "Stablecoin issuance increases demand for U.S. Treasury "
                        "securities"
                    ),
                    "is_composite": False,
                },
                stance="increase",
                resolution_source_class="official",
                ambiguities=[],
                contractible_version=(
                    "Stablecoin issuers collectively report holdings of at least "
                    "$200 billion in U.S. Treasury securities by December 31, 2027."
                ),
            ),
            selected_source_quote=STABLECOIN_CLARIFICATION_ANSWER,
        ),
        clarification=None,
    )
    fixtures[ROBINHOOD_REVISED_INPUT] = ExtractionProposalV2(
        mode="candidate",
        candidate=CandidateProposalV2(
            structure=ExtractedStructure(
                claim_summary=(
                    "Robinhood supports onchain trading for at least five asset "
                    "classes and an autonomous-trading API by end of 2027"
                ),
                entities=[{"name": "Robinhood", "role": "subject"}],
                event_stage="launched",
                metric={
                    "what": (
                        "onchain trading for at least five asset classes and an "
                        "API for autonomous trading systems"
                    ),
                    "measured_by": "Robinhood public product documentation",
                    "objective": True,
                },
                horizon={
                    "window_end": date(2027, 12, 31),
                    "timezone": "UTC",
                    "precision": "day",
                },
                mechanism={"asserted_causal_chain": None, "is_composite": False},
                stance="yes",
                resolution_source_class="official",
                ambiguities=[],
                contractible_version=(
                    "Robinhood supports onchain trading for at least five asset "
                    "classes and publishes an API for autonomous trading systems "
                    "by December 31, 2027."
                ),
            ),
            selected_source_quote=ROBINHOOD_CLARIFICATION_ANSWER,
        ),
        clarification=None,
    )
    # The historical double-newline keys remain replayable. The current
    # browser composes a one-space delimiter so literal model quote binding
    # remains stable when the model renders ordinary whitespace.
    fixtures[STABLECOIN_UI_REVISED_INPUT] = fixtures[STABLECOIN_REVISED_INPUT]
    fixtures[ROBINHOOD_UI_REVISED_INPUT] = fixtures[ROBINHOOD_REVISED_INPUT]


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
    *,
    model_operation: VertexCanaryOperation | None = None,
    retrieval_provider: MarketProvider | None = None,
    anonymous_demo: bool | None = None,
) -> ProductServices:
    mode = mode or os.environ.get("FITCHECK_UI_MODE", "fixture")
    anonymous_demo = (os.environ.get("FITCHECK_ANONYMOUS_DEMO") == "1"
                      if anonymous_demo is None else anonymous_demo)
    if anonymous_demo and mode != "fixture":
        raise ValueError("anonymous demo requires fixture mode")
    sessions = session_factory or make_session_factory()
    if anonymous_demo and retrieval_provider is not None:
        raise ValueError("anonymous demo requires its registered frozen provider")
    assessment_projection = None
    bounded = None
    if mode == "vertex-canary":
        bounded = model_operation or operation_from_env()
        if bounded is None:
            raise ValueError("FITCHECK_VERTEX_CANARY_PROFILE is required")
    custom_answer_profile = bool(
        bounded is not None
        and bounded.profile.profile_kind == "custom-answer-v1"
    )
    configured_gamma_provider = (
        load_lazy_configured_gamma_provider()
        if custom_answer_profile
        else None if anonymous_demo
        else load_configured_gamma_provider()
    )
    if (
        retrieval_provider is not None
        and configured_gamma_provider is not None
        and not custom_answer_profile
    ):
        raise ValueError(
            "configure either retrieval_provider or Gamma replay, not both"
        )
    selected_provider = retrieval_provider or configured_gamma_provider
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
        # An explicit injected provider is the only ordinary-product route for
        # a sealed Gamma replay.  The provider must enforce its own thesis
        # binding; no environment switch turns an old query capture into a
        # generic market universe.
        if anonymous_demo:
            from el.product.demo_frozen_markets import (
                ELIGIBILITY_POLICY, FrozenDemoMarketProvider, market_structures,
                project_assessment,
            )
            from el.retrieval.gate import Loop2Policy
            retrieval = RetrievalService(
                FrozenDemoMarketProvider(), sessions,
                Loop2Policy(min_liquidity_usd=0),
                gate_policy_version=ELIGIBILITY_POLICY,
            )
            structures = market_structures()
            assessment_projection = project_assessment
        else:
            retrieval = RetrievalService(
                selected_provider
                or FixtureMarketProvider.from_path(snapshot_path()), sessions,
            )
            structures = _market_goldens()
        market_structure = MarketStructureService(
            FixtureMarketStructureProposer(structures), sessions
        )
        fit = FitService(
            market_structure,
            sessions,
            policy=ordinary_discovery_fit_policy(),
        )
        draft = DraftContractService(
            FixtureDraftProposer(_fixture_draft_map(claims)), sessions
        )
    elif mode == "gemini":
        source_interpretation = build_gemini_source_interpretation(sessions)
        extraction = ExtractionService(
            GeminiProposerV2(),
            sessions,
            gate_policy_version=LOOP1_V2_GATE_POLICY_VERSION,
        )
        retrieval = RetrievalService(
            selected_provider
            or build_market_provider(fixture_path=str(snapshot_path())),
            sessions,
        )
        market_structure = MarketStructureService(
            GeminiMarketStructureProposer(),
            sessions,
            top_n=market_structure_top_n(),
        )
        fit = FitService(
            market_structure,
            sessions,
            policy=ordinary_discovery_fit_policy(),
            cap_initial=cap_initial(),
            cap_expanded=cap_expanded(),
        )
        draft = DraftContractService(GeminiDraftProposer(), sessions)
    elif mode == "vertex-canary":
        assert bounded is not None
        model = bounded.profile.model
        source_interpretation = SourceInterpretationService(
            GeminiSourceInterpreter(model=model, operation=bounded), sessions
        )
        extraction = ExtractionService(
            GeminiProposerV2(model=model, operation=bounded),
            sessions,
            gate_policy_version=LOOP1_V2_GATE_POLICY_VERSION,
        )
        retrieval = RetrievalService(
            selected_provider
            or FixtureMarketProvider.from_path(snapshot_path()),
            sessions,
        )
        market_structure = MarketStructureService(
            VertexCanaryMarketStructureProposer(model=model, operation=bounded),
            sessions,
            top_n=3 if custom_answer_profile else 1,
        )
        fit = FitService(
            market_structure,
            sessions,
            policy=ordinary_discovery_fit_policy(),
            cap_initial=3 if custom_answer_profile else 1,
            cap_expanded=3 if custom_answer_profile else 1,
        )
        # Draft generation is outside the three authorized canary slots.
        draft = DraftContractService(FixtureDraftProposer({}), sessions)
    else:
        raise ValueError(f"unknown FITCHECK_UI_MODE: {mode!r}")
    market_pool = MarketPoolService(
        retrieval, fit, sessions, assessment_projection=assessment_projection,
    )
    jobs = JobStore(sessions)
    source_interpretation_jobs = SourceInterpretationJobService(
        jobs=jobs,
        source_interpretation=source_interpretation,
        session_factory=sessions,
    )
    return ProductServices(
        mode=mode,
        source_interpretation=source_interpretation,
        source_interpretation_jobs=source_interpretation_jobs,
        jobs=jobs,
        extraction=extraction,
        retrieval=retrieval,
        fit=fit,
        market_pool=market_pool,
        draft=draft,
        ledger=LedgerService(sessions),
        session_factory=sessions,
    )
