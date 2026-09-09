"""MCP tool response models + typed errors (step 7).

Each response declares a FIXED `SOURCE_PATHS` (controlled by contract code,
never by user input or model output): the index-agnostic dotted paths the A7
guard treats as quoted SOURCE and exempts. Per the session-6 ruling, ONLY raw
literal source is exempt — input text and entity names. System-generated
summaries (normalized_claim_summary, thesis_summary), captures/misses,
recommendations, reasons, and labels are CHECKED.
"""

import uuid
from datetime import date, datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict


class McpError(Exception):
    """Base for typed MCP tool errors (the transport maps these to errors)."""

    code: ClassVar[str] = "mcp_error"


class BlindPriorRequired(McpError):
    code: ClassVar[str] = "blind_prior_required"

    def __init__(self, thesis_analysis_id: uuid.UUID):
        self.thesis_analysis_id = thesis_analysis_id
        super().__init__(
            "odds are withheld until a blind prior is submitted for this "
            f"thesis ({thesis_analysis_id}); call submit_blind_prior first"
        )


class NotFound(McpError):
    """Object missing OR not owned by the principal — same response, no
    existence leak (covers cross-user/object access by guessed id)."""

    code: ClassVar[str] = "not_found"


class SaveRejected(McpError):
    code: ClassVar[str] = "save_rejected"

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


class ToolRefused(McpError):
    """The underlying gate refused (e.g. insider screen, schema)."""

    code: ClassVar[str] = "refused"

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class Conflict(McpError):
    """The requested transition conflicts with immutable or stale state."""

    code: ClassVar[str] = "conflict"


class IdempotencyConflictError(Conflict):
    code: ClassVar[str] = "idempotency_conflict"


class Unavailable(McpError):
    """The configured proposer cannot handle the requested fixture/input."""

    code: ClassVar[str] = "service_unavailable"


class _McpOut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset()


class NormalizeResult(_McpOut):
    thesis_analysis_id: uuid.UUID
    normalized_claim_summary: str
    extracted_structure: dict
    input_text: str
    # Raw user text + literal entity names are quoted source; the normalized
    # summary + extracted metric/contractible fields are system-generated.
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"input_text", "extracted_structure.entities[].name"}
    )


class RejectedMarketOut(_McpOut):
    market_id: str
    reason: str


class FitCardResult(_McpOut):
    fit_card_id: uuid.UUID
    thesis_analysis_id: uuid.UUID
    candidate_set_id: uuid.UUID
    semantic_fit_class: str
    recommended_market_id: str | None
    current_odds: float | None
    odds_side: str | None
    what_it_captures: str
    what_it_misses: str
    horizon_match: str
    resolution_risk: str
    fit_confidence: float | None
    draft_contract_recommended: bool
    rejected_markets: list[RejectedMarketOut]
    # Trimmed, odds-free provenance + MCP markers (the persisted blob carries
    # no price; we surface only safe system fields). For preview, current_odds
    # is None and odds_withheld is True.
    provenance: dict
    # All fields are system-generated (ids/enums/our copy); nothing exempt.
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset()


class DraftResult(_McpOut):
    generated: bool
    gate_verdict: str
    # A gate-PASS draft is a "shape-valid draft candidate", NOT a verified
    # clean expression (claim discipline) — surfaced explicitly, not implied
    # by gate_verdict=pass.
    label: str | None = None
    draft_contract_id: uuid.UUID | None = None
    proposed_title: str | None = None
    proposed_resolution_logic: str | None = None
    resolution_source: str | None = None
    resolution_source_class: str | None = None
    resolution_deadline: date | None = None
    subject_entity: str | None = None
    # The draft is our generated content (the draft gate already A7-checks it);
    # nothing exempt.
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset()


class LedgerEntryResult(_McpOut):
    id: uuid.UUID
    thesis_analysis_id: uuid.UUID
    thesis_summary: str
    user_justification: str
    linked_market_id: str | None
    odds_at_entry: float | None
    odds_at_entry_side: str | None
    fit_class: str
    attestation_status: str
    status: str
    client_type: str
    created_at: datetime
    # The user's own justification is raw source; the thesis_summary is a
    # system-generated summary and is CHECKED (ruling).
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset({"user_justification"})


class BlindPriorResult(_McpOut):
    thesis_analysis_id: uuid.UUID
    conviction_event_id: uuid.UUID | None
    status: str  # created | updated | locked


class ReviewCandidateResult(_McpOut):
    review_candidate_id: uuid.UUID
    object_type: str
    object_id: uuid.UUID
    source: str
    status: str


# Product Discovery v3.1 successor responses. These are additive: the legacy
# result models above retain their historical meanings.


class V3SourceThesisCandidateResult(_McpOut):
    source_thesis_candidate_id: uuid.UUID
    ordinal: int
    selected_source_quote: str
    source_quote_digest: str
    claim_summary: str
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"selected_source_quote"}
    )


class V3SourceInterpretationResult(_McpOut):
    source_interpretation_id: uuid.UUID
    outcome: str
    input_digest: str | None
    reasons: list[str]
    prompt_policy_version: str
    system_variant_id: str
    model_adapter: str | None
    model_run_id: str | None
    candidates: list[V3SourceThesisCandidateResult]
    created_at: datetime
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"candidates[].selected_source_quote"}
    )


class V3SourceInterpretationJobResult(_McpOut):
    job_id: uuid.UUID
    source_interpretation_request_id: uuid.UUID
    input_digest: str | None
    status: str
    stage: str | None
    created: bool | None
    error_code: str | None
    safe_error_message: str | None
    interpretation: V3SourceInterpretationResult | None
    created_at: datetime
    updated_at: datetime
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"interpretation.candidates[].selected_source_quote"}
    )


class _AgentRelayDecision(_McpOut):
    # MCP authenticates the agent principal. A separate client trace must bind
    # any human message; the tool call is never direct human attestation.
    decision_origin: Literal["agent_relay"] = "agent_relay"
    human_attestation: Literal[False] = False


class V3SourceCandidateChoiceResult(_AgentRelayDecision):
    source_candidate_choice_id: uuid.UUID
    source_interpretation_id: uuid.UUID
    selection_kind: str
    source_thesis_candidate_id: uuid.UUID | None
    created_at: datetime

class V3NormalizationAttemptResult(_McpOut):
    normalization_attempt_id: uuid.UUID
    predecessor_attempt_id: uuid.UUID | None
    source_interpretation_id: uuid.UUID | None
    source_thesis_candidate_id: uuid.UUID | None
    outcome: str
    verdict: str
    input_digest: str | None
    normalized_claim_summary: str | None
    extracted_structure: dict | None
    clarifying_question: str | None
    reasons: list[str]
    gate_policy_version: str
    prompt_policy_version: str
    system_variant_id: str
    model_adapter: str | None
    model_run_id: str | None
    created_at: datetime
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"extracted_structure.entities[].name"}
    )


class V3NormalizationDecisionResult(_AgentRelayDecision):
    normalization_decision_id: uuid.UUID
    normalization_attempt_id: uuid.UUID
    action: str
    thesis_analysis_id: uuid.UUID | None
    created_at: datetime


class V3MarketAssessmentResult(_McpOut):
    market_assessment_id: uuid.UUID
    candidate_set_member_id: uuid.UUID
    market_id: str
    market_title: str
    resolution_conditions: str
    pair_class: str
    retrieval_rank: int
    display_rank: int
    what_it_captures: str
    what_it_misses: str
    horizon_match: str | None
    resolution_risk: str | None
    fit_confidence: float | None
    authority: str
    snapshot_id: str
    rules_capture_id: uuid.UUID
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"market_title", "resolution_conditions"}
    )


class V3MarketPoolResult(_McpOut):
    market_display_set_id: uuid.UUID
    fit_card_id: uuid.UUID
    thesis_analysis_id: uuid.UUID
    candidate_set_id: uuid.UUID
    snapshot_id: str
    snapshot_as_of: datetime
    display_policy_version: str
    assessed_count: int
    target_count: int
    displayed_count: int
    assessment_complete: bool
    system_pool_outcome: str
    incomplete_reasons: list[str]
    candidate_markets: list[V3MarketAssessmentResult]
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {
            "candidate_markets[].market_title",
            "candidate_markets[].resolution_conditions",
        }
    )


class V3MarketChoiceResult(_AgentRelayDecision):
    market_choice_id: uuid.UUID
    market_display_set_id: uuid.UUID
    selection_kind: str
    market_assessment_id: uuid.UUID | None
    created_at: datetime
