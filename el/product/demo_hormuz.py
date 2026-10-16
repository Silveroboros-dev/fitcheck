"""Visitor-owned reconstruction for the retained Hormuz rules demonstration.

Historical rules are exact. Baseline acceptance and market choice are labelled
test setup, with no semantic pair assessment or prior human attestation.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from hashlib import sha256
from importlib.resources import files

from sqlalchemy import select

from el.domain.tables import (
    CandidateSet, CandidateSetMember, FitCard, MarketAssessment, MarketChoice,
    MarketDisplayItem, MarketDisplaySet, MarketRulesCapture, MarketSnapshot,
    NormalizationAttempt, NormalizationDecision, PersonalThesis,
    ThesisAnalysis, ThesisRevision,
)
from el.product.wiring import HumanActor, ProductServices
from el.revisit.service import RevisitService, _digest

MARKER = "public-demo-hormuz-reconstruction-v1"
SOURCE_REF = "retained-market-rules:2176270:20260601T1800Z"
_seed_lock = threading.Lock()


def _definition() -> dict:
    raw = json.loads(files("el.product.demo_fixtures").joinpath("hormuz_rules.json").read_text())
    if raw["market_id"] != "2176270":
        raise ValueError("Hormuz fixture market identity changed")
    for side in ("old", "new"):
        if sha256(raw[f"{side}_rules"].encode()).hexdigest() != raw[f"{side}_rules_sha256"]:
            raise ValueError("Hormuz rules resource digest changed")
    return raw


def _service(services: ProductServices, actor: HumanActor) -> RevisitService:
    return RevisitService(
        services.session_factory, user_id=actor.user_id,
        actor_id=actor.actor_id, agent_client_id=actor.agent_client_id,
        mode="fixture",
    )


def ensure_hormuz(services: ProductServices, actor: HumanActor) -> dict:
    with _seed_lock:
        return _ensure_hormuz(services, actor)


def _finish_revision(services: ProductServices, actor: HumanActor,
                     revision_id: uuid.UUID, raw: dict) -> dict:
    service = _service(services, actor)
    capture_id = service.register_capture(
        revision_id, source_kind="retained_capture", source_ref=SOURCE_REF,
        observed_at=datetime.fromisoformat(raw["new_rules_first_seen"]),
        market_id=raw["market_id"], evidence={
            "market_id": raw["market_id"], "title": None,
            "rules": raw["new_rules"], "deadline": None,
            "lifecycle": None, "condition_id": None,
        },
    )
    checked = service.check(revision_id, idempotency_key=MARKER,
                            capture_id=capture_id)
    if (checked["observation"]["result"] != "revisit_proposed"
            or checked["observation"]["triggers"] != ["rules_changed"]):
        raise RuntimeError("Hormuz reconstruction failed its postcondition")
    return {"revision_id": str(revision_id), "test_only": True}


def _ensure_hormuz(services: ProductServices, actor: HumanActor) -> dict:
    if services.mode != "fixture":
        raise ValueError("Hormuz reconstruction requires fixture mode")
    raw = _definition()
    sessions = services.session_factory
    existing_revision = None
    with sessions() as s:
        existing_revision = s.scalar(
            select(ThesisRevision.id).join(PersonalThesis).where(
                PersonalThesis.user_id == actor.user_id,
                ThesisRevision.market_id == raw["market_id"],
                ThesisRevision.baseline["source_kind"].as_string() == "frozen_market_snapshot",
            )
        )
    if existing_revision is not None:
        # Each demo session owns only this seed; never select another visitor's
        # revision and never replace a decision already made in this session.
        return _finish_revision(services, actor, existing_revision, raw)

    thesis = raw["thesis"]
    marker = {"test_only": True, "reconstructed": MARKER,
              "semantic_fit_unassessed": True, "no_provider_or_model_call": True}
    ids = {name: uuid.uuid4() for name in (
        "attempt", "analysis", "decision", "rules", "candidate_set",
        "candidate_member", "fit_card", "assessment", "display",
        "display_item", "choice", "personal", "revision",
    )}
    snapshot_id = f"demo-hormuz-old:{actor.user_id}"
    old_at = datetime.fromisoformat(raw["old_rules_last_seen"])
    title_hash = sha256(raw["title"].encode()).hexdigest()
    disclosure = (
        "TEST-ONLY HISTORICAL REPLAY — thesis acceptance and market choice are "
        "reconstructed setup, not earlier human decisions. The old rules were "
        "last observed 1 June 2026 at 16:00 UTC; the new rules were first "
        "observed at 18:00 UTC. The edit time is unknown. Semantic market fit "
        "was not assessed."
    )
    baseline = {
        "market_id": raw["market_id"], "accepted_thesis": thesis,
        "source_excerpt": disclosure, "title": raw["title"],
        "rules": raw["old_rules"], "title_hash": title_hash,
        "rules_hash": raw["old_rules_sha256"], "deadline": None,
        "lifecycle": None, "condition_id": None,
        "snapshot_as_of": old_at.isoformat(),
        "source_kind": "frozen_market_snapshot",
    }
    with sessions.begin() as s:
        s.add(ThesisAnalysis(
            id=ids["analysis"], source_signal_id=None, input_text=thesis,
            extracted_structure=marker, normalized_claim_summary=thesis,
            client_type="human_ui", agent_client_id=actor.agent_client_id,
        ))
        s.add(NormalizationAttempt(
            id=ids["attempt"], predecessor_attempt_id=None,
            source_interpretation_id=None, source_thesis_candidate_id=None,
            input_text=thesis, input_digest=sha256(thesis.encode()).hexdigest(),
            source_url=None, outcome="candidate", verdict="test_only",
            proposal=marker, clarifying_question=None,
            reasons=["test_only_reconstruction"], gate_policy_version=MARKER,
            prompt_policy_version=MARKER, system_variant_id=MARKER,
            model_adapter=None, model_run_id=None, client_type="human_ui",
            agent_client_id=actor.agent_client_id,
        ))
        s.add(MarketSnapshot(
            id=snapshot_id, venue_id="polymarket", as_of_ts=old_at,
            retrieval_id="test-only-retained-rules-no-retrieval",
        ))
        # This graph binds scalar foreign-key IDs rather than ORM relationships.
        # Persist each parent layer before its descendants, within one transaction.
        s.flush()
        s.add(NormalizationDecision(
            id=ids["decision"], attempt_id=ids["attempt"], action="accept",
            thesis_analysis_id=ids["analysis"], actor_id=actor.actor_id,
            reason="TEST ONLY: reconstructed setup, no earlier human acceptance attested",
        ))
        s.add(MarketRulesCapture(
            id=ids["rules"], market_id=raw["market_id"], snapshot_id=snapshot_id,
            contract_terms_text=raw["title"], resolution_rules_text=raw["old_rules"],
            contract_terms_hash=title_hash,
            resolution_rules_hash=raw["old_rules_sha256"], rules_captured_at=old_at,
        ))
        s.add(CandidateSet(
            id=ids["candidate_set"], thesis_analysis_id=ids["analysis"],
            snapshot_id=snapshot_id, retrieval_id=None, retrieval_scope=marker,
            job_id=None,
        ))
        s.flush()
        s.add(CandidateSetMember(
            id=ids["candidate_member"], candidate_set_id=ids["candidate_set"],
            market_id=raw["market_id"], rank=1, retrieval_score=None,
            current_probability=None, eligibility_flags=marker,
            excluded_reason=None,
        ))
        s.add(FitCard(
            id=ids["fit_card"], thesis_analysis_id=ids["analysis"],
            candidate_set_id=ids["candidate_set"],
            semantic_fit_class=None, recommended_market_id=None,
            what_it_captures="Retained historical market rules only.",
            what_it_misses="No semantic fit was adjudicated in this reconstruction.",
            horizon_match="unknown", resolution_risk="unknown",
            fit_confidence=None, draft_contract_id=None, provenance=marker,
            job_id=None,
        ))
        s.flush()
        s.add(MarketAssessment(
            id=ids["assessment"], fit_card_id=ids["fit_card"],
            thesis_analysis_id=ids["analysis"],
            candidate_set_id=ids["candidate_set"],
            candidate_set_member_id=ids["candidate_member"],
            rules_capture_id=ids["rules"], snapshot_id=snapshot_id,
            market_id=raw["market_id"], retrieval_rank=1,
            pair_class=None,
            what_it_captures="Retained rules; no fit claim.",
            what_it_misses="No normal discovery or pair-fit assessment was performed.",
            horizon_match=None, resolution_risk=None,
            authority="demo_reconstruction_only", fit_confidence=None,
            provenance=marker,
        ))
        s.add(MarketDisplaySet(
            id=ids["display"], fit_card_id=ids["fit_card"],
            thesis_analysis_id=ids["analysis"],
            candidate_set_id=ids["candidate_set"], snapshot_id=snapshot_id,
            display_policy_version=MARKER, assessed_count=0, target_count=1,
            displayed_count=1, assessment_complete=False,
            system_pool_outcome="incomplete",
            incomplete_reasons=["test_only_no_semantic_assessment"],
        ))
        s.flush()
        s.add(MarketDisplayItem(
            id=ids["display_item"], market_display_set_id=ids["display"],
            market_assessment_id=ids["assessment"], display_rank=1,
        ))
        s.add(MarketChoice(
            id=ids["choice"], market_display_set_id=ids["display"],
            selection_kind="market", market_assessment_id=ids["assessment"],
            actor_id=actor.actor_id, client_type="human_ui",
            reason="TEST ONLY: reconstructed setup, no earlier human choice attested",
        ))
        s.add(PersonalThesis(
            id=ids["personal"], user_id=actor.user_id,
            active_revision_id=ids["revision"],
        ))
        s.flush()
        pinned = {
            "actor": actor.actor_id, "analysis": str(ids["analysis"]),
            "decision": str(ids["decision"]), "source_choice": "None",
            "display": str(ids["display"]), "choice": str(ids["choice"]),
            "assessment": str(ids["assessment"]), "snapshot": snapshot_id,
            "rules_capture": str(ids["rules"]), "baseline": baseline,
        }
        s.add(ThesisRevision(
            id=ids["revision"], personal_thesis_id=ids["personal"],
            thesis_analysis_id=ids["analysis"],
            normalization_decision_id=ids["decision"],
            source_candidate_choice_id=None,
            market_display_set_id=ids["display"], market_choice_id=ids["choice"],
            market_assessment_id=ids["assessment"], market_id=raw["market_id"],
            snapshot_id=snapshot_id, rules_capture_id=ids["rules"],
            revision_digest=_digest(pinned), baseline=baseline,
            activated_by=actor.actor_id,
        ))
    return _finish_revision(services, actor, ids["revision"], raw)


def bound_impact_copy(saved: dict) -> dict | None:
    """Rebind owner-approved copy to this newly seeded graph, never old QA IDs."""
    from el.product.app import _bound_fixture_impact

    activation = saved.get("activation") or {}
    latest = saved.get("latest") or {}
    binding = activation.get("binding") or {}
    baseline = activation.get("baseline") or {}
    observation = latest.get("observation") or {}
    raw = _definition()
    if (binding.get("market_id") != raw["market_id"]
            or baseline.get("rules_hash") != raw["old_rules_sha256"]
            or (observation.get("after") or {}).get("rules_hash") != raw["new_rules_sha256"]
            or observation.get("source_ref") != SOURCE_REF):
        return None
    copy = json.loads(files("el.product.demo_fixtures").joinpath("hormuz_impact_copy.json").read_text())
    note = {
        "binding": binding,
        "observation_id": observation.get("id"),
        "capture_id": observation.get("capture_id"),
        "capture_digest": observation.get("capture_digest"),
        "source_kind": observation.get("source_kind"),
        "source_ref": observation.get("source_ref"),
        "policy_version": observation.get("policy_version"),
        "packet_id": (latest.get("packet") or {}).get("id"),
        "packet_digest": (latest.get("packet") or {}).get("digest"),
        "before_rules_hash": raw["old_rules_sha256"],
        "after_rules_hash": raw["new_rules_sha256"],
        **copy,
    }
    return _bound_fixture_impact(note, saved)
