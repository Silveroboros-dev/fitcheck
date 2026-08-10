from pathlib import Path

from el.review.reference_dataset import (
    build_governance_packet,
    build_source_candidate_packet,
    build_stress_packet,
)


def _governance_row(**overrides):
    row = {
        "governance_id": "gov_001",
        "case_id": "case-1",
        "source_text": "OpenAI is preparing to file confidentially.",
        "source_provenance": {"source_name": "X / source"},
        "normalized_claim": {"claim_text": "OpenAI IPO filing preparation"},
        "expected_fit_class": "indirect",
        "expected_best_market_id": "2314379",
        "acceptable_market_ids": ["656312"],
        "rejected_market_ids": ["2299990"],
        "review_status": "promote",
        "truth_scope": "reviewed_candidate",
        "expected_behavior": "Do not treat IPO completion as filing preparation.",
        "failure_modes": ["event_stage_mismatch"],
        "curation_priority": 190,
    }
    row.update(overrides)
    return row


def _stress_row():
    return {
        "case_id": "stress_001",
        "thesis": "OpenAI is preparing to file confidentially for an IPO.",
        "expected_fit_class": "weak_proxy",
        "expected_label_source": "constructed_template",
        "mismatch_family": "event_stage_mismatch",
        "truth_scope": "synthetic_expected_label",
        "trap_description": "Filing is not completion.",
        "market": {
            "market_id": "synth_openai_ipo_cap",
            "venue": "SyntheticStress",
            "title": "OpenAI IPO market cap above $300B?",
            "description": "Synthetic stress market.",
            "resolution_rules": "Resolves on IPO completion and first-day market cap.",
            "outcomes": ["Yes", "No"],
            "close_date": "2026-12-31",
        },
    }


def test_governance50_packet_preserves_source_fields():
    packet = build_governance_packet(_governance_row())

    assert packet.packet_id == "pkt_mfta_governance_50_gov_001"
    assert packet.source_dataset == "mfta_governance_50"
    assert packet.source_case_id == "gov_001"
    assert packet.source_text == "OpenAI is preparing to file confidentially."
    assert packet.source_provenance["source_name"] == "X / source"
    assert packet.reference_candidate["fit_class"] == "indirect"
    assert packet.reference_candidate["best_market_id"] == "2314379"
    assert packet.reference_candidate["review_status_from_source"] == "promote"
    assert packet.reference_candidate["truth_scope_from_source"] == "reviewed_candidate"
    assert packet.priority == 190
    assert packet.source_row_hash.startswith("sha256:")


def test_stress40_packet_is_marked_synthetic_stress():
    packet, snapshot = build_stress_packet(_stress_row())

    assert packet.source_scope == "synthetic_stress"
    assert packet.source_dataset == "mfta_stress_40"
    assert packet.market_snapshot_ids == [snapshot.market_snapshot_id]
    assert snapshot.market_role == "stress_embedded"
    assert snapshot.resolution_rules.startswith("Resolves on IPO completion")


def test_v2_v3_candidates_are_source_only():
    packet = build_source_candidate_packet(
        {
            "example_id": "eval_v2_005",
            "source_text": "Gemini distribution strategy post.",
            "source_provenance": {"source_name": "X / author"},
        },
        {
            "expected_thesis": {"summary": "Distribution thesis"},
            "expected_fit": {
                "semantic_fit_class": "no_clean_expression",
                "best_market_id": None,
                "case_tags": ["no_market_found"],
            },
        },
        pack_name="mfta_market_fit_v2_candidates",
    )

    assert packet.source_scope == "source_text_and_provenance_only"
    assert packet.review_state == "unreviewed"
    assert packet.reference_candidate["fit_class"] == "no_clean_expression"
    assert packet.market_snapshot_ids == []


def test_packet_hash_changes_when_source_changes():
    first = build_governance_packet(_governance_row())
    second = build_governance_packet(
        _governance_row(source_text="OpenAI has already completed an IPO.")
    )

    assert first.source_row_hash != second.source_row_hash


def test_builder_does_not_write_review_decisions(tmp_path):
    build_governance_packet(_governance_row())
    build_stress_packet(_stress_row())

    assert not (tmp_path / "review_decisions_v0.jsonl").exists()
