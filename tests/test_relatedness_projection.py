"""Relatedness evidence can support a proxy, never establish a clean fit."""
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from el.domain.tables import MarketAssessment
from el.marketpool.service import (
    MarketPoolConflict, RelatedPairProposal, relatedness_projection,
    relatedness_evidence, project_pool_outcome,
)


@pytest.fixture
def pair():
    wording = "An autonomous research system is publicly demonstrated by 2028."
    rules = "Yes if a lab announces autonomous research by 2026. Demonstration is not required."
    thesis = SimpleNamespace(
        id=uuid4(), extracted_structure={"contractible_version": wording},
        normalized_claim_summary=wording,
    )
    capture = SimpleNamespace(
        id=uuid4(), market_id="synthetic-research-announcement", snapshot_id="frozen-1",
        resolution_rules_text=rules,
    )
    payload = {
        "authority": "deterministic_only", "ceiling": "no_clean_expression",
        "published": "no_clean_expression", "hard_fails": 4,
        "advisory": {"status": "not_configured", "calls": 0},
        "checks": [{"check_id": key, "cap": "weak_proxy", "hard": True}
                   for key in ("S1", "E1", "M1", "H1")],
    }
    assessment = MarketAssessment(
        thesis_analysis_id=thesis.id, rules_capture_id=capture.id,
        market_id=capture.market_id, snapshot_id=capture.snapshot_id,
        pair_class="not_an_expression", authority="deterministic_only",
        what_it_captures="Announcement of research.", what_it_misses="No public demonstration.",
        provenance={"per_market": payload},
    )
    proposal = RelatedPairProposal(
        proposal_id=uuid4(), proposer_id="test-agent:semantic-proposal",
        thesis_analysis_id=thesis.id,
        accepted_wording_sha256=sha256(wording.encode()).hexdigest(),
        market_id=capture.market_id, snapshot_id=capture.snapshot_id,
        rules_capture_id=capture.id, rules_sha256=sha256(rules.encode()).hexdigest(),
        claim_quote="autonomous research", rules_quote="announces autonomous research",
        rationale="Both concern autonomous research; the announcement and date differ.",
    )
    return assessment, thesis, capture, proposal


def test_related_pair_preserves_diagnostics_and_no_clean_pool(pair):
    row, thesis, capture, proposal = pair
    original = deepcopy(row.provenance)
    result = relatedness_projection([proposal])(row, thesis, capture)
    assert result.pair_class == "weak_proxy"
    assert result.authority == "source_bound_relation_proposal"
    assert result.fit_confidence is None
    assert result.provenance["per_market"] == original["per_market"]
    assert result.what_it_misses == "No public demonstration."
    assert relatedness_evidence(result, thesis, capture)["proposal"] == proposal.model_dump(mode="json")
    assert project_pool_outcome(
        ["weak_proxy", "not_an_expression", "not_an_expression"], target_count=3
    ) == (True, "no_clean_expression", [])


def test_without_evidence_or_for_other_market_no_implicit_relatedness(pair):
    row, thesis, capture, proposal = pair
    assert relatedness_projection([])(row, thesis, capture).pair_class == "not_an_expression"
    other = proposal.model_copy(update={"market_id": "unrelated-sports-contract"})
    assert relatedness_projection([other])(row, thesis, capture).pair_class == "not_an_expression"
    assert relatedness_evidence(row, thesis, capture) is None


@pytest.mark.parametrize("change", [
    {"snapshot_id": "newer-snapshot"},
    {"rules_capture_id": uuid4()},
    {"accepted_wording_sha256": "a" * 64},
    {"rules_sha256": "b" * 64},
    {"claim_quote": "invented claim"},
    {"rules_quote": "invented rules"},
    {"rules_quote": " "},
    {"proposer_id": " "},
])
def test_stale_or_invented_evidence_fails_closed(pair, change):
    row, thesis, capture, proposal = pair
    with pytest.raises(MarketPoolConflict, match="not bound"):
        relatedness_projection([proposal.model_copy(update=change)])(row, thesis, capture)
    assert row.pair_class == "not_an_expression"


def test_duplicate_targets_rejected(pair):
    proposal = pair[3]
    with pytest.raises(MarketPoolConflict, match="duplicate"):
        relatedness_projection([proposal, proposal])


@pytest.mark.parametrize("failure", ["advisory", "explicit-cap", "bad-count", "authority"])
def test_cannot_override_independent_rejection(pair, failure):
    row, thesis, capture, proposal = pair
    payload = row.provenance["per_market"]
    if failure == "advisory":
        payload["advisory"] = {"status": "accepted"}
    elif failure == "explicit-cap":
        payload["checks"][0]["cap"] = "no_clean_expression"
    elif failure == "bad-count":
        payload["hard_fails"] = 0
    else:
        row.authority = "deterministic_with_advisory_veto"
    assert relatedness_projection([proposal])(row, thesis, capture).pair_class == "not_an_expression"


@pytest.mark.parametrize("pair_class", ["direct", "indirect", "weak_proxy"])
def test_existing_classes_unchanged(pair, pair_class):
    row, thesis, capture, proposal = pair
    row.pair_class = pair_class
    result = relatedness_projection([proposal])(row, thesis, capture)
    assert result.pair_class == pair_class
    assert "relatedness_basis" not in result.provenance


@pytest.mark.parametrize("tamper", ["rules", "claim", "basis", "authority", "diagnostics"])
def test_read_paths_revalidate_persisted_evidence(pair, tamper):
    row, thesis, capture, proposal = pair
    relatedness_projection([proposal])(row, thesis, capture)
    if tamper == "rules":
        capture.resolution_rules_text += " Changed terms."
    elif tamper == "claim":
        thesis.extracted_structure["contractible_version"] += " With a different meaning."
    elif tamper == "basis":
        row.provenance["relatedness_basis"] = None
    elif tamper == "authority":
        row.authority = "deterministic_only"
    else:
        row.provenance["per_market"]["checks"][0]["cap"] = "no_clean_expression"
    with pytest.raises(MarketPoolConflict):
        relatedness_evidence(row, thesis, capture)
