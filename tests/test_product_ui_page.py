"""Static product page checks: stale-state hooks, candidate labeling,
restricted vocabulary (AC-3, AC-4, AC-8)."""

import re
from pathlib import Path

PAGE = (
    Path(__file__).resolve().parents[1]
    / "el"
    / "product"
    / "static"
    / "product_console.html"
)
HTML = PAGE.read_text(encoding="utf-8")


def test_editing_source_clears_downstream_state():
    # Changing the input marks downstream stale; a run resets the chain.
    assert 'addEventListener("input", resetDownstream)' in HTML
    assert "chain.thesisId = null" in HTML
    assert "chain.fitCardId = null" in HTML


def test_responses_are_dropped_when_chain_moved_on():
    assert HTML.count("!== chain.thesisId") >= 2
    assert HTML.count("!== chain.fitCardId") >= 2


def test_candidate_markets_labeled_as_evidence_not_verdict():
    assert "candidate markets — retrieval evidence, not a verdict" in HTML


def test_captures_misses_and_tempting_rejections_are_first_class():
    assert "What this contract tests" in HTML
    assert "Comparison with your thesis" in HTML
    assert "Rejected tempting markets" in HTML


def test_no_clean_state_is_first_class():
    assert "No clean expression among the checked candidates" in HTML
    assert "draft contract candidate" in HTML.lower()


def test_restored_source_precedes_accepted_thesis_and_clears_on_edit():
    accepted = HTML[HTML.index('id="s1-out"'):HTML.index('id="s1-err"')]
    assert accepted.index('id="restored-source"') < accepted.index('id="norm-summary"')
    assert 'id="restored-source-text"' in accepted
    assert '$("restored-source-text").textContent = ""' in HTML
    assert "function resumeAcceptedThesis(" in HTML


def test_save_starts_disabled_and_requires_fields():
    assert re.search(r'id="save"\s+disabled', HTML)
    assert "conviction, exposure and justification are required" in HTML


def test_no_restricted_vocabulary_in_system_copy():
    lowered = HTML.lower()
    for phrase in (
        "risk-free",
        "you should trade",
        "place a trade",
        "wallet",
    ):
        assert phrase not in lowered
    # Word-boundary check for terms that could hide inside other words.
    for word in ("buy", "sell", "edge"):
        assert not re.search(rf"\b{word}\b", lowered), f"restricted word: {word}"


def test_blind_prior_panel_precedes_market_card():
    assert HTML.index('id="s2"') < HTML.index('id="s3"')
    assert "before any market" in HTML
    assert "before market odds" in HTML
    assert "before market context" not in HTML
