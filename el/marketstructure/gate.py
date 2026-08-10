"""Market-structure gate — contract-side schema gate (blueprint §4).

Market-side extraction is itself a proposer + gate: a model proposes the
normalization, this gate validates it. Verdicts:

- PASS: schema-valid, identity matches the request, vocabulary-clean.
- REJECTED_INVALID: failed re-validation, identity mismatch (a proposer
  must never answer about a different market than it was asked), or
  restricted vocabulary (A7 applies to schemas too).

Rejected proposals never persist; they surface in the service outcome
(Loop 4 review wiring lands with build-order step 8).
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError

from el.domain.structures import MarketStructure
from el.domain.vocabulary import vocabulary_violations
from el.models.market_adapter import MARKET_EXTRACTION_POLICY_VERSION

GATE_POLICY_VERSION = "mktstruct-v1"


class MarketGateVerdict(StrEnum):
    PASS = "pass"
    REJECTED_INVALID = "rejected_invalid"


class MarketStructureResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: MarketGateVerdict
    structure: MarketStructure | None = None
    reasons: list[str] = []
    gate_policy_version: str = GATE_POLICY_VERSION


def market_structure_gate(
    *,
    market_id: str,
    snapshot_id: str,
    structure: MarketStructure,
) -> MarketStructureResult:
    # 1. Re-validate — the gate never trusts the adapter (defense in
    #    depth, same rule as Loop 1).
    try:
        validated = MarketStructure.model_validate(
            structure.model_dump(mode="json")
        )
    except ValidationError as e:
        return MarketStructureResult(
            verdict=MarketGateVerdict.REJECTED_INVALID,
            reasons=[
                f"structure failed re-validation: {e.error_count()} errors"
            ],
        )

    # 1.5 Extraction policy must be current. v1 semantics (notably
    #     settlement-date horizons) must never masquerade as v2
    #     (condition-deadline) — that is exactly how the deadline-vs-
    #     settlement distinction would re-enter through the back door.
    if validated.extraction_policy_version != MARKET_EXTRACTION_POLICY_VERSION:
        return MarketStructureResult(
            verdict=MarketGateVerdict.REJECTED_INVALID,
            reasons=[
                "stale extraction policy: structure is v"
                f"{validated.extraction_policy_version}, gate requires v"
                f"{MARKET_EXTRACTION_POLICY_VERSION}"
            ],
        )

    # 2. Identity must match the request.
    if validated.market_id != market_id or validated.snapshot_id != snapshot_id:
        return MarketStructureResult(
            verdict=MarketGateVerdict.REJECTED_INVALID,
            reasons=[
                "identity mismatch: proposer answered for "
                f"({validated.market_id}, {validated.snapshot_id}), asked "
                f"({market_id}, {snapshot_id})"
            ],
        )

    # 3. Vocabulary gate over every text field (A7 applies to schemas).
    text = " ".join(
        filter(
            None,
            [
                validated.metric.what,
                validated.metric.measured_by,
                validated.threshold,
                validated.direction,
                *[entity.name for entity in validated.entities],
            ],
        )
    )
    violations = vocabulary_violations(text)
    if violations:
        return MarketStructureResult(
            verdict=MarketGateVerdict.REJECTED_INVALID,
            reasons=[f"restricted vocabulary: {', '.join(violations)}"],
        )

    return MarketStructureResult(
        verdict=MarketGateVerdict.PASS, structure=validated
    )
