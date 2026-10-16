"""Public identity invariant for advisory merge (FC-Q01)."""

import pytest

from el.fitgate.gate import merge_advisory
from el.fitgate.policy import evaluate_market
from test_fitgate_checks import make_claim, make_market


def test_deterministic_market_identity_mismatch_fails_closed():
    deterministic = evaluate_market(make_claim(), make_market()).model_copy(
        update={"market_id": "some_other_market"}
    )

    with pytest.raises(ValueError, match="deterministic verdict is for"):
        merge_advisory(deterministic, None, market_id="mkt_test")
