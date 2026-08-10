"""Loop 3 deterministic policy — ceiling mapping and thesis aggregation.

Blueprint §14 (ratification item 9). The ceiling is THE deterministic
class: a pure, run-invariant function of the two structures under a
versioned policy. Authority split (persisted, external-review
amendment):

    deterministic_ceiling = pure gate output (this module)
    advisory_caps         = model condition failures through the SAME
                            cap table (gate.py, build commit 8)
    published_fit_class   = min(ceiling, advisory_caps)

Until the advisory merge lands, published == ceiling and
authority == deterministic_only. The model may veto display strength;
the ceiling remains the auditable gate output. Claim discipline: the
hard-zero claim attaches to the CEILING.

Mapping rules:
- start at `direct`; every cap lowers (min over the class order);
- caps are commutative — check ordering is build sequencing, not
  semantics;
- stacking: >= `stacking_threshold` HARD fails -> no_clean_expression
  (wrong on two independent dimensions is not a proxy).

This module must stay model-free (CI structural check).
"""

import uuid
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from el.domain.enums import FitClass, HorizonMatch, ResolutionRisk
from el.domain.structures import ExtractedStructure, MarketStructure
from el.fitgate.aliases import ALIAS_RULES_EMPTY, ALIAS_RULES_VERSION
from el.fitgate.checks import (
    ALL_CHECKS,
    HORIZON_TOLERANCES_V1,
    TOKEN_RULES_VERSION,
    check_composite_single_leg,
    CheckOutcome,
    check_horizon_tolerance,
    check_metric_lexical_floor,
)

FIT_GATE_POLICY_VERSION = "loop3-v1"

# direct > indirect > weak_proxy > no_clean_expression
_CLASS_RANK = {
    FitClass.DIRECT: 3,
    FitClass.INDIRECT: 2,
    FitClass.WEAK_PROXY: 1,
    FitClass.NO_CLEAN_EXPRESSION: 0,
}


def class_rank(fit_class: FitClass) -> int:
    return _CLASS_RANK[fit_class]


# The recommendable set: aggregate_thesis recommends a market only at direct or
# indirect (weak/no-clean are surfaced and refused). A weak/no-clean thesis
# published as EITHER is a "false strong recommendation" — the product's
# critical failure mode (label_guide.md §front-matter). WEAK_CLASSES is its
# complement. Single source for both the deterministic eval gate and the
# advisory false-strong check.
STRONG_CLASSES = frozenset({FitClass.DIRECT, FitClass.INDIRECT})
WEAK_CLASSES = frozenset({FitClass.WEAK_PROXY, FitClass.NO_CLEAN_EXPRESSION})


def weaker_of(a: FitClass, b: FitClass) -> FitClass:
    return a if _CLASS_RANK[a] <= _CLASS_RANK[b] else b


@dataclass(frozen=True)
class FitPolicy:
    """loop3-v1 constants. Governed policy data: changes go through
    Loop 4 with a before/after eval delta, never a quiet edit."""

    stacking_threshold: int = 2
    # Escalation predicate constant (Program 2 seam, log-only in v1).
    escalation_confidence_floor: float = 0.6
    # claim precision -> (good_days, fair_days); beyond fair = poor.
    # Single source: checks.HORIZON_TOLERANCES_V1.
    horizon_tolerances: dict = field(
        default_factory=lambda: dict(HORIZON_TOLERANCES_V1)
    )
    # Governed metric-synonym alias table M1 consults. PROMOTED to alias-v2
    # on 2026-06-13 via the verifier's recorded GO
    # (evals/promotions/rt_003_synonym_undercall_alias-v2.json): v2 keeps the
    # rt_003 sales~revenue resolution but is UNSTEMMED + structural, closing
    # the alias-v1 transaction-sale leak (external review: "stake sale
    # contraction" et al. cleared M1 against "revenue decline"). alias-v1 is
    # retained as a named historical version; ALIAS_RULES_EMPTY remains the
    # verifier's "before". Bumping again is another Loop 4 promotion + delta.
    alias_rules_version: str = ALIAS_RULES_VERSION
    # M1 floor direction-token guard (governed Step 5 / Loop 4 patch,
    # m1-direction-guard-v1): when True, change/polarity tokens (decline,
    # increase, drop, ...) are subtracted from M1's raw overlap before the
    # floor clears, so a shared "decline" alone no longer passes (market share
    # decline vs revenue decline). False = the verifier's before-baseline.
    # Artifact: evals/promotions/m1_direction_token_floor_guard_v1.json.
    m1_direction_guard: bool = True
    # Composite-v2 sidecar coverage is a Loop 4 candidate path only. None keeps
    # schema-v1 X2 behavior: all composite claims cap at indirect.
    composite_coverage: object | None = None


class Authority(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # deterministic_only | deterministic_with_advisory_veto |
    # deterministic_fallback — persisted on every judgment so no
    # published class can masquerade as purely deterministic when an
    # advisory veto changed it.
    mode: str


AUTHORITY_DETERMINISTIC_ONLY = "deterministic_only"
AUTHORITY_ADVISORY_VETO = "deterministic_with_advisory_veto"
AUTHORITY_FALLBACK = "deterministic_fallback"


class MarketFitVerdict(BaseModel):
    """Per-market judgment: the full check vector plus derived classes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    market_id: str
    snapshot_id: str
    checks: list[CheckOutcome]
    hard_fail_count: int
    deterministic_ceiling: FitClass
    published: FitClass
    horizon_match: HorizonMatch | None = None
    resolution_risk: ResolutionRisk | None = None
    thesis_side: str | None = None  # yes | no | unknown (P1)
    authority: str = AUTHORITY_DETERMINISTIC_ONLY
    gate_policy_version: str = FIT_GATE_POLICY_VERSION
    token_rules_version: str = TOKEN_RULES_VERSION
    alias_rules_version: str = ALIAS_RULES_EMPTY

    def fired(self) -> list[str]:
        return [c.check_id for c in self.checks if c.cap is not None]


class ThesisFit(BaseModel):
    """Thesis-level outcome over one candidate pool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fit_class: FitClass
    recommended_market_id: str | None
    rejected: list[MarketFitVerdict]
    draft_contract_recommended: bool
    gate_policy_version: str = FIT_GATE_POLICY_VERSION


def compute_ceiling(
    outcomes: list[CheckOutcome], policy: FitPolicy
) -> tuple[FitClass, int]:
    ceiling = FitClass.DIRECT
    hard_fails = 0
    for outcome in outcomes:
        if outcome.cap is not None:
            ceiling = weaker_of(ceiling, outcome.cap)
        if outcome.hard:
            hard_fails += 1
    if hard_fails >= policy.stacking_threshold:
        ceiling = weaker_of(ceiling, FitClass.NO_CLEAN_EXPRESSION)
    return ceiling, hard_fails


def evaluate_market(
    claim: ExtractedStructure,
    market: MarketStructure,
    policy: FitPolicy = FitPolicy(),
) -> MarketFitVerdict:
    outcomes: list[CheckOutcome] = []
    for check in ALL_CHECKS:
        if check is check_horizon_tolerance:
            outcomes.append(check(claim, market, policy.horizon_tolerances))
        elif check is check_metric_lexical_floor:
            outcomes.append(
                check(
                    claim,
                    market,
                    policy.alias_rules_version,
                    policy.m1_direction_guard,
                )
            )
        elif check is check_composite_single_leg:
            outcomes.append(
                check(
                    claim,
                    market,
                    policy.composite_coverage,
                    policy.alias_rules_version,
                    policy.m1_direction_guard,
                    policy.horizon_tolerances,
                )
            )
        else:
            outcomes.append(check(claim, market))
    ceiling, hard_fails = compute_ceiling(outcomes, policy)
    annotations: dict[str, str] = {}
    for outcome in outcomes:
        annotations.update(outcome.annotations)
    horizon_match = annotations.get("horizon_match")
    resolution_risk = annotations.get("resolution_risk")
    return MarketFitVerdict(
        market_id=market.market_id,
        snapshot_id=market.snapshot_id,
        checks=outcomes,
        hard_fail_count=hard_fails,
        deterministic_ceiling=ceiling,
        published=ceiling,
        horizon_match=HorizonMatch(horizon_match) if horizon_match else None,
        resolution_risk=(
            ResolutionRisk(resolution_risk) if resolution_risk else None
        ),
        thesis_side=annotations.get("thesis_side"),
        alias_rules_version=policy.alias_rules_version,
    )


def aggregate_thesis(
    verdicts: list[tuple[MarketFitVerdict, int]],
) -> ThesisFit:
    """Best per-market class wins; ties break by Loop 2 rank.

    Recommendations exist only at direct/indirect (A4: weak proxies are
    surfaced and refused, never recommended). Weak/no-clean -> no
    recommendation + draft-contract flag. Every evaluated
    non-recommended market is a rejection with reasons.
    """
    if not verdicts:
        return ThesisFit(
            fit_class=FitClass.NO_CLEAN_EXPRESSION,
            recommended_market_id=None,
            rejected=[],
            draft_contract_recommended=True,
        )
    ordered = sorted(
        verdicts, key=lambda vr: (-class_rank(vr[0].published), vr[1])
    )
    best, _ = ordered[0]
    if best.published in (FitClass.DIRECT, FitClass.INDIRECT):
        return ThesisFit(
            fit_class=best.published,
            recommended_market_id=best.market_id,
            rejected=[v for v, _ in ordered[1:]],
            draft_contract_recommended=False,
        )
    return ThesisFit(
        fit_class=best.published,
        recommended_market_id=None,
        rejected=[v for v, _ in ordered],
        draft_contract_recommended=True,
    )


def new_trace_id() -> str:
    # Run UUID until Phoenix wiring lands (blueprint §9).
    return str(uuid.uuid4())
