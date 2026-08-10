"""Step 5b — draft-contract gate: D1-D5 validation (blueprint §13)."""

from datetime import date

from el.domain.structures import (
    ClaimHorizon,
    Entity,
    ExtractedStructure,
    Mechanism,
    Metric,
)
from el.draftcontract.gate import DraftGateVerdict, draft_contract_gate
from el.models.draft_adapter import ProposedDraft


def _claim(**overrides) -> ExtractedStructure:
    base = dict(
        claim_summary="OpenAI releases GPT-5 by December 2026.",
        entities=[
            Entity(name="OpenAI", role="subject"),
            Entity(name="GPT-5", role="object"),
        ],
        event_stage="launched",
        metric=Metric(
            what="GPT-5 general availability",
            measured_by="OpenAI release channels",
            objective=True,
        ),
        horizon=ClaimHorizon(window_end=date(2026, 12, 31), precision="day"),
        mechanism=Mechanism(),
        stance="yes",
        resolution_source_class="press",
        contractible_version="Will OpenAI release GPT-5 by Dec 31, 2026?",
    )
    base.update(overrides)
    return ExtractedStructure(**base)


def _good_draft(**overrides) -> ProposedDraft:
    base = dict(
        proposed_title=(
            "Will OpenAI make GPT-5 generally available to the public on or "
            "before December 31, 2026?"
        ),
        proposed_resolution_logic=(
            "Resolves YES if OpenAI provides public access to a model "
            "designated GPT-5 via consumer product or API before the deadline."
        ),
        resolution_source="OpenAI official release announcements",
        resolution_source_class="press",
        resolution_deadline=date(2026, 12, 31),
        subject_entity="OpenAI",
        event_stage="launched",
        category="ai_models",
        time_horizon="by end of 2026",
    )
    base.update(overrides)
    return ProposedDraft(**base)


def test_announce_for_launch_draft_rejected():
    # announce-vs-release drift: the claim is about a LAUNCH; a draft that
    # resolves on ANNOUNCE is a different, easier event -> D7 fails.
    result = draft_contract_gate(
        claim=_claim(event_stage="launched"),
        proposed=_good_draft(
            proposed_title=(
                "Will OpenAI announce GPT-5 on or before December 31, 2026?"
            ),
            event_stage="announced",
        ),
    )
    assert result.verdict is DraftGateVerdict.REJECTED_INVALID
    assert any(c.check_id == "D7" and c.status == "fail" for c in result.checks)


def test_clean_draft_passes_all_checks():
    result = draft_contract_gate(claim=_claim(), proposed=_good_draft())
    assert result.verdict is DraftGateVerdict.PASS
    assert result.draft is not None
    assert [c.check_id for c in result.checks] == [
        "D1",
        "D2",
        "D3",
        "D4",
        "D5",
        "D6",
        "D7",
    ]
    assert all(c.status == "pass" for c in result.checks)
    assert result.reasons == []


def test_drifted_object_metric_rejected():
    # The reviewer's case: shape-valid but semantically wrong — subject,
    # deadline, source all fine, but "any product update" is not the
    # "GPT-5 release" the claim measures.
    result = draft_contract_gate(
        claim=_claim(),
        proposed=_good_draft(
            proposed_title=(
                "Will OpenAI publish any product update on or before "
                "December 31, 2026?"
            ),
            proposed_resolution_logic=(
                "Resolves YES if OpenAI publishes any product update before "
                "the deadline."
            ),
        ),
    )
    assert result.verdict is DraftGateVerdict.REJECTED_INVALID
    assert any(c.check_id == "D6" and c.status == "fail" for c in result.checks)


def test_drifted_deadline_rejected():
    # The eval_002 launch->announce failure, productized: the draft commits
    # to a deadline that drifts from the claim's window end (settlement date).
    result = draft_contract_gate(
        claim=_claim(), proposed=_good_draft(resolution_deadline=date(2027, 1, 31))
    )
    assert result.verdict is DraftGateVerdict.REJECTED_INVALID
    assert any(c.check_id == "D3" and c.status == "fail" for c in result.checks)


def test_subject_absent_from_prose_rejected():
    # subject_entity is a real claim entity, but the prose never names it —
    # a generic title is the mad-libs failure D4 exists to catch.
    result = draft_contract_gate(
        claim=_claim(),
        proposed=_good_draft(
            proposed_title="Will the company ship a new model before the deadline?",
            proposed_resolution_logic="Resolves YES on a public model release.",
            subject_entity="OpenAI",
        ),
    )
    assert result.verdict is DraftGateVerdict.REJECTED_INVALID
    assert any(c.check_id == "D4" and c.status == "fail" for c in result.checks)


def test_hallucinated_subject_rejected():
    # Declared subject is not a claim subject at all.
    result = draft_contract_gate(
        claim=_claim(),
        proposed=_good_draft(
            proposed_title="Will Google ship Gemini Ultra before December 31, 2026?",
            proposed_resolution_logic="Resolves YES on a Google release.",
            subject_entity="Google",
        ),
    )
    assert result.verdict is DraftGateVerdict.REJECTED_INVALID
    assert any(c.check_id == "D4" and c.status == "fail" for c in result.checks)


def test_restricted_vocabulary_rejected():
    result = draft_contract_gate(
        claim=_claim(),
        proposed=_good_draft(
            proposed_resolution_logic=(
                "A risk-free outcome; you should buy OpenAI GPT-5 exposure."
            )
        ),
    )
    assert result.verdict is DraftGateVerdict.REJECTED_INVALID
    assert any(c.check_id == "D2" and c.status == "fail" for c in result.checks)


def test_unobservable_source_rejected():
    # The draft must UPGRADE resolvability — class 'none' is not a test.
    result = draft_contract_gate(
        claim=_claim(),
        proposed=_good_draft(
            resolution_source_class="none", resolution_source="nobody"
        ),
    )
    assert result.verdict is DraftGateVerdict.REJECTED_INVALID
    assert any(c.check_id == "D5" and c.status == "fail" for c in result.checks)


def test_subject_echo_falls_back_to_all_entities():
    # A claim with no subject-role entity still gets a real echo check
    # (fallback to all entity names), never a vacuous pass. Metric is
    # coherent with the draft so D6 also passes.
    claim = _claim(
        entities=[Entity(name="LMSYS Arena", role="venue")],
        metric=Metric(
            what="LMSYS Arena #1 ranking",
            measured_by="LMSYS leaderboard",
            objective=True,
        ),
    )
    result = draft_contract_gate(
        claim=claim,
        proposed=_good_draft(
            proposed_title=(
                "Will LMSYS Arena publish a #1 ranking on or before "
                "December 31, 2026?"
            ),
            proposed_resolution_logic=(
                "Resolves YES when LMSYS Arena publishes the #1 ranking."
            ),
            subject_entity="LMSYS Arena",
        ),
    )
    assert result.verdict is DraftGateVerdict.PASS


def test_multiple_failures_collect_all_reasons():
    result = draft_contract_gate(
        claim=_claim(),
        proposed=_good_draft(
            resolution_deadline=date(2027, 1, 31),
            resolution_source_class="none",
            resolution_source="nobody",
        ),
    )
    assert result.verdict is DraftGateVerdict.REJECTED_INVALID
    failed = {c.check_id for c in result.checks if c.status == "fail"}
    assert {"D3", "D5"} <= failed
    assert len(result.reasons) == len(failed)
