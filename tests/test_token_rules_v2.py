"""FC-R02: an explicitly selected successor must not count ``this`` as content."""

from datetime import date
from dataclasses import asdict

import pytest

from el.domain.enums import FitClass
from el.domain.structures import ExtractedStructure, MarketStructure
from el.fitgate.checks import TOKEN_RULES_VERSION, CheckStatus, tok_v1
from el.fitgate.gate import quote_span_violations
from el.fitgate.m1_subject_only import (
    TOKEN_V2_FIT_POLICY_VERSION,
    TokenV2FitPolicy,
    aggregate_thesis,
    evaluate_market,
    fit_policy_from_pins,
    gate_policy_version,
    ordinary_discovery_fit_policy,
    stopword_corrected_fit_policy,
)
from el.fitgate.policy import FitPolicy
from el.fitgate.token_rules import (
    TOKEN_RULES_SUCCESSOR_VERSION,
    content_tokens,
    tok_v2,
    token_rules_version,
)
from el.models.fit_adapter import ConditionVerdict, FitAdvisory


def _quote_advisory(quote: str) -> FitAdvisory:
    return FitAdvisory(
        market_id="market",
        condition_verdicts=[
            ConditionVerdict(
                condition="same_metric",
                status="pass",
                claim_evidence=quote,
                market_evidence=quote,
            )
        ],
        bridge_assumptions=[],
        falsifier="the stated condition fails",
        suggested_class=FitClass.DIRECT,
        what_it_captures="the stated condition",
        what_it_misses="other conditions",
        confidence=0.9,
    )


def test_fc_r02_quote_requires_explicit_successor_and_preserves_replay():
    quote = "this always"
    advisory = _quote_advisory(quote)
    assert tok_v1("this") == {"thi"}
    assert quote_span_violations(
        advisory, claim_text=quote, rules_text=quote
    ) == []
    violations = quote_span_violations(
        advisory,
        claim_text=quote,
        rules_text=quote,
        token_rules_version=TOKEN_RULES_SUCCESSOR_VERSION,
    )
    assert len(violations) == 2
    assert all("is too thin" in violation for violation in violations)


# Development probes only; no adjudicated labels or quote thresholds change.
TOKEN_PROBES = [
    (None, set(), set()),
    ("this", {"thi"}, set()),
    ("THIS always", {"thi", "alway"}, {"alway"}),
    ("this quarterly revenue", {"thi", "quarterly", "revenue"}, {"quarterly", "revenue"}),
    ("sales rides US", {"sale", "ride", "us"}, {"sale", "ride", "us"}),
    ("thats thesis analysis", {"thesi", "analysi"}, {"thesi", "analysi"}),
    ("1899 1900 1999 2026 2099 2100", {"1899", "2100"}, {"1899", "2100"}),
    ("the Form S-1 filed in 2026", {"form", "1", "filed"}, {"form", "1", "filed"}),
    ("this thi", {"thi"}, {"thi"}),
]


@pytest.mark.parametrize(("text", "before", "after"), TOKEN_PROBES)
def test_development_token_delta_preserves_year_and_plural_rules(text, before, after):
    assert tok_v1(text) == before
    assert tok_v2(text) == after


@pytest.mark.parametrize(
    ("quote", "before_accepts", "after_accepts"),
    [
        ("this always", True, False),
        ("this revenue", True, False),
        ("this quarterly revenue", True, True),
        ("quarterly revenue decline", True, True),
        ("sales rides US", True, True),
        ("revenue 2026", False, False),
        ("revenue 2100", True, True),
        ("thats thesis analysis", True, True),
    ],
)
def test_development_quote_delta(quote, before_accepts, after_accepts):
    advisory = _quote_advisory(quote)
    for version, accepts in (
        (TOKEN_RULES_VERSION, before_accepts),
        (TOKEN_RULES_SUCCESSOR_VERSION, after_accepts),
    ):
        assert bool(quote_span_violations(
            advisory, claim_text=quote, rules_text=quote, token_rules_version=version
        )) is not accepts


def _pair(claim_metric, market_metric, subject="Acme", entity="Acme"):
    claim = ExtractedStructure(
        claim_summary=f"{subject} quarterly revenue declines by 2026",
        contractible_version=f"{subject} quarterly revenue declines by 2026",
        entities=[{"name": subject, "role": "subject"}],
        event_stage="measured",
        metric={"what": claim_metric, "measured_by": "filings", "objective": True},
        horizon={"window_end": date(2026, 12, 31), "precision": "year"},
        mechanism={},
        stance="decrease",
        resolution_source_class="filing",
    )
    market = MarketStructure(
        market_id="market",
        snapshot_id="snapshot",
        entities=[{"name": entity, "role": "subject"}],
        event_stage="measured",
        metric={"what": market_metric, "measured_by": "filings", "objective": True},
        horizon={"resolution_date": date(2026, 12, 31)},
        direction="decline",
        resolution_source_class="filing",
    )
    return claim, market


@pytest.mark.parametrize(
    ("claim_metric", "market_metric", "subject", "entity", "changed_checks", "after_class"),
    [
        ("this revenue", "this output", "Acme", "Acme", {"M1"}, FitClass.WEAK_PROXY),
        ("revenue", "revenue", "This Orion", "This Vega", {"E1"}, FitClass.WEAK_PROXY),
        ("this revenue", "this output", "This Orion", "This Vega", {"E1"}, FitClass.NO_CLEAN_EXPRESSION),
        ("this", "this", "Acme", "Acme", {"M1"}, FitClass.DIRECT),
        ("sales rides 2026", "sales rides 2026", "Acme", "Acme", set(), FitClass.DIRECT),
        ("quarterly revenue", "quarterly revenue", "Acme", "Acme", set(), FitClass.DIRECT),
    ],
)
def test_development_check_delta(
    claim_metric, market_metric, subject, entity, changed_checks, after_class
):
    pair = _pair(claim_metric, market_metric, subject, entity)
    before = evaluate_market(*pair, ordinary_discovery_fit_policy())
    after = evaluate_market(*pair, stopword_corrected_fit_policy())
    before_checks = {check.check_id: check.status for check in before.checks}
    after_checks = {check.check_id: check.status for check in after.checks}
    assert {key for key in before_checks if before_checks[key] != after_checks[key]} == changed_checks
    expected_before = FitClass.WEAK_PROXY if after_class is FitClass.NO_CLEAN_EXPRESSION else FitClass.DIRECT
    assert before.published is expected_before
    assert after.published is after_class
    assert after.gate_policy_version == TOKEN_V2_FIT_POLICY_VERSION
    assert after.token_rules_version == TOKEN_RULES_SUCCESSOR_VERSION
    if claim_metric == "this":
        assert after_checks["M1"] is CheckStatus.UNKNOWN


def _pins(policy):
    return dict(
        gate_policy_version=gate_policy_version(policy),
        token_rules_version=token_rules_version(policy),
        stacking_threshold=policy.stacking_threshold,
        escalation_confidence_floor=policy.escalation_confidence_floor,
        horizon_tolerances=dict(policy.horizon_tolerances),
        alias_rules_version=policy.alias_rules_version,
        m1_direction_guard=policy.m1_direction_guard,
    )


def test_successor_policy_pins_replay_without_changing_historical_policy_shape():
    assert "token_rules_version" not in asdict(FitPolicy())
    pair = _pair("this revenue", "this output")
    for policy in (FitPolicy(), ordinary_discovery_fit_policy(), stopword_corrected_fit_policy()):
        restored = fit_policy_from_pins(**_pins(policy))
        assert evaluate_market(*pair, restored) == evaluate_market(*pair, policy)
    assert token_rules_version(FitPolicy()) == TOKEN_RULES_VERSION
    assert token_rules_version(ordinary_discovery_fit_policy()) == TOKEN_RULES_VERSION
    assert token_rules_version(stopword_corrected_fit_policy()) == TOKEN_RULES_SUCCESSOR_VERSION


def test_mixed_or_forged_token_policy_pins_and_aggregation_fail_closed():
    pair = _pair("this revenue", "this output")
    before_policy = ordinary_discovery_fit_policy()
    after_policy = stopword_corrected_fit_policy()
    before = evaluate_market(*pair, before_policy)
    after = evaluate_market(*pair, after_policy)
    with pytest.raises(ValueError, match="mixed fit policies"):
        aggregate_thesis([(before, 0), (after, 1)])
    forged = after.model_copy(update={"token_rules_version": TOKEN_RULES_VERSION})
    with pytest.raises(ValueError, match="mixed token rules"):
        aggregate_thesis([(after, 0), (forged, 1)])
    with pytest.raises(ValueError, match="verdict token rules differ"):
        aggregate_thesis([(forged, 0)])
    for policy, wrong_version in (
        (before_policy, TOKEN_RULES_SUCCESSOR_VERSION),
        (after_policy, TOKEN_RULES_VERSION),
    ):
        pins = _pins(policy)
        pins["token_rules_version"] = wrong_version
        with pytest.raises(ValueError, match="token rules differ"):
            fit_policy_from_pins(**pins)
    assert aggregate_thesis([], policy=after_policy).gate_policy_version == TOKEN_V2_FIT_POLICY_VERSION
    with pytest.raises(ValueError, match="unsupported token rules"):
        content_tokens("this", "unregistered")


def test_composite_sidecar_leg_matching_uses_successor_tokens():
    from el.fitgate.composite_v2 import CompositeCoveragePolicy, evaluate_composite_coverage

    claim, market = _pair("this revenue", "this output")
    claim = claim.model_copy(update={"mechanism": claim.mechanism.model_copy(update={"is_composite": True})})
    coverage = CompositeCoveragePolicy.model_validate({
        "claims": {
            claim.claim_summary: {
                "claim_summary": claim.claim_summary,
                "operator": "all",
                "legs": [{
                    "leg_id": "claim-leg",
                    "entities": claim.entities,
                    "event_stage": claim.event_stage,
                    "metric": claim.metric,
                    "horizon": claim.horizon,
                    "direction": "decline",
                    "stance": claim.stance,
                    "resolution_source_class": claim.resolution_source_class,
                }],
            }
        },
        "markets": {
            market.market_id: {
                "market_id": market.market_id,
                "operator": "all",
                "legs": [{
                    "leg_id": "market-leg",
                    "entities": market.entities,
                    "event_stage": market.event_stage,
                    "metric": market.metric,
                    "horizon": market.horizon,
                    "direction": "decline",
                    "resolution_source_class": market.resolution_source_class,
                }],
            }
        },
    })
    assert evaluate_composite_coverage(
        claim, market, coverage, m1_subject_only_residual_guard=True
    ).status == "full_cover"
    assert evaluate_composite_coverage(
        claim, market, coverage, m1_subject_only_residual_guard=True,
        token_rules_version=TOKEN_RULES_SUCCESSOR_VERSION
    ).status == "not_full_cover"
    successor = evaluate_market(claim, market, TokenV2FitPolicy(composite_coverage=coverage))
    x2 = next(check for check in successor.checks if check.check_id == "X2")
    assert x2.status is CheckStatus.FAIL
