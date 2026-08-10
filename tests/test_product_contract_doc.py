"""AC-1: the UI contract exists and carries its required sections."""

from pathlib import Path

CONTRACT = (
    Path(__file__).resolve().parents[1] / "docs" / "agent-guided-ui-contract-v0.md"
)

REQUIRED_SECTIONS = (
    "## UI Modes / Surfaces",
    "## Identity Keys",
    "## Data Source per Field",
    "## Canonical Truth vs Advisory/Candidate Data",
    "## Actions That Mutate Ledger State",
    "## Forbidden Actions",
    "## Stale-State Clearing Rules",
    "## Failure / Empty States",
    "## Acceptance Criteria",
    "## Manual Smoke (AC-10)",
)


def test_contract_exists_with_required_sections():
    text = CONTRACT.read_text(encoding="utf-8")
    for section in REQUIRED_SECTIONS:
        assert section in text, f"contract missing section: {section}"


def test_contract_lists_all_acceptance_criteria():
    text = CONTRACT.read_text(encoding="utf-8")
    for i in range(1, 11):
        assert f"AC-{i}:" in text, f"contract missing AC-{i}"


def test_contract_names_loop_authority_and_vocabulary():
    text = CONTRACT.read_text(encoding="utf-8")
    assert "loops-with-gates" in text
    assert "no clean expression" in text
    for forbidden in ("wallets", "execution"):
        assert forbidden in text  # named as forbidden, per mission
