"""Local-human, saved-evidence revisit for one selected market."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.tables import (
    CandidateSet, CandidateSetMember, HumanRevisitDecision, MarketAssessment,
    MarketChoice, MarketDisplayItem, MarketDisplaySet, MarketRulesCapture,
    MarketSnapshot, MonitoringObservation, NormalizationAttempt,
    NormalizationDecision, PersonalThesis, RevisitCapture, RevisitPacket,
    RevisitRequest, SourceCandidateChoice, SourceInterpretation,
    SourceThesisCandidate, ThesisAnalysis, ThesisRevision,
)
from el.revisit.comparator import POLICY_VERSION, compare_market_evidence
from el.sourceinterpretation.binding import (
    DurableSourceBindingError,
    require_durable_source_binding,
)


class RevisitNotFound(Exception):
    pass


class RevisitConflict(Exception):
    pass


class RevisitUnavailable(Exception):
    pass


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class RevisitService:
    def __init__(
        self, sessions: sessionmaker[Session], *, user_id: uuid.UUID,
        actor_id: str, agent_client_id: str, mode: str,
    ):
        self._sessions = sessions
        self._user_id = user_id
        self._actor_id = actor_id
        self._agent_client_id = agent_client_id
        self._mode = mode

    def _owned_revision(self, s: Session, revision_id: uuid.UUID) -> tuple[PersonalThesis, ThesisRevision]:
        revision = s.get(ThesisRevision, revision_id)
        thesis = s.get(PersonalThesis, revision.personal_thesis_id) if revision else None
        if thesis is None or thesis.user_id != self._user_id or revision.activated_by != self._actor_id:
            raise RevisitNotFound()
        pinned = {
            "actor": revision.activated_by,
            "analysis": str(revision.thesis_analysis_id),
            "decision": str(revision.normalization_decision_id),
            "source_choice": str(revision.source_candidate_choice_id),
            "display": str(revision.market_display_set_id),
            "choice": str(revision.market_choice_id),
            "assessment": str(revision.market_assessment_id),
            "snapshot": revision.snapshot_id,
            "rules_capture": str(revision.rules_capture_id),
            "baseline": revision.baseline,
        }
        if _digest(pinned) != revision.revision_digest:
            raise RevisitConflict("activated revision digest is invalid")
        return thesis, revision

    @staticmethod
    def _binding(revision: ThesisRevision) -> dict:
        return {
            "personal_thesis_id": str(revision.personal_thesis_id),
            "revision_id": str(revision.id),
            "revision_digest": revision.revision_digest,
            "thesis_analysis_id": str(revision.thesis_analysis_id),
            "normalization_decision_id": str(revision.normalization_decision_id),
            "source_candidate_choice_id": str(revision.source_candidate_choice_id) if revision.source_candidate_choice_id else None,
            "market_display_set_id": str(revision.market_display_set_id),
            "market_choice_id": str(revision.market_choice_id),
            "market_assessment_id": str(revision.market_assessment_id),
            "market_id": revision.market_id,
            "baseline_snapshot_id": revision.snapshot_id,
            "baseline_rules_capture_id": str(revision.rules_capture_id),
            "baseline_rules_digest": revision.baseline["rules_hash"],
        }

    def _activation_out(self, s: Session, thesis: PersonalThesis, revision: ThesisRevision) -> dict:
        captures = s.scalars(select(RevisitCapture).where(
            RevisitCapture.revision_id == revision.id,
            RevisitCapture.source_kind == "retained_capture",
        ).order_by(RevisitCapture.created_at.desc(), RevisitCapture.id.desc()).limit(20)).all()
        return {
            "binding": self._binding(revision),
            "baseline": revision.baseline,
            "active": thesis.active_revision_id == revision.id,
            "registered_captures": [
                {"capture_id": str(row.id), "source_kind": row.source_kind,
                 "observed_at": _utc(row.observed_at), "receipt_at": _utc(row.receipt_at)}
                for row in captures
            ],
        }

    def _source_binding(
        self, s: Session, accepted: NormalizationAttempt
    ) -> tuple[uuid.UUID | None, str | None]:
        """Walk validated human edit predecessors to the selected source."""
        attempt = accepted
        visited: set[uuid.UUID] = set()
        for _ in range(32):
            if (attempt.id in visited or attempt.client_type != "human_ui"
                or attempt.agent_client_id != self._agent_client_id):
                raise RevisitConflict("normalization predecessor binding is invalid")
            visited.add(attempt.id)
            has_source = (attempt.source_interpretation_id is not None
                or attempt.source_thesis_candidate_id is not None)
            if has_source:
                if (attempt.source_interpretation_id is None
                    or attempt.source_thesis_candidate_id is None
                    or attempt.predecessor_attempt_id is not None):
                    raise RevisitConflict("source binding is partial")
                break
            if attempt.predecessor_attempt_id is None:
                return None, None
            prior = s.get(NormalizationAttempt, attempt.predecessor_attempt_id)
            prior_decision = s.scalar(select(NormalizationDecision).where(
                NormalizationDecision.attempt_id == attempt.predecessor_attempt_id,
            ))
            if (prior is None or prior_decision is None
                or prior_decision.action != "edit"
                or prior_decision.actor_id != self._actor_id):
                raise RevisitConflict("human edit predecessor is invalid")
            attempt = prior
        else:
            raise RevisitConflict("normalization predecessor chain is too long")
        candidate = s.get(SourceThesisCandidate, attempt.source_thesis_candidate_id)
        interpretation = s.get(SourceInterpretation, attempt.source_interpretation_id)
        source_choice = s.scalar(select(SourceCandidateChoice).where(
            SourceCandidateChoice.source_interpretation_id == attempt.source_interpretation_id,
        ))
        if (candidate is None or interpretation is None or source_choice is None
            or interpretation.outcome != "candidates"
            or interpretation.client_type != "human_ui"
            or interpretation.agent_client_id != self._agent_client_id
            or interpretation.input_text is None
            or candidate.source_interpretation_id != interpretation.id
            or source_choice.selection_kind != "candidate"
            or source_choice.source_thesis_candidate_id != candidate.id
            or source_choice.actor_id != self._actor_id
            or source_choice.client_type != "human_ui"
            or attempt.input_text != candidate.selected_source_quote
            or attempt.input_digest != candidate.source_quote_digest
            or candidate.selected_source_quote not in interpretation.input_text
            or sha256(interpretation.input_text.encode()).hexdigest() != interpretation.input_digest
            or sha256(candidate.selected_source_quote.encode()).hexdigest() != candidate.source_quote_digest):
            raise RevisitConflict("selected source binding is invalid")
        try:
            require_durable_source_binding(
                s,
                interpretation,
                owner_client_type="human_ui",
                owner_actor_id=self._actor_id,
                owner_user_id=self._user_id,
                agent_client_id=self._agent_client_id,
            )
        except DurableSourceBindingError as exc:
            raise RevisitConflict("durable source binding is invalid") from exc
        return source_choice.id, candidate.selected_source_quote

    def activation_for_choice(self, market_choice_id: uuid.UUID) -> dict:
        with self._sessions() as s:
            revision = s.scalar(select(ThesisRevision).where(ThesisRevision.market_choice_id == market_choice_id))
            if revision is None:
                raise RevisitNotFound()
            thesis, revision = self._owned_revision(s, revision.id)
            return self._activation_out(s, thesis, revision)

    def activate(self, market_choice_id: uuid.UUID) -> dict:
        with self._sessions() as s:
            choice = s.get(MarketChoice, market_choice_id)
            if choice is None or choice.actor_id != self._actor_id or choice.client_type != "human_ui":
                raise RevisitNotFound()
            if choice.selection_kind != "market" or choice.market_assessment_id is None:
                raise RevisitUnavailable("selected-contract revisit is unavailable for none")
            display = s.get(MarketDisplaySet, choice.market_display_set_id)
            assessment = s.get(MarketAssessment, choice.market_assessment_id)
            item = s.scalar(select(MarketDisplayItem).where(
                MarketDisplayItem.market_display_set_id == choice.market_display_set_id,
                MarketDisplayItem.market_assessment_id == choice.market_assessment_id,
            ))
            if display is None or assessment is None or item is None:
                raise RevisitConflict("choice is not bound to a displayed assessment")
            analysis = s.get(ThesisAnalysis, display.thesis_analysis_id)
            decision = s.scalar(select(NormalizationDecision).where(
                NormalizationDecision.thesis_analysis_id == display.thesis_analysis_id,
                NormalizationDecision.action == "accept",
            ))
            if (analysis is None or decision is None or decision.actor_id != self._actor_id
                or analysis.client_type != "human_ui" or analysis.agent_client_id != self._agent_client_id):
                raise RevisitNotFound()
            attempt = s.get(NormalizationAttempt, decision.attempt_id)
            candidate_set = s.get(CandidateSet, display.candidate_set_id)
            member = s.get(CandidateSetMember, assessment.candidate_set_member_id)
            capture = s.get(MarketRulesCapture, assessment.rules_capture_id)
            snapshot = s.get(MarketSnapshot, display.snapshot_id)
            if (attempt is None or attempt.outcome != "candidate" or attempt.input_text != analysis.input_text
                or attempt.client_type != "human_ui" or attempt.agent_client_id != self._agent_client_id
                or candidate_set is None or candidate_set.thesis_analysis_id != analysis.id
                or candidate_set.snapshot_id != display.snapshot_id or member is None
                or member.candidate_set_id != candidate_set.id or member.market_id != assessment.market_id
                or capture is None or capture.market_id != assessment.market_id
                or capture.snapshot_id != display.snapshot_id or snapshot is None
                or assessment.thesis_analysis_id != analysis.id or assessment.candidate_set_id != candidate_set.id
                or assessment.fit_card_id != display.fit_card_id or assessment.snapshot_id != display.snapshot_id):
                raise RevisitConflict("activation evidence binding is invalid")
            if (sha256(capture.contract_terms_text.encode()).hexdigest() != capture.contract_terms_hash
                or sha256(capture.resolution_rules_text.encode()).hexdigest() != capture.resolution_rules_hash):
                raise RevisitConflict("stored rules hashes do not match the frozen text")
            source_choice_id, source_excerpt = self._source_binding(s, attempt)
            existing = s.scalar(select(ThesisRevision).where(ThesisRevision.thesis_analysis_id == analysis.id))
            if existing is not None:
                thesis, existing = self._owned_revision(s, existing.id)
                if existing.market_choice_id != choice.id:
                    raise RevisitConflict("analysis already has a different activation")
                return self._activation_out(s, thesis, existing)
            baseline = {
                "market_id": assessment.market_id,
                "accepted_thesis": analysis.normalized_claim_summary,
                "source_excerpt": source_excerpt,
                "title": capture.contract_terms_text,
                "rules": capture.resolution_rules_text,
                "title_hash": capture.contract_terms_hash,
                "rules_hash": capture.resolution_rules_hash,
                "deadline": None,
                "lifecycle": None,
                "condition_id": None,
                "snapshot_as_of": _utc(snapshot.as_of_ts),
                "source_kind": "frozen_market_snapshot",
            }
            pinned = {
                "actor": self._actor_id, "analysis": str(analysis.id),
                "decision": str(decision.id), "source_choice": str(source_choice_id),
                "display": str(display.id), "choice": str(choice.id),
                "assessment": str(assessment.id), "snapshot": display.snapshot_id,
                "rules_capture": str(capture.id), "baseline": baseline,
            }
            thesis = PersonalThesis(user_id=self._user_id)
            s.add(thesis)
            s.flush()
            revision = ThesisRevision(
                personal_thesis_id=thesis.id, thesis_analysis_id=analysis.id,
                normalization_decision_id=decision.id,
                source_candidate_choice_id=source_choice_id,
                market_display_set_id=display.id, market_choice_id=choice.id,
                market_assessment_id=assessment.id, market_id=assessment.market_id,
                snapshot_id=display.snapshot_id, rules_capture_id=capture.id,
                revision_digest=_digest(pinned), baseline=baseline,
                activated_by=self._actor_id,
            )
            s.add(revision)
            s.flush()
            thesis.active_revision_id = revision.id
            s.commit()
            return self._activation_out(s, thesis, revision)

    def register_capture(
        self, revision_id: uuid.UUID, *, source_kind: str, source_ref: str,
        observed_at: datetime, market_id: str, evidence: dict,
    ) -> uuid.UUID:
        """Trusted server/operator entry point. No HTTP raw-capture route exists."""
        if source_kind not in {"synthetic_fixture", "retained_capture"} or not source_ref:
            raise RevisitUnavailable("unsupported saved capture provenance")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise RevisitUnavailable("capture source timestamp must be timezone-aware")
        if evidence.get("market_id") != market_id:
            raise RevisitConflict("capture market identity is inconsistent")
        observed_utc = observed_at.astimezone(timezone.utc)
        digest = _digest({"source_kind": source_kind, "source_ref": source_ref, "observed_at": _utc(observed_utc), "evidence": evidence})
        with self._sessions() as s:
            self._owned_revision(s, revision_id)
            existing = s.scalar(select(RevisitCapture).where(
                RevisitCapture.revision_id == revision_id,
                RevisitCapture.capture_digest == digest,
            ))
            if existing:
                return existing.id
            row = RevisitCapture(
                revision_id=revision_id, market_id=market_id,
                source_kind=source_kind, source_ref=source_ref,
                observed_at=observed_utc, receipt_at=datetime.now(timezone.utc),
                capture_digest=digest, evidence=evidence,
            )
            s.add(row)
            s.commit()
            return row.id

    def _fixture_capture(self, revision: ThesisRevision, scenario: str) -> uuid.UUID:
        if self._mode != "fixture":
            raise RevisitUnavailable("fixture replay is available only in fixture mode")
        if scenario not in {"unchanged", "rules_added", "unavailable"}:
            raise RevisitUnavailable("unknown fixture scenario")
        baseline = revision.baseline
        evidence = {
            "market_id": revision.market_id,
            "title": baseline["title"] if scenario != "unavailable" else None,
            "rules": (baseline["rules"] + "\nSynthetic later rule: an added review clause.") if scenario == "rules_added" else baseline["rules"] if scenario == "unchanged" else None,
            "deadline": None,
            "lifecycle": None,
            "condition_id": None,
        }
        as_of = datetime.fromisoformat(baseline["snapshot_as_of"])
        return self.register_capture(
            revision.id, source_kind="synthetic_fixture",
            source_ref=f"fixture:{POLICY_VERSION}:{scenario}",
            observed_at=as_of + timedelta(days=1), market_id=revision.market_id,
            evidence=evidence,
        )

    def _comparison(self, revision: ThesisRevision, capture: RevisitCapture) -> dict:
        if (_digest({"source_kind": capture.source_kind, "source_ref": capture.source_ref,
                     "observed_at": _utc(capture.observed_at), "evidence": capture.evidence})
            != capture.capture_digest
            or capture.evidence.get("market_id") != capture.market_id):
            raise RevisitConflict("registered capture digest or identity is invalid")
        comparison = compare_market_evidence(
            revision.baseline, capture.evidence, market_id=revision.market_id,
            today=datetime.fromisoformat(_utc(capture.observed_at)).date(),
        )
        baseline_at = datetime.fromisoformat(revision.baseline["snapshot_as_of"])
        if datetime.fromisoformat(_utc(capture.observed_at)) <= baseline_at:
            comparison["triggers"] = []
            comparison["unchanged"] = []
            comparison["unknowns"] = sorted(set([*comparison["unknowns"], "capture_not_later_than_baseline"]))
            comparison["result"] = "incomplete"
        return comparison

    def _observation_out(
        self, s: Session, revision: ThesisRevision,
        observation: MonitoringObservation,
        request_capture_id: uuid.UUID | None = None,
    ) -> dict:
        capture = s.get(RevisitCapture, observation.capture_id)
        request_capture = s.get(RevisitCapture, request_capture_id) if request_capture_id else None
        packet = s.scalar(select(RevisitPacket).where(RevisitPacket.observation_id == observation.id))
        decision = s.scalar(select(HumanRevisitDecision).where(HumanRevisitDecision.packet_id == packet.id)) if packet else None
        if (observation.revision_id != revision.id or capture is None
            or capture.revision_id != revision.id or observation.policy_version != POLICY_VERSION
            or observation.comparison != self._comparison(revision, capture)
            or observation.result != observation.comparison["result"]
            or (request_capture_id is not None and (request_capture is None or request_capture.revision_id != revision.id))
            or (request_capture is not None and (
                request_capture.source_kind != capture.source_kind
                or self._comparison(revision, request_capture) != observation.comparison))
            or (packet is not None and (packet.revision_id != revision.id
                or packet.packet_digest != _digest({"revision": revision.revision_digest, "comparison": observation.comparison})))
            or (decision is not None and (decision.revision_id != revision.id
                or decision.observation_id != observation.id or decision.packet_id != packet.id
                or decision.user_id != self._user_id or decision.actor_id != self._actor_id
                or decision.decision_origin != "human_ui"
                or decision.packet_digest != packet.packet_digest))):
            raise RevisitConflict("revisit evidence binding is invalid")
        return {
            "binding": self._binding(revision),
            "observation": {
                "id": str(observation.id), "capture_id": str(capture.id),
                "capture_digest": capture.capture_digest,
                "result": observation.result,
                **observation.comparison,
                "source_kind": capture.source_kind,
                "source_ref": capture.source_ref,
                "observed_at": _utc(capture.observed_at),
                "receipt_at": _utc(capture.receipt_at),
            },
            "packet": {"id": str(packet.id), "digest": packet.packet_digest} if packet else None,
            "decision": self._decision_out(revision, decision) if decision else None,
            "request_capture": {
                "id": str(request_capture.id), "digest": request_capture.capture_digest,
                "source_kind": request_capture.source_kind,
                "observed_at": _utc(request_capture.observed_at),
            } if request_capture else None,
        }

    def check(
        self, revision_id: uuid.UUID, *, idempotency_key: str,
        fixture_scenario: str | None = None, capture_id: uuid.UUID | None = None,
    ) -> dict:
        if not idempotency_key or len(idempotency_key) > 160:
            raise RevisitUnavailable("idempotency key is required")
        if (fixture_scenario is None) == (capture_id is None):
            raise RevisitUnavailable("select exactly one registered capture or fixture scenario")
        with self._sessions() as s:
            thesis, revision = self._owned_revision(s, revision_id)
            if thesis.active_revision_id != revision.id:
                raise RevisitConflict("revision is no longer active")
            request_digest = _digest({"revision": str(revision.id), "fixture_scenario": fixture_scenario, "capture_id": str(capture_id) if capture_id else None})
            prior = s.scalar(select(RevisitRequest).where(
                RevisitRequest.user_id == self._user_id,
                RevisitRequest.revision_id == revision_id,
                RevisitRequest.idempotency_key == idempotency_key,
            ))
            if prior:
                if prior.request_digest != request_digest:
                    raise RevisitConflict("idempotency key has different pinned input")
                observation = s.get(MonitoringObservation, prior.observation_id)
                return self._observation_out(s, revision, observation, prior.capture_id)
            if fixture_scenario is not None:
                capture_id = self._fixture_capture(revision, fixture_scenario)
            capture = s.get(RevisitCapture, capture_id)
            if capture is None or capture.revision_id != revision.id:
                raise RevisitNotFound()
            comparison = self._comparison(revision, capture)
            observation = s.scalar(select(MonitoringObservation).where(
                MonitoringObservation.revision_id == revision.id,
                MonitoringObservation.capture_id == capture.id,
            ))
            if observation is None:
                # A repeated equivalent capture is retained, while its request
                # points at the first immutable comparison and any disposition.
                prior_observations = s.scalars(select(MonitoringObservation).where(
                    MonitoringObservation.revision_id == revision.id,
                    MonitoringObservation.policy_version == POLICY_VERSION,
                )).all()
                observation = next(
                    (row for row in prior_observations
                     if row.comparison == comparison
                     and (prior_capture := s.get(RevisitCapture, row.capture_id)) is not None
                     and prior_capture.revision_id == revision.id
                     and prior_capture.source_kind == capture.source_kind), None
                )
                if observation is None:
                    observation = MonitoringObservation(
                        revision_id=revision.id, capture_id=capture.id,
                        result=comparison["result"], comparison=comparison,
                        policy_version=POLICY_VERSION,
                    )
                    s.add(observation)
                    s.flush()
                    if observation.result == "revisit_proposed":
                        s.add(RevisitPacket(
                            revision_id=revision.id, observation_id=observation.id,
                            packet_digest=_digest({"revision": revision.revision_digest, "comparison": comparison}),
                        ))
            s.add(RevisitRequest(
                user_id=self._user_id, revision_id=revision.id,
                idempotency_key=idempotency_key, request_digest=request_digest,
                capture_id=capture.id,
                observation_id=observation.id,
            ))
            s.commit()
            return self._observation_out(s, revision, observation, capture.id)

    def read(self, revision_id: uuid.UUID) -> dict:
        with self._sessions() as s:
            thesis, revision = self._owned_revision(s, revision_id)
            latest = s.scalar(select(MonitoringObservation).where(
                MonitoringObservation.revision_id == revision.id,
            ).order_by(MonitoringObservation.created_at.desc(), MonitoringObservation.id.desc()))
            return {
                "activation": self._activation_out(s, thesis, revision),
                "latest": self._observation_out(s, revision, latest) if latest else None,
            }

    def _decision_out(self, revision: ThesisRevision, decision: HumanRevisitDecision) -> dict:
        return {
            "binding": self._binding(revision),
            "packet_id": str(decision.packet_id),
            "observation_id": str(decision.observation_id),
            "decision_id": str(decision.id),
            "action": decision.action,
            "decision_origin": decision.decision_origin,
            "created_at": _utc(decision.created_at),
        }

    def decide(
        self, packet_id: uuid.UUID, *, action: str,
        expected_revision_digest: str, expected_observation_id: uuid.UUID,
    ) -> dict:
        if action not in {"dismiss", "reaffirm"}:
            raise RevisitUnavailable("unsupported disposition")
        with self._sessions() as s:
            packet = s.get(RevisitPacket, packet_id)
            if packet is None:
                raise RevisitNotFound()
            thesis, revision = self._owned_revision(s, packet.revision_id)
            observation = s.get(MonitoringObservation, packet.observation_id)
            if observation is None:
                raise RevisitConflict("packet observation is missing")
            self._observation_out(s, revision, observation)
            if (thesis.active_revision_id != revision.id
                or revision.revision_digest != expected_revision_digest
                or packet.observation_id != expected_observation_id):
                raise RevisitConflict("stale or mismatched revisit binding")
            latest_packet = s.scalar(select(RevisitPacket).where(
                RevisitPacket.revision_id == revision.id,
            ).order_by(RevisitPacket.created_at.desc(), RevisitPacket.id.desc()))
            if latest_packet is None or latest_packet.id != packet.id:
                raise RevisitConflict("packet is no longer the latest proposal")
            existing = s.scalar(select(HumanRevisitDecision).where(HumanRevisitDecision.packet_id == packet.id))
            if existing:
                if existing.action != action or existing.actor_id != self._actor_id:
                    raise RevisitConflict("packet already has a different disposition")
                return self._decision_out(revision, existing)
            decision = HumanRevisitDecision(
                packet_id=packet.id, revision_id=revision.id,
                observation_id=packet.observation_id, user_id=self._user_id,
                actor_id=self._actor_id, action=action,
                decision_origin="human_ui", packet_digest=packet.packet_digest,
            )
            s.add(decision)
            s.commit()
            return self._decision_out(revision, decision)
