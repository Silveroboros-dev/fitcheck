from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "el" / "review" / "static" / "review_console.html"


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def test_review_health_redacts_validation_error_details():
    from fastapi.testclient import TestClient

    from el.review.reference_app import build_app
    from el.review.reference_dataset import ReviewDataError

    sensitive_detail = "INTERNAL_VALIDATION_DETAIL_DO_NOT_EXPOSE"

    class InvalidRepository:
        def validate(self) -> None:
            raise ReviewDataError(sensitive_detail)

    response = TestClient(build_app(InvalidRepository())).get("/review/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error": "review data validation failed",
    }
    assert sensitive_detail not in response.text


def test_review_page_contains_required_controls():
    html = _html()
    for control in (
        "fit_class",
        "confidence",
        "case_quality",
        "failure_taxonomy",
        "false_strong_risk",
        "selected_market_snapshot_id",
        "preferred_judgment_id",
        "recommendation",
        "notes",
    ):
        assert f'id="{control}"' in html


def test_review_page_marks_expected_reviewer_fields_required():
    html = _html()

    assert 'id="reviewer_id" name="reviewer_id" type="text" value="ruslan" required' in html
    assert 'id="failure_taxonomy" name="failure_taxonomy" type="text" list="failure-taxonomy-options" placeholder="no_failure" required' in html
    assert '<option value="no failure">' in html
    assert '<option value="no_failure">' in html
    assert 'id="selected_market_snapshot_id" name="selected_market_snapshot_id" required' in html
    assert 'id="notes" name="notes" required' in html
    assert 'id="review-fields-warning"' in html


def test_review_page_surfaces_market_identity_and_rules():
    html = _html()

    assert "Review Focus" in html
    assert "Case ID:" in html
    assert "Comparison market title:" in html
    assert "Same normalized thesis group:" in html
    assert "Exact duplicate payload group:" in html
    assert "Proposed Thesis" in html
    assert "No proposed or normalized thesis is attached" in html
    assert "Reference Candidate" in html
    assert "Best market ID" in html
    assert "Market ID:" in html
    assert "Market snapshot ID:" in html
    assert "Kind:" in html
    assert "Provider:" in html
    assert "Chosen market snapshot" in html
    assert "Choose one proposed snapshot, or choose none" in html
    assert "none - no proposed market is relevant" in html
    assert "Resolution rules:" in html
    assert "No frozen market snapshots are attached" in html
    assert "hidden from the default review queue" in html


def test_review_page_binds_packet_id_explicitly():
    html = _html()

    assert 'id="packet_id"' in html
    assert "currentPacketId" in html
    assert "selectPacket(packet.packet_id" in html
    assert "clearActivePacket" in html


def test_review_page_labels_candidate_judgments_as_advisory():
    html = _html()

    assert "Advisory Candidate Judgments" in html
    assert "ADVISORY ONLY" in html
    assert "They cannot write reference labels" in html
    assert "Preferred advisory judgment (from candidate_judgments_v0.jsonl)" in html
    assert "No advisory candidate judgments are attached" in html
    assert "Optional. Use this only when" in html
    assert "it creates preference-pair data, not reference truth" in html
    assert 'id="advisory-preference-fields" hidden' in html
    assert "advisoryPreferenceFields.hidden = !hasCandidateJudgments" in html


def test_review_page_hides_reviewed_packets_after_submit():
    html = _html()

    assert "No pending review packets. Reviewed packets are hidden from this queue." in html
    assert "reviewed packet removed from the queue" in html


def test_review_page_can_show_duplicate_payloads_for_audit():
    html = _html()

    assert 'id="include_duplicates"' in html
    assert "Show duplicate payloads" in html
    assert "Default queue hides exact duplicate source/thesis/market evidence" in html
    assert "include_duplicates=true" in html


def test_review_page_allows_switching_with_incomplete_fields():
    html = _html()

    assert "reviewValidationIssues" in html
    assert "canLeaveActivePacket" not in html
    assert "beforeunload" not in html
    assert "Complete these required review fields before submitting" in html
    assert "clear unsaved form state" not in html.lower()


def test_review_page_marks_missing_blockers_red():
    html = _html()

    assert ".field-blocker" in html
    assert "border-color: #c81e1e" in html
    assert "markBlockingFields" in html
    assert "aria-invalid" in html
    assert "market snapshot choice" in html


def test_review_page_submits_selected_market_snapshot():
    html = _html()

    assert "market_snapshot_selection: noProposedMarket ? 'none' : 'selected'" in html
    assert "selected_market_snapshot_id: noProposedMarket ? null : selectedMarketValue || null" in html
    assert "selectedSnapshot.appendChild(option)" in html
    assert "Reviewed exports keep this one choice" in html
    assert "direct/indirect/weak proxy require a selected market snapshot" in html
    assert "no clean expression must use none for market snapshot choice" in html


def test_review_page_can_edit_market_snapshot_evidence():
    html = _html()

    assert "Edit market snapshot" in html
    assert "Save edited snapshot" in html
    assert "reviewer-authored frozen evidence" in html
    assert "/market-snapshots" in html
    assert "source_market_snapshot_id" in html
    assert "snapshotEditValue" in html


def test_review_page_hides_advisory_preference_controls_when_unavailable():
    html = _html()

    assert "Preference-pair controls are hidden for this packet" in html
    assert "preferred.disabled = !hasCandidateJudgments" in html
    assert "preferenceStrength.disabled = !hasCandidateJudgments" in html


def test_review_page_exposes_only_pending_and_correction_modes():
    html = _html()

    assert 'id="queue_mode"' in html
    assert "Decision-quality corrections" in html
    assert "Weak-proxy recheck" not in html
    assert "/review/rechecks" not in html
    assert "/review/corrections" in html
    assert "audit_flags" in html
    assert "currentDecisionEditId" in html
    assert "Correction mode: editing existing decision row" in html
    assert "no new review record is created" in html


def test_review_page_updates_existing_decision_in_correction_mode():
    html = _html()

    assert "populateReviewFormFromDecision" in html
    assert "latest_decision" in html
    assert "PUT" in html
    assert "/review/decisions/${encodeURIComponent(currentDecisionEditId)}" in html
    assert "updated existing" in html
    assert "no new review record created" in html
    assert "cleared from correction queue" in html
