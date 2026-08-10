from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "reviewed-reference-dataset-spec-v0.md"


def _text() -> str:
    return CONTRACT.read_text(encoding="utf-8")


def test_contract_contains_acceptance_criteria():
    text = _text()
    for n in range(1, 36):
        assert f"AC-{n}:" in text


def test_contract_declares_identity_keys():
    text = _text()
    for key in (
        "packet_id",
        "source_dataset",
        "source_case_id",
        "source_row_hash",
        "market_snapshot_selection",
        "selected_market_snapshot_id",
        "review_decision_id",
        "reviewer_id",
    ):
        assert key in text


def test_contract_declares_mutation_boundary():
    text = _text()
    assert "immutable" in text
    assert "append-only" in text
    assert "correction mode" in text
    assert "replace one existing latest review" in text
    assert "must not mutate packets" in text
    assert "explicit reviewer snapshot-edit endpoint" in text
    assert "mutate review decisions" in text
    assert "advisory candidate judgments" in text


def test_contract_declares_advisory_model_outputs():
    text = _text()
    assert "advisory" in text
    assert "Model outputs cannot write or modify reference labels" in text


def test_contract_declares_duplicate_thesis_review_focus():
    text = _text()

    assert "review-focus summary" in text
    assert "same-normalized-thesis count" in text
    assert "comparison market" in text
    assert "before repeated normalized-thesis text" in text


def test_contract_declares_retired_weak_proxy_flow_and_correction_mode():
    text = _text()

    assert "one-off weak-proxy recheck queue is retired" in text
    assert "`GET /review/rechecks`" not in text
    assert "exactly two reviewer queues" in text
    assert "review work enters only through pending review or decision-quality" in text
    assert "preserving" in text
    assert "`review_decision_id`" in text
    assert "`reviewed_at_utc`" in text
    assert "`recheck_status=cleared`" in text
    assert "without appending a new" in text
    assert "Decision-quality correction mode" in text
    assert "manual correction requests" in text
    assert "Direct, indirect, and weak-proxy review decisions require exactly one" in text


def test_contract_declares_normalization_repair_boundary():
    text = _text()

    assert "Normalization Repair Contract" in text
    assert "advisory repair candidates" in text
    assert "explicit `--apply`" in text
    assert "original packet and all historical review decisions remain unchanged" in text
    assert "never modify `review_decisions_v0.jsonl`" in text
    assert "cannot promote itself" in text


def test_contract_declares_reviewed_fit_testing_tiers():
    text = _text()

    assert "Reviewed Fit Testing Dataset Contract" in text
    assert "evaluation_tier=clean_primary" in text
    assert "evaluation_tier=legacy_regression" in text
    assert "source_unavailable" in text
    assert "must never back-promote legacy rows" in text
