"""Loop 3 checks + policy units — loop3-v1 (blueprint §14)."""

from datetime import date

from el.domain.enums import FitClass
from el.domain.structures import ExtractedStructure, MarketStructure
from el.fitgate.checks import (
    CheckKind,
    CheckOutcome,
    CheckStatus,
    check_metric_lexical_floor,
    check_subject_lexical_floor,
    tok_v1,
)
from el.fitgate.policy import (
    FitPolicy,
    aggregate_thesis,
    compute_ceiling,
    evaluate_market,
    weaker_of,
)


def make_claim(**overrides) -> ExtractedStructure:
    base = dict(
        claim_summary="Nvidia reports a quarterly revenue decline by end of 2026.",
        entities=[{"name": "Nvidia", "role": "subject"}],
        event_stage="measured",
        metric={
            "what": "quarterly revenue decline",
            "measured_by": "earnings reports",
            "objective": True,
        },
        horizon={
            "window_start": None,
            "window_end": date(2026, 12, 31),
            "timezone": "UTC",
            "precision": "day",
        },
        mechanism={"asserted_causal_chain": None, "is_composite": False},
        stance="decrease",
        resolution_source_class="official",
        ambiguities=[],
        contractible_version="Will Nvidia report a quarterly revenue decline by Dec 31, 2026?",
    )
    base.update(overrides)
    return ExtractedStructure.model_validate(base)


def make_market(**overrides) -> MarketStructure:
    base = dict(
        market_id="mkt_test",
        snapshot_id="snap_test",
        event_stage="measured",
        metric={
            "what": "year-over-year decline in reported quarterly revenue",
            "measured_by": "company earnings reports",
            "objective": True,
        },
        horizon={"resolution_date": date(2026, 12, 31), "timezone": "UTC"},
        entities=[{"name": "Nvidia", "role": "subject"}],
        threshold=None,
        direction="decline",
        resolution_source_class="official",
        extraction_policy_version=1,
    )
    base.update(overrides)
    return MarketStructure.model_validate(base)


# --- tok-v1 ---------------------------------------------------------------


def test_tok_v1_normalization_rules():
    # casefold + non-alphanumeric split + $/#/% stripping via word runs
    assert tok_v1("MSFT closing price above $500") == {
        "msft", "closing", "price", "above", "500",
    }
    # plural-s stem only beyond 3 chars; identical stemming on both sides
    assert tok_v1("sales rides US") == {"sale", "ride", "us"}
    # stopwords and bare year tokens drop; single-char alpha drops ("S-1")
    assert tok_v1("the Form S-1 filed in 2026") == {"form", "1", "filed"}
    # "#1" keeps its digit (rank semantics)
    assert "1" in tok_v1("rank #1 on the leaderboard")
    assert tok_v1(None) == frozenset()
    assert tok_v1("") == frozenset()


# --- E1 subject lexical floor ----------------------------------------------


def test_e1_pass_on_token_overlap():
    outcome = check_subject_lexical_floor(make_claim(), make_market())
    assert outcome.status is CheckStatus.PASS
    assert outcome.cap is None and not outcome.hard


def test_e1_hard_fail_on_zero_overlap():
    market = make_market(
        entities=[{"name": "Microsoft", "role": "subject"},
                  {"name": "NASDAQ", "role": "venue"}]
    )
    outcome = check_subject_lexical_floor(make_claim(), market)
    assert outcome.status is CheckStatus.FAIL
    assert outcome.kind is CheckKind.HARD_CAP
    assert outcome.cap is FitClass.WEAK_PROXY and outcome.hard


def test_e1_unknown_without_subject_role_flags_never_guesses():
    claim = make_claim(entities=[{"name": "Nvidia", "role": "object"}])
    outcome = check_subject_lexical_floor(claim, make_market())
    assert outcome.status is CheckStatus.UNKNOWN
    assert outcome.kind is CheckKind.SOFT_CAP
    assert outcome.cap is FitClass.INDIRECT and not outcome.hard


# --- M1 metric lexical floor -------------------------------------------------


def test_m1_inconclusive_on_overlap_never_pass():
    outcome = check_metric_lexical_floor(make_claim(), make_market())
    # A floor: overlap proves nothing, so the status is INCONCLUSIVE,
    # never PASS — nothing downstream may read it as a blessing.
    assert outcome.status is CheckStatus.INCONCLUSIVE
    assert outcome.cap is None and not outcome.hard


def test_m1_hard_fail_on_zero_overlap():
    claim = make_claim(
        metric={
            "what": "sales contraction relative to twelve months prior",
            "measured_by": "earnings reports",
            "objective": True,
        }
    )
    outcome = check_metric_lexical_floor(claim, make_market())
    assert outcome.status is CheckStatus.FAIL
    assert outcome.cap is FitClass.WEAK_PROXY and outcome.hard


def test_m1_reads_market_threshold_too():
    market = make_market(
        metric={
            "what": "closing price level",
            "measured_by": "exchange",
            "objective": True,
        },
        threshold="quarterly revenue decline threshold",
    )
    outcome = check_metric_lexical_floor(make_claim(), market)
    assert outcome.status is CheckStatus.INCONCLUSIVE


# --- ceiling + stacking ------------------------------------------------------


def _outcome(check_id: str, cap: FitClass | None, hard: bool) -> CheckOutcome:
    return CheckOutcome(
        check_id=check_id,
        name=check_id,
        kind=(
            CheckKind.HARD_CAP if hard
            else CheckKind.SOFT_CAP if cap else CheckKind.NONE
        ),
        status=CheckStatus.FAIL if cap else CheckStatus.PASS,
        cap=cap,
        hard=hard,
        detail="synthetic",
    )


def test_ceiling_caps_are_commutative_min():
    policy = FitPolicy()
    outcomes = [
        _outcome("A", FitClass.INDIRECT, hard=False),
        _outcome("B", None, hard=False),
    ]
    ceiling, hard = compute_ceiling(outcomes, policy)
    assert ceiling is FitClass.INDIRECT and hard == 0
    assert compute_ceiling(list(reversed(outcomes)), policy)[0] is ceiling


def test_two_hard_fails_stack_to_no_clean():
    policy = FitPolicy()
    outcomes = [
        _outcome("A", FitClass.WEAK_PROXY, hard=True),
        _outcome("B", FitClass.WEAK_PROXY, hard=True),
    ]
    ceiling, hard = compute_ceiling(outcomes, policy)
    assert ceiling is FitClass.NO_CLEAN_EXPRESSION and hard == 2


def test_one_hard_fail_stays_weak():
    ceiling, hard = compute_ceiling(
        [_outcome("A", FitClass.WEAK_PROXY, hard=True)], FitPolicy()
    )
    assert ceiling is FitClass.WEAK_PROXY and hard == 1


def test_weaker_of_total_order():
    assert weaker_of(FitClass.DIRECT, FitClass.WEAK_PROXY) is FitClass.WEAK_PROXY
    assert (
        weaker_of(FitClass.NO_CLEAN_EXPRESSION, FitClass.INDIRECT)
        is FitClass.NO_CLEAN_EXPRESSION
    )


# --- aggregation -------------------------------------------------------------


def test_aggregate_recommends_only_direct_or_indirect():
    claim = make_claim()
    good = evaluate_market(claim, make_market(market_id="mkt_good"))
    twin = evaluate_market(
        claim,
        make_market(
            market_id="mkt_twin",
            entities=[{"name": "Microsoft", "role": "subject"}],
            metric={
                "what": "MSFT closing price above level",
                "measured_by": "NASDAQ",
                "objective": True,
            },
        ),
    )
    thesis = aggregate_thesis([(good, 0), (twin, 1)])
    assert thesis.fit_class is FitClass.DIRECT
    assert thesis.recommended_market_id == "mkt_good"
    assert [v.market_id for v in thesis.rejected] == ["mkt_twin"]
    assert not thesis.draft_contract_recommended


def test_aggregate_weak_means_no_recommendation_plus_draft():
    # A genuinely disjoint metric (NOT an alias-v1 synonym of the market's
    # revenue-decline metric) -> M1 hard-fails -> weak_proxy. The old
    # "sales contraction" string is now bridged to "revenue decline" by the
    # promoted alias table and would resolve to direct here.
    claim = make_claim(
        metric={
            "what": "monthly active user count",
            "measured_by": "product analytics",
            "objective": True,
        }
    )
    verdict = evaluate_market(claim, make_market())
    assert verdict.deterministic_ceiling is FitClass.WEAK_PROXY
    thesis = aggregate_thesis([(verdict, 0)])
    assert thesis.fit_class is FitClass.WEAK_PROXY
    assert thesis.recommended_market_id is None
    assert thesis.draft_contract_recommended
    # The weak market is still surfaced as a rejection with reasons.
    assert [v.market_id for v in thesis.rejected] == ["mkt_test"]


def test_aggregate_empty_pool_is_no_clean_with_draft():
    thesis = aggregate_thesis([])
    assert thesis.fit_class is FitClass.NO_CLEAN_EXPRESSION
    assert thesis.recommended_market_id is None
    assert thesis.draft_contract_recommended


def test_tie_break_by_loop2_rank_is_deterministic():
    claim = make_claim()
    first = evaluate_market(claim, make_market(market_id="mkt_rank0"))
    second = evaluate_market(claim, make_market(market_id="mkt_rank1"))
    thesis = aggregate_thesis([(second, 1), (first, 0)])
    assert thesis.recommended_market_id == "mkt_rank0"


# --- run-invariance ----------------------------------------------------------


def test_evaluate_market_run_invariance():
    claim, market = make_claim(), make_market()
    first = evaluate_market(claim, market)
    second = evaluate_market(claim, market)
    assert first.model_dump() == second.model_dump()


# --- S1 event stage (build commit 2) ------------------------------------------


def test_s1_pass_on_equal_stage():
    outcome = [
        o for o in evaluate_market(make_claim(), make_market()).checks
        if o.check_id == "S1"
    ][0]
    assert outcome.status is CheckStatus.PASS


def test_s1_hard_fail_on_any_mismatch_no_adjacency_tolerance():
    # announced != launched (the GPT-5 trap) AND adopted != measured
    # (law-passed vs effect-realized): both distance 1, both refuse.
    claim = make_claim(event_stage="launched")
    market = make_market(event_stage="announced")
    verdict = evaluate_market(claim, market)
    s1 = [o for o in verdict.checks if o.check_id == "S1"][0]
    assert s1.status is CheckStatus.FAIL
    assert s1.cap is FitClass.WEAK_PROXY and s1.hard
    assert verdict.deterministic_ceiling is FitClass.WEAK_PROXY


# --- H1 horizon tolerance (build commit 3) -----------------------------------


def test_h1_good_within_precision_tolerance_annotates_match():
    verdict = evaluate_market(make_claim(), make_market())
    h1 = [o for o in verdict.checks if o.check_id == "H1"][0]
    assert h1.status is CheckStatus.PASS
    assert h1.annotations["horizon_match"] == "good"
    assert verdict.horizon_match is not None
    assert verdict.horizon_match.value == "good"


def test_h1_fair_soft_caps_indirect():
    # day precision: good <= 7d, fair <= 14d
    market = make_market(
        horizon={"resolution_date": date(2027, 1, 10), "timezone": "UTC"}
    )
    verdict = evaluate_market(make_claim(), market)
    h1 = [o for o in verdict.checks if o.check_id == "H1"][0]
    assert h1.cap is FitClass.INDIRECT and not h1.hard
    assert verdict.deterministic_ceiling is FitClass.INDIRECT
    assert verdict.horizon_match.value == "fair"


def test_h1_poor_hard_fails_weak():
    market = make_market(
        horizon={"resolution_date": date(2027, 6, 30), "timezone": "UTC"}
    )
    verdict = evaluate_market(make_claim(), market)
    h1 = [o for o in verdict.checks if o.check_id == "H1"][0]
    assert h1.cap is FitClass.WEAK_PROXY and h1.hard
    assert verdict.horizon_match.value == "poor"


def test_h1_tolerance_scales_with_claim_precision():
    # The same 245d delta is poor at month precision but good at year
    # precision (a year-precision claim is fuzzy by construction).
    market = make_market(
        horizon={"resolution_date": date(2026, 12, 31), "timezone": "UTC"}
    )
    month_claim = make_claim(
        horizon={
            "window_start": None,
            "window_end": date(2026, 4, 30),
            "timezone": "UTC",
            "precision": "month",
        }
    )
    year_claim = make_claim(
        horizon={
            "window_start": None,
            "window_end": date(2026, 4, 30),
            "timezone": "UTC",
            "precision": "year",
        }
    )
    assert (
        evaluate_market(month_claim, market).horizon_match.value == "poor"
    )
    assert evaluate_market(year_claim, market).horizon_match.value == "good"


def test_h1_deadline_before_window_start_is_poor():
    claim = make_claim(
        horizon={
            "window_start": date(2027, 1, 1),
            "window_end": date(2027, 12, 31),
            "timezone": "UTC",
            "precision": "day",
        }
    )
    verdict = evaluate_market(claim, make_market())
    h1 = [o for o in verdict.checks if o.check_id == "H1"][0]
    assert h1.cap is FitClass.WEAK_PROXY and h1.hard
    assert "cannot observe" in h1.detail


# --- X1 mechanism endpoint (build commit 4) -----------------------------------


def test_x1_not_applicable_without_mechanism():
    verdict = evaluate_market(make_claim(), make_market())
    x1 = [o for o in verdict.checks if o.check_id == "X1"][0]
    assert x1.status is CheckStatus.NOT_APPLICABLE
    assert x1.cap is None


def test_x1_soft_caps_every_market_at_indirect():
    # eval_009's shape: "revenue declines BECAUSE of GPU oversupply" —
    # the endpoint market is strong evidence, never a distinguishing
    # test of the causal chain.
    claim = make_claim(
        mechanism={
            "asserted_causal_chain": "GPU oversupply causes revenue decline",
            "is_composite": False,
        }
    )
    verdict = evaluate_market(claim, make_market())
    x1 = [o for o in verdict.checks if o.check_id == "X1"][0]
    assert x1.cap is FitClass.INDIRECT and not x1.hard
    assert verdict.deterministic_ceiling is FitClass.INDIRECT
    assert verdict.hard_fail_count == 0


# --- M2/M3 objectivity (build commit 5) ----------------------------------------


def test_m2_fires_only_when_claim_demands_objectivity():
    subjective_market = make_market(
        metric={
            "what": "year-over-year decline in reported quarterly revenue",
            "measured_by": "panel judgment",
            "objective": False,
        }
    )
    # official-source objective claim DEMANDS objectivity -> hard fail
    verdict = evaluate_market(make_claim(), subjective_market)
    m2 = [o for o in verdict.checks if o.check_id == "M2"][0]
    assert m2.cap is FitClass.WEAK_PROXY and m2.hard
    # the same claim sourced from press tolerates it (eval_005 lesson):
    # no fit refusal, but R1 elevates the risk
    press_claim = make_claim(resolution_source_class="press")
    verdict = evaluate_market(press_claim, subjective_market)
    m2 = [o for o in verdict.checks if o.check_id == "M2"][0]
    assert m2.status is CheckStatus.PASS
    assert verdict.resolution_risk is not None
    assert verdict.resolution_risk.value == "medium"


def test_m3_subjective_claim_objective_market_soft_caps():
    claim = make_claim(
        metric={
            "what": "enterprise traction decline",
            "measured_by": "unspecified",
            "objective": False,
        },
        resolution_source_class="none",
    )
    verdict = evaluate_market(claim, make_market())
    m3 = [o for o in verdict.checks if o.check_id == "M3"][0]
    assert m3.cap is FitClass.INDIRECT and not m3.hard


# --- R1 resolution risk (build commit 5) ----------------------------------------


def test_r1_risk_table():
    # official + objective -> low
    assert evaluate_market(make_claim(), make_market()).resolution_risk.value == "low"
    # official + subjective (eval_005's market shape) -> medium
    medium = make_market(
        metric={"what": "redesigned product shipped", "measured_by": "review",
                "objective": False},
    )
    claim = make_claim(resolution_source_class="press",
                       metric={"what": "redesigned product shipped",
                               "measured_by": "review", "objective": True})
    assert evaluate_market(claim, medium).resolution_risk.value == "medium"
    # subjective + press -> high
    high = make_market(
        metric={"what": "redesigned product shipped", "measured_by": "review",
                "objective": False},
        resolution_source_class="press",
    )
    assert evaluate_market(claim, high).resolution_risk.value == "high"
    # R1 never caps
    r1 = [o for o in evaluate_market(claim, high).checks if o.check_id == "R1"][0]
    assert r1.cap is None and r1.kind is CheckKind.ANNOTATOR


# --- P1 outcome polarity (build commit 5) ----------------------------------------


def test_p1_yes_stance_maps_to_yes_side():
    verdict = evaluate_market(make_claim(stance="yes"), make_market())
    assert verdict.thesis_side == "yes"


def test_p1_no_stance_maps_to_no_side_still_direct():
    # External-review case 1: "X will NOT happen" vs "Will X happen?" is
    # a DIRECT expression on the NO outcome — side recorded, no cap.
    verdict = evaluate_market(make_claim(stance="no"), make_market())
    assert verdict.thesis_side == "no"
    assert verdict.deterministic_ceiling is FitClass.DIRECT


def test_p1_directional_stance_against_market_direction():
    # decrease vs "decline" market -> aligned, YES side (eval_009 shape)
    assert (
        evaluate_market(make_claim(stance="decrease"), make_market()).thesis_side
        == "yes"
    )
    # decrease vs "above" market -> opposed, NO side (eval_003 x msft)
    above = make_market(direction="above")
    assert (
        evaluate_market(make_claim(stance="decrease"), above).thesis_side == "no"
    )


def test_p1_unresolved_direction_flags_and_soft_caps():
    no_direction = make_market(direction=None)
    verdict = evaluate_market(make_claim(stance="decrease"), no_direction)
    assert verdict.thesis_side == "unknown"
    p1 = [o for o in verdict.checks if o.check_id == "P1"][0]
    assert p1.cap is FitClass.INDIRECT and not p1.hard
    assert "outcome_side_unresolved" in p1.detail
    assert verdict.deterministic_ceiling is FitClass.INDIRECT


def test_p1_unclear_stance_flags_without_cap():
    verdict = evaluate_market(make_claim(stance="unclear"), make_market())
    assert verdict.thesis_side == "unknown"
    p1 = [o for o in verdict.checks if o.check_id == "P1"][0]
    assert p1.cap is None and p1.kind is CheckKind.ANNOTATOR


# --- X2 composite (build commit 6) ---------------------------------------------


def test_x2_not_applicable_for_simple_claims():
    verdict = evaluate_market(make_claim(), make_market())
    x2 = [o for o in verdict.checks if o.check_id == "X2"][0]
    assert x2.status is CheckStatus.NOT_APPLICABLE


def test_x2_soft_caps_all_markets_for_composite_claims():
    claim = make_claim(
        mechanism={"asserted_causal_chain": None, "is_composite": True}
    )
    verdict = evaluate_market(claim, make_market())
    x2 = [o for o in verdict.checks if o.check_id == "X2"][0]
    assert x2.cap is FitClass.INDIRECT and not x2.hard
    assert verdict.deterministic_ceiling is FitClass.INDIRECT
    assert verdict.hard_fail_count == 0
