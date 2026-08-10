"""API/MCP boundary contracts for the FitCheck domain.

These are the Pydantic shapes that cross surface boundaries (HTTP + MCP).
Persistence lives in el.domain.tables; the two are kept deliberately
separate so the ORM never leaks into responses.

Binding rules encoded here (not just documented):
- conviction events anchor on thesis_analysis_id; ledger_entry_id is
  nullable and back-filled at save (spec v2 Core Objects);
- a blind prior exists before odds are revealed: prior_type=blind requires
  market_context_seen=False and odds_revealed_at=None at record time;
- a conviction event carries a prior, a conviction, or both — never
  neither.
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from el.domain.enums import (
    AttestationAction,
    AttestationObjectType,
    AttestationStatus,
    ClientType,
    ConvictionLevel,
    ExposureBucket,
    FitClass,
    HorizonMatch,
    LedgerEntryStatus,
    PriorConfidence,
    PriorType,
    ResolutionRisk,
)
from el.domain.structures import ExtractedStructure


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ThesisAnalysisIn(_Contract):
    input_text: str = Field(min_length=1, max_length=20_000)
    source_url: str | None = None
    client_type: ClientType
    agent_client_id: str | None = None


class ThesisAnalysisOut(_Contract):
    id: UUID
    input_text: str
    extracted_structure: ExtractedStructure
    normalized_claim_summary: str
    client_type: ClientType
    created_at: datetime


class ConvictionEventIn(_Contract):
    thesis_analysis_id: UUID
    ledger_entry_id: UUID | None = None
    conviction_level: ConvictionLevel | None = None
    intended_exposure_bucket: ExposureBucket | None = None
    prior_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    prior_confidence: PriorConfidence | None = None
    prior_reason: str | None = Field(default=None, max_length=500)
    prior_type: PriorType
    market_context_seen: bool
    odds_revealed_at: datetime | None = None
    client_type: ClientType
    agent_client_id: str | None = None

    @model_validator(mode="after")
    def _temporal_rules(self) -> "ConvictionEventIn":
        if self.prior_type is PriorType.BLIND:
            if self.market_context_seen:
                raise ValueError(
                    "blind prior cannot have market_context_seen=True"
                )
            if self.odds_revealed_at is not None:
                raise ValueError(
                    "blind prior cannot carry odds_revealed_at"
                )
            if self.prior_probability is None:
                raise ValueError("blind prior requires prior_probability")
        if self.prior_probability is None and self.conviction_level is None:
            raise ValueError(
                "event must carry a prior, a conviction, or both"
            )
        return self


class RejectedMarket(_Contract):
    market_id: str
    reason: str = Field(min_length=1)


class StructureExtractionOut(_Contract):
    """Top-N structure-extraction funnel: the retrieved universe is unbounded,
    but the structured set (and thus the Gemini structure-extraction calls) is
    capped to the highest-ranked eligible candidates. ``skipped_unstructured``
    counts eligible markets the cap kept out of extraction — they stay
    persisted as candidate-set members, just never structured/judged.
    ``structure_cap`` is the active cap (None when unbounded)."""

    retrieved_count: int
    eligible_count: int
    structured_count: int
    skipped_unstructured_count: int
    structure_cap: int | None = None
    cap_policy_version: str | None = None
    expanded: bool = False
    initial_cap: int | None = None
    final_cap: int | None = None
    initial_fit_class: str | None = None
    initial_recommended_market_id: str | None = None
    final_fit_class: str | None = None
    final_recommended_market_id: str | None = None
    expansion_reason: str | None = None


class RetrievalSourceResultOut(_Contract):
    market_id: str = Field(min_length=1, max_length=256)
    source_order: int = Field(ge=1)
    source_score: float | None = Field(default=None, allow_inf_nan=False)


class RetrievalProvenanceOut(_Contract):
    backend: str = Field(min_length=1, max_length=64)
    index_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    backend_request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    source_results: list[RetrievalSourceResultOut]

    @model_validator(mode="after")
    def _source_results_preserve_backend_order(self) -> "RetrievalProvenanceOut":
        orders = [result.source_order for result in self.source_results]
        market_ids = [result.market_id for result in self.source_results]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("retrieval source order must be contiguous")
        if len(market_ids) != len(set(market_ids)):
            raise ValueError("retrieval source markets must be unique")
        return self


class FitCardProvenanceOut(_Contract):
    """The fit-card provenance blob as the service actually persists it
    (el.fitgate.service): the base run/policy fields PLUS the Loop 3
    authority/escalation fields. The strict Provenance model alone forbids
    those extras, so serializing a real card through it fails — this is the
    boundary projection that matches reality. extra='forbid' (from
    _Contract) keeps it honest: a new service-side provenance key fails the
    round-trip test loudly rather than silently dropping."""

    # Base run/policy provenance (mirrors el.domain.structures.Provenance).
    gate_policy_version: str
    extraction_schema_version: int
    market_structure_schema_version: int
    model_adapter: str
    model_run_id: str
    trace_id: str
    eval_pack_version: str
    judged_at: datetime
    # Loop 3 authority split + escalation seam (service _provenance).
    authority: str
    confidence_source: str
    thesis_side: str | None = None
    escalation: dict
    per_market: dict
    # Optional for historical cards created before candidate-index pinning.
    retrieval: RetrievalProvenanceOut | None = None
    # Top-N structure-extraction funnel (bounded structured set / Gemini calls).
    structure_extraction: StructureExtractionOut | None = None


class FitCardOut(_Contract):
    """The signature object (spec v2 §Market Fit Card).

    `current_odds` is None until the blind-prior protocol unlocks it for
    the requesting client (agent surface) or the UI variant reveals it.
    `fit_confidence` is None on a deterministic-fallback card (no
    calibrated source) — never a sentinel, mirrors the persisted column.
    """

    id: UUID
    thesis_analysis_id: UUID
    candidate_set_id: UUID
    semantic_fit_class: FitClass
    recommended_market_id: str | None
    current_odds: float | None = None
    what_it_captures: str
    what_it_misses: str
    horizon_match: HorizonMatch
    resolution_risk: ResolutionRisk
    rejected_markets: list[RejectedMarket]
    fit_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    draft_contract_id: UUID | None = None
    provenance: FitCardProvenanceOut

    @model_validator(mode="after")
    def _no_clean_consistency(self) -> "FitCardOut":
        if (
            self.semantic_fit_class is FitClass.NO_CLEAN_EXPRESSION
            and self.recommended_market_id is not None
        ):
            raise ValueError(
                "no_clean_expression cannot carry a recommended market"
            )
        return self


class DraftContractOut(_Contract):
    """A SHAPE-VALID DRAFT CANDIDATE — not a verified clean expression.

    A gate-PASS draft cleared draftgen-v1's checks (schema, A7 vocabulary,
    deadline anti-drift, subject + object/metric echo, resolution
    observability, event-stage echo). Those are necessary, not sufficient:
    the echo checks trust the proposer's declared structured fields, so the
    prose can still drift from the structure. Presenting this as a "clean"
    or "verified" expression requires the booked structural re-fit
    (ProposedDraft -> DraftMarketStructure -> the deterministic fit gate;
    see el.draftcontract.gate). Until then: "shape-valid draft candidate".
    """

    id: UUID
    thesis_analysis_id: UUID
    proposed_title: str
    proposed_resolution_logic: str
    resolution_source: str
    category: str | None = None
    time_horizon: str | None = None
    # Gate-verified echo fields, first-class for UI/API auditability
    # (not buried in provenance): the deadline checked against the claim
    # window, the resolution source class, and the subject the draft is about.
    resolution_deadline: date | None = None
    resolution_source_class: str | None = None
    subject_entity: str | None = None


class LedgerEntryOut(_Contract):
    id: UUID
    thesis_analysis_id: UUID
    thesis_summary: str
    user_justification: str
    linked_market_id: str | None
    odds_at_entry: float | None
    odds_at_entry_side: str | None  # yes | no | side_unknown
    snapshot_id: str | None
    fit_class: FitClass
    attestation_status: AttestationStatus
    client_type: ClientType
    status: LedgerEntryStatus
    created_at: datetime


class AttestationIn(_Contract):
    object_type: AttestationObjectType
    object_id: UUID
    action: AttestationAction
    notes: str | None = Field(default=None, max_length=1000)
