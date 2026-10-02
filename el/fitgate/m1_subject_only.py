"""Explicit Loop 4 overlay for the owner-adopted M1 successor.

``policy`` and ``checks`` are frozen verifier runtime files.  This module
keeps their loop3-v1 bytes and semantics intact, then replaces only the M1
outcome (and the composite-v2 X2 outcome when that sidecar is enabled) for
the named successor identity.
"""

from dataclasses import dataclass, field

from el.domain.enums import FitClass
from el.domain.structures import ExtractedStructure, MarketStructure
from el.fitgate.aliases import ALIAS_RULES_EMPTY
from el.fitgate.checks import (
    CHANGE_DIRECTION_TOKENS,
    CheckKind,
    CheckOutcome,
    CheckStatus,
    check_metric_lexical_floor,
    metrics_alias_equivalent,
    tok_v1,
)
from el.fitgate.policy import (
    FIT_GATE_POLICY_VERSION,
    FitPolicy,
    MarketFitVerdict,
    ThesisFit,
    aggregate_thesis as _aggregate_thesis_v1,
    compute_ceiling,
    evaluate_market as _evaluate_market_v1,
)

M1_SUBJECT_ONLY_POLICY_VERSION = "loop3-m1-subject-only-v4"


@dataclass(frozen=True)
class M1SubjectOnlyFitPolicy(FitPolicy):
    """The named successor; its guard follows from its identity."""

    gate_policy_version: str = field(
        default=M1_SUBJECT_ONLY_POLICY_VERSION, init=False
    )


def ordinary_discovery_fit_policy() -> M1SubjectOnlyFitPolicy:
    """Policy for new discovery runs; plain ``FitPolicy()`` remains v1."""
    return M1SubjectOnlyFitPolicy()


def gate_policy_version(policy: FitPolicy) -> str:
    """Return an explicit successor identity while accepting legacy policies."""
    return getattr(policy, "gate_policy_version", FIT_GATE_POLICY_VERSION)


def is_m1_subject_only_policy(policy: FitPolicy) -> bool:
    return gate_policy_version(policy) == M1_SUBJECT_ONLY_POLICY_VERSION


def fit_policy_from_pins(
    *,
    gate_policy_version: str,
    stacking_threshold: int,
    escalation_confidence_floor: float,
    horizon_tolerances: dict,
    alias_rules_version: str,
    m1_direction_guard: bool,
) -> FitPolicy:
    """Rebuild a v1 pin without adding a successor-only serialized field."""
    kwargs = dict(
        stacking_threshold=stacking_threshold,
        escalation_confidence_floor=escalation_confidence_floor,
        horizon_tolerances=horizon_tolerances,
        alias_rules_version=alias_rules_version,
        m1_direction_guard=m1_direction_guard,
    )
    if gate_policy_version == FIT_GATE_POLICY_VERSION:
        return FitPolicy(**kwargs)
    if gate_policy_version == M1_SUBJECT_ONLY_POLICY_VERSION:
        return M1SubjectOnlyFitPolicy(**kwargs)
    raise ValueError(f"unsupported fit-gate policy version: {gate_policy_version}")


def check_subject_only_metric_lexical_floor(
    claim: ExtractedStructure,
    market: MarketStructure,
    alias_version: str = ALIAS_RULES_EMPTY,
    direction_guard: bool = True,
) -> CheckOutcome:
    """Apply M1-v4 only after the frozen predecessor floor cleared."""
    baseline = check_metric_lexical_floor(
        claim, market, alias_version, direction_guard
    )
    if baseline.status is not CheckStatus.INCONCLUSIVE:
        return baseline

    # A governed alias remains authoritative before entity-token exclusion.
    if metrics_alias_equivalent(
        claim.metric.what,
        f"{market.metric.what} {market.threshold or ''}",
        alias_version,
    ):
        return baseline

    claim_tokens = tok_v1(claim.metric.what)
    market_tokens = tok_v1(market.metric.what) | tok_v1(market.threshold)
    overlap = claim_tokens & market_tokens
    substantive_overlap = (
        overlap - CHANGE_DIRECTION_TOKENS if direction_guard else overlap
    )
    subject_tokens = set().union(
        *(set(tok_v1(entity.name)) for entity in claim.entities if entity.role == "subject")
    )
    entity_tokens = set().union(
        *(set(tok_v1(entity.name)) for entity in market.entities)
    )
    bridge = substantive_overlap & subject_tokens & entity_tokens
    if not bridge or substantive_overlap != bridge:
        return baseline

    claim_residual = (
        set(claim_tokens) - set(CHANGE_DIRECTION_TOKENS) - subject_tokens
    )
    market_residual = (
        set(market_tokens) - set(CHANGE_DIRECTION_TOKENS) - entity_tokens
    )
    if not claim_residual or not market_residual:
        return CheckOutcome(
            check_id="M1",
            name="metric_lexical_floor",
            kind=CheckKind.NONE,
            status=CheckStatus.UNKNOWN,
            detail=(
                "shared subject/entity tokens alone; one metric has no "
                "substantive non-entity residual, so metric correspondence is unknown"
            ),
        )
    return CheckOutcome(
        check_id="M1",
        name="metric_lexical_floor",
        kind=CheckKind.HARD_CAP,
        status=CheckStatus.FAIL,
        cap=FitClass.WEAK_PROXY,
        hard=True,
        detail=(
            "only shared subject/entity tokens "
            f"{sorted(bridge)}; residual metrics differ "
            f"{sorted(claim_residual)} vs {sorted(market_residual)}"
        ),
    )


def _subject_only_composite_single_leg(
    claim: ExtractedStructure,
    market: MarketStructure,
    policy: FitPolicy,
) -> CheckOutcome:
    """Use the v4 M1 wrapper inside explicit composite-v2 leg matching."""
    from el.fitgate.composite_v2 import evaluate_composite_coverage

    result = evaluate_composite_coverage(
        claim,
        market,
        policy.composite_coverage,
        alias_rules_version=policy.alias_rules_version,
        m1_direction_guard=policy.m1_direction_guard,
        m1_subject_only_residual_guard=True,
        horizon_tolerances=policy.horizon_tolerances,
    )
    if result.status == "full_cover":
        return CheckOutcome(
            check_id="X2",
            name="composite_single_leg",
            kind=CheckKind.NONE,
            status=CheckStatus.PASS,
            detail=result.detail,
        )
    if result.status == "not_full_cover":
        return CheckOutcome(
            check_id="X2",
            name="composite_single_leg",
            kind=CheckKind.SOFT_CAP,
            status=CheckStatus.FAIL,
            cap=FitClass.INDIRECT,
            detail=f"composite-v2 coverage rejected direct: {result.detail}",
        )
    return CheckOutcome(
        check_id="X2",
        name="composite_single_leg",
        kind=CheckKind.SOFT_CAP,
        status=CheckStatus.FAIL,
        cap=FitClass.INDIRECT,
        detail=(
            "composite thesis: no single market distinguishes a conjunction; "
            "a market covering one leg is strong evidence at best, and v1 cannot "
            "verify full-leg coverage"
        ),
    )


def evaluate_market(
    claim: ExtractedStructure,
    market: MarketStructure,
    policy: FitPolicy = FitPolicy(),
) -> MarketFitVerdict:
    """Dispatch v1 unchanged, or overlay successor outcomes by check identity."""
    baseline = _evaluate_market_v1(claim, market, policy)
    if not is_m1_subject_only_policy(policy):
        return baseline

    replacements = {
        "M1": check_subject_only_metric_lexical_floor(
            claim, market, policy.alias_rules_version, policy.m1_direction_guard
        )
    }
    if policy.composite_coverage is not None and claim.mechanism.is_composite:
        replacements["X2"] = _subject_only_composite_single_leg(
            claim, market, policy
        )
    outcomes = [
        replacements.get(outcome.check_id, outcome) for outcome in baseline.checks
    ]
    ceiling, hard_fails = compute_ceiling(outcomes, policy)
    return baseline.model_copy(
        update={
            "checks": outcomes,
            "hard_fail_count": hard_fails,
            "deterministic_ceiling": ceiling,
            "published": ceiling,
            "gate_policy_version": M1_SUBJECT_ONLY_POLICY_VERSION,
        }
    )


def aggregate_thesis(
    verdicts: list[tuple[MarketFitVerdict, int]],
    *,
    policy: FitPolicy | None = None,
) -> ThesisFit:
    """Aggregate one named policy identity and reject mixed pools."""
    expected = gate_policy_version(policy) if policy is not None else None
    identities = {verdict.gate_policy_version for verdict, _ in verdicts}
    if len(identities) > 1:
        raise ValueError("mixed fit policies cannot be aggregated")
    observed = next(iter(identities), None)
    if expected is not None and observed is not None and observed != expected:
        raise ValueError("verdict policy identity differs from aggregation policy")
    identity = expected or observed or FIT_GATE_POLICY_VERSION
    return _aggregate_thesis_v1(verdicts).model_copy(
        update={"gate_policy_version": identity}
    )
