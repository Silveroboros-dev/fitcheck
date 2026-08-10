"""Closed vocabularies for the FitCheck domain.

Values are ratified in docs/epistemic-ledger-product-spec-v2.md (Core
Objects) and docs/epistemic-ledger-backend-blueprint.md (§4, §5). Changing
any of these is a Loop 4 (promotion-gated) decision, not a refactor.
"""

from enum import StrEnum


class FitClass(StrEnum):
    DIRECT = "direct"
    INDIRECT = "indirect"
    WEAK_PROXY = "weak_proxy"
    NO_CLEAN_EXPRESSION = "no_clean_expression"


class Stance(StrEnum):
    # Deliberately no bullish/bearish: trading vocabulary stays out of the
    # data model (axiom A7 applies to schemas too).
    YES = "yes"
    NO = "no"
    INCREASE = "increase"
    DECREASE = "decrease"
    OUTPERFORM = "outperform"
    UNDERPERFORM = "underperform"
    UNCLEAR = "unclear"


class EventStage(StrEnum):
    ANNOUNCED = "announced"
    LAUNCHED = "launched"
    SHIPPED = "shipped"
    ADOPTED = "adopted"
    MEASURED = "measured"
    RESOLVED = "resolved"


class HorizonPrecision(StrEnum):
    DAY = "day"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class ResolutionSourceClass(StrEnum):
    OFFICIAL = "official"
    LEADERBOARD = "leaderboard"
    FILING = "filing"
    PRESS = "press"
    NONE = "none"


class HorizonMatch(StrEnum):
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"


class ResolutionRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ConvictionLevel(StrEnum):
    EXPLORING = "exploring"
    LEANING = "leaning"
    CONVICTION = "conviction"


class ExposureBucket(StrEnum):
    # Framed as hypothetical risk in all user-facing copy (A7).
    USD_10 = "$10"
    USD_25 = "$25"
    USD_50 = "$50"
    USD_100 = "$100"
    USD_250 = "$250"
    USD_500_PLUS = "$500+"


class PriorType(StrEnum):
    BLIND = "blind"
    CONTEXT = "context"


class PriorConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ClientType(StrEnum):
    HUMAN_UI = "human_ui"
    AGENT_MCP = "agent_mcp"
    API = "api"


class AttestationStatus(StrEnum):
    ATTESTED = "attested"
    UNATTESTED = "unattested"


class AttestationAction(StrEnum):
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    REJECTED = "rejected"


class AttestationObjectType(StrEnum):
    LEDGER_ENTRY = "ledger_entry"
    FIT_CORRECTION = "fit_correction"
    MARKET_REJECTION = "market_rejection"
    DRAFT_CONTRACT = "draft_contract"


class ReviewSource(StrEnum):
    USER_CORRECTION = "user_correction"
    USER_REJECTION = "user_rejection"
    # Agent/api-originated corrections and rejections (the MCP surface): kept
    # distinct from the human user_* sources so the candidate's origin is
    # queryable by source alone, not only via reviewer_notes provenance.
    AGENT_CORRECTION = "agent_correction"
    AGENT_REJECTION = "agent_rejection"
    AGENT_SAVE = "agent_save"
    EVAL_FAILURE = "eval_failure"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    # ACCEPTED = reviewer triaged it as "ready for promotion review" (Loop 4
    # queue, Step 8). It is NOT promoted: turning an accepted correction into a
    # governed change stays the deliberate el.review.promotion path. PROMOTED is
    # the later, separate state once a verifier GO + artifact exist.
    ACCEPTED = "accepted"
    PROMOTED = "promoted"
    REJECTED = "rejected"


class LedgerEntryStatus(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    WITHDRAWN = "withdrawn"
