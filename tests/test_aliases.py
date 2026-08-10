"""Learning Loop v0 — governed metric alias equivalence (alias-v2).

alias-v2 closed the transaction-sale leak the external reviewer found in
alias-v1 (2026-06-13): a denylist over STEMMED tokens let "stake sale
contraction" and neighbors clear M1 against "revenue decline" and manufacture
false-clean DIRECT expressions. v2 is UNSTEMMED and structural (revenue term +
change term + no transaction disqualifier). alias-v1 is retained, frozen, and
still tested as historical so its GO artifact re-derives.
"""

from datetime import date

import pytest

from el.domain.enums import FitClass
from el.domain.structures import (
    ClaimHorizon,
    Entity,
    ExtractedStructure,
    MarketHorizon,
    MarketStructure,
    Mechanism,
    Metric,
)
from el.fitgate.aliases import (
    ALIAS_RULES_EMPTY,
    ALIAS_RULES_V1,
    ALIAS_RULES_VERSION,
    metrics_alias_equivalent,
)
from el.fitgate.checks import CheckStatus, check_metric_lexical_floor
from el.fitgate.policy import evaluate_market

RT003_CLAIM = "sales contraction relative to twelve months prior"
RT003_MARKET = "year-over-year decline in reported quarterly revenue"
REVENUE_DECLINE = "revenue decline"

# The reviewer's minimum acceptance set (2026-06-13) against a revenue-decline
# market. True = must clear the floor (same class); False = must NOT clear.
ACCEPTANCE = [
    ("sales contraction", True),  # rt_003 sense — the promotion to preserve
    ("stake sale contraction", False),
    ("stake sales contraction", False),
    ("divestiture sale contraction", False),
    ("acquisition sale contraction", False),
    ("merger sale contraction", False),
    ("buyout sale contraction", False),
    ("sale volume contraction", False),
    ("sales volume contraction", False),
    ("unit sales contraction", False),
    ("sale price contraction", False),
    ("championship trophies won", False),
]


@pytest.mark.parametrize("metric,clears", ACCEPTANCE)
def test_alias_v2_acceptance(metric, clears):
    assert (
        metrics_alias_equivalent(metric, REVENUE_DECLINE, ALIAS_RULES_VERSION)
        is clears
    )


def test_default_pointer_is_alias_v2():
    assert ALIAS_RULES_VERSION == "alias-v2"


def test_empty_version_never_equates():
    assert not metrics_alias_equivalent(
        RT003_CLAIM, RT003_MARKET, ALIAS_RULES_EMPTY
    )
    assert not metrics_alias_equivalent(RT003_CLAIM, RT003_MARKET)  # default arg


def test_rt003_pair_clears_under_v2():
    assert metrics_alias_equivalent(RT003_CLAIM, RT003_MARKET, ALIAS_RULES_VERSION)


def test_equivalence_is_symmetric_under_v2():
    assert metrics_alias_equivalent(
        RT003_CLAIM, RT003_MARKET, ALIAS_RULES_VERSION
    ) == metrics_alias_equivalent(
        RT003_MARKET, RT003_CLAIM, ALIAS_RULES_VERSION
    )


def test_unrelated_metric_does_not_equate_under_v2():
    assert not metrics_alias_equivalent(
        "monthly active user count", RT003_MARKET, ALIAS_RULES_VERSION
    )


# --- alias-v1 retained as historical (frozen, reproducible) ----------------
def test_v1_historical_still_clears_rt003():
    assert metrics_alias_equivalent(RT003_CLAIM, RT003_MARKET, ALIAS_RULES_V1)


def test_v2_closes_a_leak_v1_had():
    # The fix delta, documented as an executable check: a transaction-sale
    # phrase cleared under the leaky v1 table, is correctly excluded under v2.
    leaky = "stake sale contraction"
    assert metrics_alias_equivalent(leaky, REVENUE_DECLINE, ALIAS_RULES_V1)
    assert not metrics_alias_equivalent(leaky, REVENUE_DECLINE, ALIAS_RULES_VERSION)


# --- floor + end-to-end (the leak is a false-clean ceiling, not just M1) ----
def _claim(metric_what: str) -> ExtractedStructure:
    return ExtractedStructure(
        claim_summary="Nvidia metric claim 2026.",
        entities=[Entity(name="Nvidia", role="subject")],
        event_stage="measured",
        metric=Metric(
            what=metric_what, measured_by="earnings reports", objective=True
        ),
        horizon=ClaimHorizon(window_end=date(2026, 12, 31), precision="day"),
        mechanism=Mechanism(),
        stance="decrease",
        resolution_source_class="official",
        contractible_version="Will Nvidia report this by Dec 31, 2026?",
    )


def _market(direction: str | None = None) -> MarketStructure:
    return MarketStructure(
        market_id="mkt_nvidia_revenue_drop",
        snapshot_id="snap",
        event_stage="measured",
        metric=Metric(
            what=RT003_MARKET, measured_by="earnings reports", objective=True
        ),
        horizon=MarketHorizon(resolution_date=date(2026, 12, 31)),
        entities=[Entity(name="Nvidia", role="subject")],
        direction=direction,
        resolution_source_class="official",
        extraction_policy_version=2,
    )


def test_m1_floor_under_v2_clears_legit_fails_transaction():
    market = _market()
    legit = check_metric_lexical_floor(
        _claim(RT003_CLAIM), market, ALIAS_RULES_VERSION
    )
    assert legit.status is CheckStatus.INCONCLUSIVE  # cleared, not PASS
    txn = check_metric_lexical_floor(
        _claim("stake sale contraction"), market, ALIAS_RULES_VERSION
    )
    assert txn.status is CheckStatus.FAIL and txn.hard


def test_end_to_end_default_policy_demotes_transaction_sale():
    # Everything else aligned (stage, subject, horizon, objectivity, direction)
    # so the metric is the only differentiator — the reviewer's "-> direct".
    market = _market(direction="decline")
    assert evaluate_market(_claim(RT003_CLAIM), market).published is FitClass.DIRECT
    assert (
        evaluate_market(_claim("stake sale contraction"), market).published
        is FitClass.WEAK_PROXY
    )
