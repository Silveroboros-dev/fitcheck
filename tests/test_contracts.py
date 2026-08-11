"""Domain contract rules — the binding decisions, as executable checks."""

import uuid
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from el.domain.contracts import ConvictionEventIn, FitCardOut
from el.domain.enums import ClientType, FitClass, PriorType
from el.domain.structures import ExtractedStructure, MarketStructure

CLAIM_STRUCTURE = {
    "schema_version": 1,
    "claim_summary": "OpenAI loses enterprise traction by end of 2026",
    "entities": [{"name": "OpenAI", "role": "subject"}],
    "event_stage": "measured",
    "metric": {
        "what": "enterprise revenue share",
        "measured_by": "company filings",
        "objective": True,
    },
    "horizon": {
        "window_end": "2026-12-31",
        "timezone": "UTC",
        "precision": "year",
    },
    "mechanism": {
        "asserted_causal_chain": "Gemini bundling erodes enterprise deals",
        "is_composite": False,
    },
    "stance": "decrease",
    "resolution_source_class": "filing",
    "ambiguities": ["'traction' needs operationalization"],
    "contractible_version": (
        "OpenAI enterprise revenue share declines YoY by 2026-12-31"
    ),
}

MARKET_STRUCTURE = {
    "schema_version": 1,
    "market_id": "mkt_123",
    "snapshot_id": "snap_001",
    "event_stage": "measured",
    "metric": {
        "what": "monthly active users",
        "measured_by": "third-party tracker",
        "objective": True,
    },
    "horizon": {"resolution_date": "2026-12-31", "timezone": "UTC"},
    "entities": [{"name": "ChatGPT", "role": "subject"}],
    "threshold": "decline vs 2025",
    "direction": "decrease",
    "resolution_source_class": "press",
    "extraction_policy_version": 1,
}


def test_claim_structure_validates():
    s = ExtractedStructure.model_validate(CLAIM_STRUCTURE)
    assert s.schema_version == 1
    assert s.horizon.window_end == date(2026, 12, 31)


def test_market_structure_validates():
    m = MarketStructure.model_validate(MARKET_STRUCTURE)
    assert m.market_id == "mkt_123"


def test_structure_rejects_unknown_fields():
    bad = dict(CLAIM_STRUCTURE, surprise="field")
    with pytest.raises(ValidationError):
        ExtractedStructure.model_validate(bad)


def test_structure_rejects_wrong_schema_version():
    with pytest.raises(ValidationError):
        ExtractedStructure.model_validate(dict(CLAIM_STRUCTURE, schema_version=2))


def _conviction(**overrides):
    base = dict(
        thesis_analysis_id=uuid.uuid4(),
        prior_type=PriorType.BLIND,
        market_context_seen=False,
        prior_probability=0.6,
        client_type=ClientType.AGENT_MCP,
    )
    base.update(overrides)
    return ConvictionEventIn.model_validate(base)


def test_blind_prior_valid_before_save():
    ev = _conviction()
    assert ev.ledger_entry_id is None  # exists before any ledger entry


def test_blind_prior_can_record_odds_free_fit_context():
    event = _conviction(market_context_seen=True)

    assert event.prior_type is PriorType.BLIND
    assert event.market_context_seen is True
    assert event.odds_revealed_at is None


def test_blind_prior_rejects_revealed_odds():
    with pytest.raises(ValidationError, match="odds_revealed_at"):
        _conviction(odds_revealed_at=datetime.now(timezone.utc))


def test_blind_prior_requires_probability():
    with pytest.raises(ValidationError, match="prior_probability"):
        _conviction(prior_probability=None)


def test_event_must_carry_prior_or_conviction():
    with pytest.raises(ValidationError, match="prior, a conviction"):
        _conviction(
            prior_type=PriorType.CONTEXT,
            market_context_seen=True,
            prior_probability=None,
            conviction_level=None,
        )


def _provenance():
    # The fit-card provenance as the service persists it: base run/policy
    # fields PLUS the Loop 3 authority/escalation fields (FitCardProvenanceOut).
    return {
        "gate_policy_version": "gate-v0.1",
        "extraction_schema_version": 1,
        "market_structure_schema_version": 1,
        "model_adapter": "test",
        "model_run_id": "run-1",
        "trace_id": "trace-1",
        "eval_pack_version": "seed-0",
        "judged_at": datetime.now(timezone.utc),
        "authority": "deterministic_only",
        "confidence_source": "deterministic_fallback_uncalibrated",
        "thesis_side": None,
        "escalation": {"eligible": True, "reasons": ["advisory_absent"]},
        "per_market": {},
    }


def test_no_clean_cannot_recommend_market():
    with pytest.raises(ValidationError, match="no_clean_expression"):
        FitCardOut(
            id=uuid.uuid4(),
            thesis_analysis_id=uuid.uuid4(),
            candidate_set_id=uuid.uuid4(),
            semantic_fit_class=FitClass.NO_CLEAN_EXPRESSION,
            recommended_market_id="mkt_123",
            what_it_captures="nothing",
            what_it_misses="everything",
            horizon_match="poor",
            resolution_risk="high",
            rejected_markets=[],
            fit_confidence=0.4,
            provenance=_provenance(),
        )
