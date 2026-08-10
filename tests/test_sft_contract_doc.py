"""SFT fit-classifier contract checks."""

from pathlib import Path


CONTRACT = (
    Path(__file__).resolve().parents[1] / "docs" / "sft-fit-classifier-contract-v1.md"
)

REQUIRED_SECTIONS = (
    "## Purpose",
    "## Source Data",
    "## Splits",
    "## Output Files",
    "## Readiness Rule",
    "## Inference",
    "## Measurement",
    "## Promotion Gate",
    "## Claim Discipline",
    "## Acceptance Criteria",
)


def test_sft_contract_exists_with_required_sections():
    text = CONTRACT.read_text(encoding="utf-8")
    for section in REQUIRED_SECTIONS:
        assert section in text, f"contract missing section: {section}"


def test_sft_contract_lists_acceptance_criteria():
    text = CONTRACT.read_text(encoding="utf-8")
    for i in range(1, 8):
        assert f"AC-{i}:" in text, f"contract missing AC-{i}"


def test_sft_contract_pins_training_boundaries():
    text = CONTRACT.read_text(encoding="utf-8")
    assert "validated_rejection_sentinels_v1" in text
    assert "legacy_regression" in text
    assert "excluded from training" in text.lower()
    assert "ready_for_plumbing_spike_only" in text
    assert "candidate_for_small_sft" in text


def test_sft_contract_requires_explicit_tuned_model_and_gates():
    text = CONTRACT.read_text(encoding="utf-8")
    assert "must not fall back to `GEMINI_MODEL`" in text
    assert "sentinel false strong = 0" in text
    assert "direct false positives do not increase" in text
    assert "Product wiring" in text
    assert "explicit human approval" in text
