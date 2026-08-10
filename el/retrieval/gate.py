"""Loop 2 candidate-eligibility gate (deterministic, spec v2 loop table).

Checks: taxonomy confidence, liquidity floor, horizon window. Each check
returns pass / fail / unknown:

- fail → candidate is marked ineligible with an `excluded_reason`;
- unknown (field absent from the provider) → flagged, never excluded.
  The gate filters KNOWN-bad candidates; it never guesses. Missing data
  is candidate evidence for Loop 3, not grounds for silent exclusion.

Nothing is dropped: every retrieved candidate is persisted with its
flags (rejected-market auditability, blueprint §5).
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from el.domain.structures import ExtractedStructure
from el.retrieval.provider import CandidateMarketRecord

GATE_POLICY_VERSION = "loop2-gate-v1"


@dataclass(frozen=True)
class Loop2Policy:
    min_liquidity_usd: float = 1000.0
    min_taxonomy_confidence: float = 0.5
    horizon_slack_days: int = 366


class EligibilityVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    eligible: bool
    flags: dict[str, str]
    excluded_reason: str | None = None


def evaluate_eligibility(
    record: CandidateMarketRecord,
    structure: ExtractedStructure,
    policy: Loop2Policy = Loop2Policy(),
) -> EligibilityVerdict:
    flags: dict[str, str] = {}
    failures: list[str] = []

    if record.liquidity_usd is None:
        flags["liquidity"] = "unknown"
    elif record.liquidity_usd < policy.min_liquidity_usd:
        flags["liquidity"] = "fail"
        failures.append("liquidity_below_floor")
    else:
        flags["liquidity"] = "pass"

    if record.taxonomy_low_confidence is None and record.taxonomy_confidence is None:
        flags["taxonomy"] = "unknown"
    elif record.taxonomy_low_confidence or (
        record.taxonomy_confidence is not None
        and record.taxonomy_confidence < policy.min_taxonomy_confidence
    ):
        flags["taxonomy"] = "fail"
        failures.append("taxonomy_low_confidence")
    else:
        flags["taxonomy"] = "pass"

    if record.close_date is None:
        flags["horizon"] = "unknown"
    else:
        window_start = structure.horizon.window_start
        window_end = structure.horizon.window_end
        if window_start and record.close_date < window_start:
            flags["horizon"] = "fail"
            failures.append("closes_before_claim_window")
        elif (record.close_date - window_end).days > policy.horizon_slack_days:
            flags["horizon"] = "fail"
            failures.append("closes_beyond_horizon_slack")
        else:
            flags["horizon"] = "pass"

    return EligibilityVerdict(
        eligible=not failures,
        flags=flags,
        excluded_reason=failures[0] if failures else None,
    )
