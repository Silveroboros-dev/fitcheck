"""Composite-v2 sidecar coverage evaluator.

Schema v1 remains frozen: production claim/market structures still carry only
``mechanism.is_composite``. This module is a Loop 4 candidate path that can be
enabled from eval fixtures by passing a CompositeCoveragePolicy into FitPolicy.
Without that sidecar, X2 keeps the existing conservative indirect cap.
"""

import json
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from el.domain.enums import EventStage, FitClass, ResolutionSourceClass, Stance
from el.domain.structures import (
    ClaimHorizon,
    Entity,
    ExtractedStructure,
    MarketHorizon,
    MarketStructure,
    Mechanism,
    Metric,
)
from el.fitgate.aliases import ALIAS_RULES_VERSION

COMPOSITE_COVERAGE_VERSION = "composite-v2-candidate"


class CompositeOperator(StrEnum):
    ALL = "all"
    ANY = "any"
    K_OF_N = "k_of_n"
    SEQUENCE = "sequence"
    UNKNOWN = "unknown"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ClaimCompositeLeg(_Model):
    leg_id: str = Field(min_length=1)
    entities: list[Entity] = Field(min_length=1)
    event_stage: EventStage
    metric: Metric
    horizon: ClaimHorizon
    threshold: str | None = None
    direction: str | None = None
    stance: Stance = Stance.YES
    resolution_source_class: ResolutionSourceClass


class MarketCompositeLeg(_Model):
    leg_id: str = Field(min_length=1)
    entities: list[Entity] = Field(min_length=1)
    event_stage: EventStage
    metric: Metric
    horizon: MarketHorizon
    threshold: str | None = None
    direction: str | None = None
    resolution_source_class: ResolutionSourceClass


class ClaimCompositeSpec(_Model):
    claim_summary: str = Field(min_length=1)
    operator: CompositeOperator
    k: int | None = None
    ordered: bool = False
    legs: list[ClaimCompositeLeg] = Field(min_length=1)


class MarketCompositeSpec(_Model):
    market_id: str = Field(min_length=1)
    operator: CompositeOperator
    k: int | None = None
    ordered: bool = False
    legs: list[MarketCompositeLeg] = Field(min_length=1)


class CompositeCoveragePolicy(_Model):
    version: str = COMPOSITE_COVERAGE_VERSION
    claims: dict[str, ClaimCompositeSpec] = Field(default_factory=dict)
    markets: dict[str, MarketCompositeSpec] = Field(default_factory=dict)


class CompositeCoverageResult(_Model):
    status: Literal["not_configured", "full_cover", "not_full_cover"]
    detail: str
    matched_pairs: list[tuple[str, str]] = []
    blocking_reasons: list[str] = []


def load_composite_coverage_policy(path: str | Path) -> CompositeCoveragePolicy:
    raw = json.loads(Path(path).read_text())
    return CompositeCoveragePolicy(
        version=raw["version"],
        claims={
            row["claim_summary"]: ClaimCompositeSpec.model_validate(row)
            for row in raw.get("claims", [])
        },
        markets={
            row["market_id"]: MarketCompositeSpec.model_validate(row)
            for row in raw.get("markets", [])
        },
    )


def evaluate_composite_coverage(
    claim: ExtractedStructure,
    market: MarketStructure,
    policy: CompositeCoveragePolicy | None,
    *,
    alias_rules_version: str = ALIAS_RULES_VERSION,
    m1_direction_guard: bool = True,
    m1_subject_only_residual_guard: bool = False,
    horizon_tolerances: dict | None = None,
) -> CompositeCoverageResult:
    """Return whether explicit sidecar data proves full composite coverage."""
    if policy is None:
        return CompositeCoverageResult(
            status="not_configured",
            detail="composite-v2 sidecar policy is not enabled",
        )
    claim_spec = policy.claims.get(claim.claim_summary)
    market_spec = policy.markets.get(market.market_id)
    if claim_spec is None or market_spec is None:
        return CompositeCoverageResult(
            status="not_configured",
            detail=(
                "missing composite-v2 sidecar for "
                f"claim={claim.claim_summary!r} market={market.market_id!r}"
            ),
        )

    op_ok, op_reason = _operator_compatible(claim_spec, market_spec)
    if not op_ok:
        return CompositeCoverageResult(
            status="not_full_cover",
            detail=op_reason,
            blocking_reasons=[op_reason],
        )

    matched = _match_all_legs(
        claim_spec,
        market_spec,
        alias_rules_version=alias_rules_version,
        m1_direction_guard=m1_direction_guard,
        m1_subject_only_residual_guard=m1_subject_only_residual_guard,
        horizon_tolerances=horizon_tolerances,
    )
    if matched is None:
        return CompositeCoverageResult(
            status="not_full_cover",
            detail="not every claim leg has a distinct matching market leg",
            blocking_reasons=["leg_coverage_incomplete"],
        )

    used_market_legs = {market_leg for _, market_leg in matched}
    extra_legs = [
        leg.leg_id for leg in market_spec.legs if leg.leg_id not in used_market_legs
    ]
    if extra_legs:
        return CompositeCoverageResult(
            status="not_full_cover",
            detail=f"unmatched restrictive market legs: {extra_legs}",
            matched_pairs=matched,
            blocking_reasons=[f"unmatched_market_leg:{leg}" for leg in extra_legs],
        )

    return CompositeCoverageResult(
        status="full_cover",
        detail=(
            f"composite-v2 {claim_spec.operator}/{market_spec.operator} "
            f"full coverage with {len(matched)} matched legs"
        ),
        matched_pairs=matched,
    )


def _operator_compatible(
    claim: ClaimCompositeSpec, market: MarketCompositeSpec
) -> tuple[bool, str]:
    if claim.operator is CompositeOperator.UNKNOWN:
        return False, "claim composite operator is unknown"
    if market.operator is CompositeOperator.UNKNOWN:
        return False, "market composite operator is unknown"
    if claim.operator is not market.operator:
        return (
            False,
            f"operator mismatch: claim {claim.operator} vs market {market.operator}",
        )
    if claim.operator is CompositeOperator.K_OF_N and claim.k != market.k:
        return False, f"k_of_n mismatch: claim k={claim.k} vs market k={market.k}"
    if claim.operator is CompositeOperator.SEQUENCE and not (
        claim.ordered and market.ordered
    ):
        return False, "sequence operator requires ordered claim and market legs"
    return True, "operator compatible"


def _match_all_legs(
    claim: ClaimCompositeSpec,
    market: MarketCompositeSpec,
    *,
    alias_rules_version: str,
    m1_direction_guard: bool,
    m1_subject_only_residual_guard: bool,
    horizon_tolerances: dict | None,
) -> list[tuple[str, str]] | None:
    if len(market.legs) < len(claim.legs):
        return None

    if claim.operator is CompositeOperator.SEQUENCE:
        if len(claim.legs) != len(market.legs):
            return None
        pairs: list[tuple[str, str]] = []
        for claim_leg, market_leg in zip(claim.legs, market.legs):
            ok, _ = _leg_matches(
                claim_leg,
                market_leg,
                alias_rules_version=alias_rules_version,
                m1_direction_guard=m1_direction_guard,
                m1_subject_only_residual_guard=m1_subject_only_residual_guard,
                horizon_tolerances=horizon_tolerances,
            )
            if not ok:
                return None
            pairs.append((claim_leg.leg_id, market_leg.leg_id))
        return pairs

    return _search_leg_matching(
        claim.legs,
        market.legs,
        alias_rules_version=alias_rules_version,
        m1_direction_guard=m1_direction_guard,
        m1_subject_only_residual_guard=m1_subject_only_residual_guard,
        horizon_tolerances=horizon_tolerances,
        used=set(),
        index=0,
        pairs=[],
    )


def _search_leg_matching(
    claim_legs: list[ClaimCompositeLeg],
    market_legs: list[MarketCompositeLeg],
    *,
    alias_rules_version: str,
    m1_direction_guard: bool,
    m1_subject_only_residual_guard: bool,
    horizon_tolerances: dict | None,
    used: set[int],
    index: int,
    pairs: list[tuple[str, str]],
) -> list[tuple[str, str]] | None:
    if index == len(claim_legs):
        return pairs
    claim_leg = claim_legs[index]
    for market_index, market_leg in enumerate(market_legs):
        if market_index in used:
            continue
        ok, _ = _leg_matches(
            claim_leg,
            market_leg,
            alias_rules_version=alias_rules_version,
            m1_direction_guard=m1_direction_guard,
            m1_subject_only_residual_guard=m1_subject_only_residual_guard,
            horizon_tolerances=horizon_tolerances,
        )
        if not ok:
            continue
        found = _search_leg_matching(
            claim_legs,
            market_legs,
            alias_rules_version=alias_rules_version,
            m1_direction_guard=m1_direction_guard,
            m1_subject_only_residual_guard=m1_subject_only_residual_guard,
            horizon_tolerances=horizon_tolerances,
            used=used | {market_index},
            index=index + 1,
            pairs=pairs + [(claim_leg.leg_id, market_leg.leg_id)],
        )
        if found is not None:
            return found
    return None


def _leg_matches(
    claim_leg: ClaimCompositeLeg,
    market_leg: MarketCompositeLeg,
    *,
    alias_rules_version: str,
    m1_direction_guard: bool,
    m1_subject_only_residual_guard: bool,
    horizon_tolerances: dict | None,
) -> tuple[bool, list[str]]:
    from el.fitgate.checks import (
        check_event_stage_match,
        check_horizon_tolerance,
        check_metric_lexical_floor,
        check_objectivity_conflict,
        check_outcome_polarity,
        check_subject_lexical_floor,
    )

    claim = _claim_from_leg(claim_leg)
    market = _market_from_leg(market_leg)
    horizon = (
        check_horizon_tolerance(claim, market)
        if horizon_tolerances is None
        else check_horizon_tolerance(claim, market, horizon_tolerances)
    )
    if m1_subject_only_residual_guard:
        from el.fitgate.m1_subject_only import check_subject_only_metric_lexical_floor

        metric = check_subject_only_metric_lexical_floor(
            claim, market, alias_rules_version, m1_direction_guard
        )
    else:
        metric = check_metric_lexical_floor(
            claim, market, alias_rules_version, m1_direction_guard
        )
    outcomes = [
        check_subject_lexical_floor(claim, market),
        check_event_stage_match(claim, market),
        metric,
        horizon,
        check_objectivity_conflict(claim, market),
        check_outcome_polarity(claim, market),
    ]
    reasons = [outcome.detail for outcome in outcomes if outcome.cap is not None]
    if claim_leg.threshold != market_leg.threshold:
        reasons.append(
            f"threshold mismatch: claim {claim_leg.threshold!r} vs "
            f"market {market_leg.threshold!r}"
        )
    if claim_leg.direction != market_leg.direction:
        reasons.append(
            f"direction mismatch: claim {claim_leg.direction!r} vs "
            f"market {market_leg.direction!r}"
        )
    if claim_leg.resolution_source_class is not market_leg.resolution_source_class:
        reasons.append(
            "source-class mismatch: claim "
            f"{claim_leg.resolution_source_class} vs market "
            f"{market_leg.resolution_source_class}"
        )
    return not reasons, reasons


def _claim_from_leg(leg: ClaimCompositeLeg) -> ExtractedStructure:
    return ExtractedStructure(
        claim_summary=leg.leg_id,
        entities=leg.entities,
        event_stage=leg.event_stage,
        metric=leg.metric,
        horizon=leg.horizon,
        mechanism=Mechanism(),
        stance=leg.stance,
        resolution_source_class=leg.resolution_source_class,
        contractible_version=leg.leg_id,
    )


def _market_from_leg(leg: MarketCompositeLeg) -> MarketStructure:
    return MarketStructure(
        market_id=leg.leg_id,
        snapshot_id="composite-v2-sidecar",
        event_stage=leg.event_stage,
        metric=leg.metric,
        horizon=leg.horizon,
        entities=leg.entities,
        threshold=leg.threshold,
        direction=leg.direction,
        resolution_source_class=leg.resolution_source_class,
        extraction_policy_version=2,
    )
