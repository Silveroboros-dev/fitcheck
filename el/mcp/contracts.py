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
from typing import ClassVar

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
