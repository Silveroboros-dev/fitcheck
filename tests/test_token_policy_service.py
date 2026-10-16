"""FC-R02: a selected token successor reaches service quotes and provenance."""

import hashlib
from types import SimpleNamespace

from el.domain.contracts import FitCardProvenanceOut
from el.domain.tables import FitCard
from el.fitgate.m1_subject_only import (
    TOKEN_V2_FIT_POLICY_VERSION,
    stopword_corrected_fit_policy,
)
from el.fitgate.service import FitService
from el.fitgate.token_rules import TOKEN_RULES_SUCCESSOR_VERSION
from el.models.fit_adapter import FixtureFitProposer
from test_fit_service import GOLDENS, _advisory, _classify, _lmsys_claim


def test_token_successor_reaches_service_provenance_and_public_contract():
    outcome, sessions = _classify(
        _lmsys_claim(), policy=stopword_corrected_fit_policy()
    )
    assert outcome.gate_policy_version == TOKEN_V2_FIT_POLICY_VERSION
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        provenance = FitCardProvenanceOut.model_validate(card.provenance)
        assert provenance.gate_policy_version == TOKEN_V2_FIT_POLICY_VERSION
        assert all(
            item["token_rules_version"] == TOKEN_RULES_SUCCESSOR_VERSION
            for item in provenance.per_market.values()
        )


def test_service_uses_selected_quote_token_floor_and_preserves_legacy():
    market = GOLDENS["mkt_gemini_lmsys_1"]
    claim = _lmsys_claim()
    source = claim.claim_summary + " this always"
    advisory = _advisory(
        market.market_id,
        claim_evidence="this always",
        market_evidence="This market resolves to YES",
    )
    contract_terms = ""
    resolution_rules = "This market resolves to YES"
    capture = SimpleNamespace(
        contract_terms_text=contract_terms,
        resolution_rules_text=resolution_rules,
        contract_terms_hash=hashlib.sha256(contract_terms.encode()).hexdigest(),
        resolution_rules_hash=hashlib.sha256(
            resolution_rules.encode()
        ).hexdigest(),
    )
    for policy, expected in ((None, "accepted"), (stopword_corrected_fit_policy(), "fallback")):
        service = FitService(
            None, None, policy=policy,
            advisory=FixtureFitProposer({market.market_id: advisory}),
        )
        service._rules_capture = lambda *_, **__: capture
        judgment = service._judge_market(
            None, claim, source, market, market.snapshot_id, 0
        )
        assert judgment.advisory_meta["status"] == expected
        if expected == "fallback":
            assert judgment.advisory_meta["calls"] == 2
            assert any("too thin" in reason for reason in judgment.advisory_meta["rejected_reasons"])
