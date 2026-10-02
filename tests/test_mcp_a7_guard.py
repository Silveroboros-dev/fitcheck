"""Field-aware A7 response guard (session-6 amendment 4)."""

import pytest

from el.mcp.contracts import V3MarketPoolResult
from el.mcp.v3_tools import McpV3Tools
from el.mcp.vocab_guard import A7Violation, assert_a7_clean
from el.product.api import MarketPoolUiOut, ProductApi
from el.retrieval.scope import CandidateSetRetrievalScope


def test_generated_advice_field_hard_fails():
    # System-generated copy that gives trading advice must hard-fail.
    with pytest.raises(A7Violation):
        assert_a7_clean({"fit_reason": "you should buy this market now"})
    with pytest.raises(A7Violation):
        assert_a7_clean({"what_it_misses": "honestly you should sell"})


def test_quoted_source_with_buy_sell_sale_passes():
    # A market title / captured rules quoting buy/sell/sale is SOURCE, exempt.
    payload = {
        "market_title": "Will Acme sell its cloud unit (asset sale) in 2026?",
        "resolution_rules": "Resolves YES if Acme announces a sale or sell-off.",
    }
    assert_a7_clean(
        payload, source_paths=frozenset({"market_title", "resolution_rules"})
    )  # no raise


def test_same_source_text_would_fail_if_not_exempt():
    # Demonstrates WHY the exemption is load-bearing: unexempted, the market
    # title trips on the whole word "sell".
    with pytest.raises(A7Violation):
        assert_a7_clean({"market_title": "Will Acme sell its unit?"})


def test_sale_alone_is_not_restricted():
    # "sale" is not a restricted token (only buy/sell + advice phrases are).
    assert_a7_clean({"summary": "the asset sale concluded in Q3"})  # no raise


def test_nested_list_paths_are_index_agnostic():
    payload = {
        "rejected_markets": [
            {"market_title": "Will X sell off?", "reason": "weak proxy on metric"},
            {"market_title": "Buy-side flows market", "reason": "horizon mismatch"},
        ]
    }
    # Titles exempt (source); our generated reasons are checked and clean.
    assert_a7_clean(
        payload, source_paths=frozenset({"rejected_markets[].market_title"})
    )
    # A generated reason giving advice fails even when titles are exempt.
    payload["rejected_markets"][0]["reason"] = "you should sell instead"
    with pytest.raises(A7Violation) as exc:
        assert_a7_clean(
            payload, source_paths=frozenset({"rejected_markets[].market_title"})
        )
    assert any("reason" in path for path, _ in exc.value.offenders)


@pytest.mark.parametrize(
    ("output_type", "guard"),
    [
        (MarketPoolUiOut, ProductApi._guard),
        (V3MarketPoolResult, McpV3Tools._guard),
    ],
    ids=["product-ui", "mcp"],
)
def test_market_pool_guard_checks_generated_summary_and_query_provenance(
    output_type, guard
):
    """Neither a normalized summary nor an observed query is quoted source."""

    summary = output_type.model_construct(accepted_thesis_summary="you should buy")
    with pytest.raises(A7Violation) as summary_error:
        guard(None, summary)
    assert summary_error.value.offenders[0][0] == "accepted_thesis_summary"

    scope = CandidateSetRetrievalScope(
        provider="fixture",
        scope_kind="bounded_query_observation",
        query_text="you should buy",
        snapshot_id="synthetic-snapshot",
        retrieval_id="synthetic-retrieval",
        returned_count=1,
        lifecycle_eligible_count=1,
        lifecycle_excluded_count=0,
        gate_eligible_count=1,
    )
    query = output_type.model_construct(
        accepted_thesis_summary="A neutral synthetic thesis",
        retrieval_scope=scope,
    )
    with pytest.raises(A7Violation) as query_error:
        guard(None, query)
    assert query_error.value.offenders[0][0] == "retrieval_scope.query_text"

    assert output_type.SOURCE_PATHS == frozenset(
        {
            "candidate_markets[].market_title",
            "candidate_markets[].resolution_conditions",
        }
    )
    assert_a7_clean(
        {
            "candidate_markets": [
                {
                    "market_title": "Will Acme sell its unit?",
                    "resolution_conditions": "The source says buy or sell.",
                }
            ]
        },
        source_paths=output_type.SOURCE_PATHS,
    )
