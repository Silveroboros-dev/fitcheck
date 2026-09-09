"""Transport-neutral v3 market-pool application service.

The existing retrieval and fit services remain the computation engines. This
service materializes their per-market evidence as first-class assessments,
then applies a separately versioned display policy and records human choice.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.tables import (
    CandidateSetMember,
    FitCard,
    MarketAssessment,
    MarketChoice,
    MarketDisplayItem,
    MarketDisplaySet,
    MarketRulesCapture,
    NormalizationAttempt,
    NormalizationDecision,
)
from el.domain.vocabulary import vocabulary_violations
from el.fitgate.service import FitService
from el.retrieval.service import RetrievalService

DISPLAY_POLICY_VERSION = "fit-class-then-retrieval-v1"

_PAIR_CLASS_RANK = {
    "direct": 3,
    "indirect": 2,
    "weak_proxy": 1,
    "not_an_expression": 0,
}
_LEGACY_TO_PAIR_CLASS = {
    "direct": "direct",
    "indirect": "indirect",
    "weak_proxy": "weak_proxy",
    "no_clean_expression": "not_an_expression",
}


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MarketPoolBuildOutcome(_Out):
    market_display_set_id: uuid.UUID
    fit_card_id: uuid.UUID
    thesis_analysis_id: uuid.UUID
    candidate_set_id: uuid.UUID
    snapshot_id: str
    display_policy_version: str
    assessed_count: int
    target_count: int
    displayed_count: int
    assessment_complete: bool
    system_pool_outcome: str
    incomplete_reasons: list[str]


class MarketChoiceOutcome(_Out):
    market_choice_id: uuid.UUID
    market_display_set_id: uuid.UUID
    selection_kind: str
    market_assessment_id: uuid.UUID | None
    created_at: datetime


class MarketPoolNotFound(Exception):
    pass


class MarketPoolConflict(Exception):
    pass


def pair_class(legacy_class: str) -> str:
    try:
        return _LEGACY_TO_PAIR_CLASS[legacy_class]
    except KeyError as exc:
        raise MarketPoolConflict(
            f"unsupported legacy pair class: {legacy_class}"
        ) from exc


def project_pool_outcome(
    pair_classes: list[str], *, target_count: int
) -> tuple[bool, str, list[str]]:
    """Pure completeness/no-clean rule used by runtime and unit tests."""

    reasons: list[str] = []
    if target_count == 0:
        reasons.append("no_eligible_candidates")
    if len(pair_classes) < target_count:
        reasons.append("assessment_target_not_filled")
    complete = target_count > 0 and len(pair_classes) == target_count
    if not complete:
        return False, "incomplete", reasons
    if any(value in {"direct", "indirect"} for value in pair_classes):
        return True, "candidate_expressions", []
    return True, "no_clean_expression", []


def _assessment_text(payload: dict) -> tuple[str, str]:
    checks = payload.get("checks") or []
    passed = [
        check.get("name") or check["check_id"]
        for check in checks
        if check.get("status") == "pass"
    ]
    mismatches = [
        check.get("detail")
        for check in checks
        if check.get("cap") is not None and check.get("detail")
    ]
    captures = (
        "Matched conditions: " + ", ".join(passed) + "."
        if passed
        else "No checked condition established a clean match."
    )
    misses = (
        "; ".join(mismatches)
        if mismatches
        else (
            "No deterministic mismatch fired; semantic correctness still "
            "requires human review."
        )
    )
    for value in (captures, misses):
        violations = vocabulary_violations(value)
        if violations:
            raise MarketPoolConflict(
                "market assessment text violated product vocabulary"
            )
    return captures, misses


class MarketPoolService:
    def __init__(
        self,
        retrieval: RetrievalService,
        fit: FitService,
        session_factory: sessionmaker[Session],
    ):
        self._retrieval = retrieval
        self._fit = fit
        self._sessions = session_factory

    def _require_confirmed_thesis(
        self,
        session: Session,
        thesis_analysis_id: uuid.UUID,
        *,
        client_type: str,
        agent_client_id: str,
    ) -> None:
        decision = session.scalar(
            select(NormalizationDecision)
            .join(
                NormalizationAttempt,
                NormalizationAttempt.id == NormalizationDecision.attempt_id,
            )
            .where(
                NormalizationDecision.thesis_analysis_id == thesis_analysis_id,
                NormalizationDecision.action == "accept",
                NormalizationAttempt.client_type == client_type,
                NormalizationAttempt.agent_client_id == agent_client_id,
            )
        )
        if decision is None:
            raise MarketPoolNotFound()

    def build(
        self,
        thesis_analysis_id: uuid.UUID,
        *,
        client_type: str,
        agent_client_id: str,
    ) -> MarketPoolBuildOutcome:
        with self._sessions() as session:
            self._require_confirmed_thesis(
                session,
                thesis_analysis_id,
                client_type=client_type,
                agent_client_id=agent_client_id,
            )

        retrieval = self._retrieval.retrieve_candidates(thesis_analysis_id)
        fit = self._fit.classify_fit(
            thesis_analysis_id, retrieval.candidate_set_id
        )

        with self._sessions() as session:
            card = session.get(FitCard, fit.fit_card_id)
            if card is None:
                raise MarketPoolConflict("fit card disappeared before projection")
            if (
                card.thesis_analysis_id != thesis_analysis_id
                or card.candidate_set_id != retrieval.candidate_set_id
            ):
                raise MarketPoolConflict("fit card identity does not match retrieval")
            per_market = (card.provenance or {}).get("per_market")
            if not isinstance(per_market, dict):
                raise MarketPoolConflict("fit card has no per-market evidence")

            members = session.scalars(
                select(CandidateSetMember).where(
                    CandidateSetMember.candidate_set_id
                    == retrieval.candidate_set_id
                )
            ).all()
            members_by_market = {member.market_id: member for member in members}
            assessments: list[MarketAssessment] = []
            for market_id, payload in per_market.items():
                member = members_by_market.get(market_id)
                if member is None:
                    raise MarketPoolConflict(
                        "per-market evidence is not bound to this candidate set"
                    )
                capture = session.scalar(
                    select(MarketRulesCapture).where(
                        MarketRulesCapture.market_id == market_id,
                        MarketRulesCapture.snapshot_id == retrieval.snapshot_id,
                    )
                )
                if capture is None:
                    raise MarketPoolConflict(
                        "display candidate has no frozen rules capture"
                    )
                captures, misses = _assessment_text(payload)
                advisory = payload.get("advisory") or {}
                assessment = MarketAssessment(
                    fit_card_id=card.id,
                    thesis_analysis_id=thesis_analysis_id,
                    candidate_set_id=retrieval.candidate_set_id,
                    candidate_set_member_id=member.id,
                    rules_capture_id=capture.id,
                    snapshot_id=retrieval.snapshot_id,
                    market_id=market_id,
                    retrieval_rank=member.rank,
                    pair_class=pair_class(payload["published"]),
                    what_it_captures=captures,
                    what_it_misses=misses,
                    horizon_match=payload.get("horizon_match"),
                    resolution_risk=payload.get("resolution_risk"),
                    authority=payload["authority"],
                    fit_confidence=(
                        advisory.get("confidence")
                        if advisory.get("status") == "accepted"
                        else None
                    ),
                    provenance={
                        "fit_gate_policy_version": card.provenance.get(
                            "gate_policy_version"
                        ),
                        "fit_trace_id": card.provenance.get("trace_id"),
                        "per_market": payload,
                    },
                )
                session.add(assessment)
                assessments.append(assessment)
            session.flush()

            ordered = sorted(
                assessments,
                key=lambda row: (
                    -_PAIR_CLASS_RANK[row.pair_class],
                    row.retrieval_rank,
                    row.market_id,
                ),
            )
            target_count = min(3, fit.eligible_count)
            displayed = ordered[:target_count]
            complete, outcome, reasons = project_pool_outcome(
                [row.pair_class for row in displayed],
                target_count=target_count,
            )
            display_set = MarketDisplaySet(
                fit_card_id=card.id,
                thesis_analysis_id=thesis_analysis_id,
                candidate_set_id=retrieval.candidate_set_id,
                snapshot_id=retrieval.snapshot_id,
                display_policy_version=DISPLAY_POLICY_VERSION,
                assessed_count=len(assessments),
                target_count=target_count,
                displayed_count=len(displayed),
                assessment_complete=complete,
                system_pool_outcome=outcome,
                incomplete_reasons=reasons,
            )
            session.add(display_set)
            session.flush()
            for display_rank, assessment in enumerate(displayed, start=1):
                session.add(
                    MarketDisplayItem(
                        market_display_set_id=display_set.id,
                        market_assessment_id=assessment.id,
                        display_rank=display_rank,
                    )
                )
            session.commit()
            return MarketPoolBuildOutcome(
                market_display_set_id=display_set.id,
                fit_card_id=card.id,
                thesis_analysis_id=thesis_analysis_id,
                candidate_set_id=retrieval.candidate_set_id,
                snapshot_id=retrieval.snapshot_id,
                display_policy_version=DISPLAY_POLICY_VERSION,
                assessed_count=len(assessments),
                target_count=target_count,
                displayed_count=len(displayed),
                assessment_complete=complete,
                system_pool_outcome=outcome,
                incomplete_reasons=reasons,
            )

    def _owned_display_set(
        self,
        session: Session,
        display_set_id: uuid.UUID,
        *,
        client_type: str,
        agent_client_id: str,
    ) -> MarketDisplaySet:
        display_set = session.get(MarketDisplaySet, display_set_id)
        if display_set is None:
            raise MarketPoolNotFound()
        self._require_confirmed_thesis(
            session,
            display_set.thesis_analysis_id,
            client_type=client_type,
            agent_client_id=agent_client_id,
        )
        return display_set

    def require_displayed_assessment(
        self,
        assessment_id: uuid.UUID,
        *,
        client_type: str,
        agent_client_id: str,
    ) -> MarketAssessment:
        with self._sessions() as session:
            item = session.scalar(
                select(MarketDisplayItem).where(
                    MarketDisplayItem.market_assessment_id == assessment_id
                )
            )
            if item is None:
                raise MarketPoolNotFound()
            self._owned_display_set(
                session,
                item.market_display_set_id,
                client_type=client_type,
                agent_client_id=agent_client_id,
            )
            assessment = session.get(MarketAssessment, assessment_id)
            if assessment is None:
                raise MarketPoolNotFound()
            return assessment

    def choose(
        self,
        display_set_id: uuid.UUID,
        *,
        selection_kind: str,
        market_assessment_id: uuid.UUID | None,
        actor_id: str,
        client_type: str,
        agent_client_id: str,
        reason: str | None = None,
    ) -> MarketChoiceOutcome:
        if selection_kind not in {"market", "none"}:
            raise ValueError("selection_kind must be market or none")
        if (selection_kind == "market") != (market_assessment_id is not None):
            raise ValueError(
                "market selection requires an assessment; none forbids one"
            )
        with self._sessions() as session:
            display_set = self._owned_display_set(
                session,
                display_set_id,
                client_type=client_type,
                agent_client_id=agent_client_id,
            )
            existing = session.scalar(
                select(MarketChoice).where(
                    MarketChoice.market_display_set_id == display_set.id
                )
            )
            if existing is not None:
                if (
                    existing.selection_kind == selection_kind
                    and existing.market_assessment_id == market_assessment_id
                    and existing.reason == reason
                ):
                    return self._choice_out(existing)
                raise MarketPoolConflict(
                    "market display set already has a different human choice"
                )
            if market_assessment_id is not None:
                displayed = session.scalar(
                    select(MarketDisplayItem.id).where(
                        MarketDisplayItem.market_display_set_id == display_set.id,
                        MarketDisplayItem.market_assessment_id
                        == market_assessment_id,
                    )
                )
                if displayed is None:
                    raise MarketPoolConflict(
                        "selected assessment is not in this displayed pool"
                    )
            row = MarketChoice(
                market_display_set_id=display_set.id,
                selection_kind=selection_kind,
                market_assessment_id=market_assessment_id,
                actor_id=actor_id,
                client_type=client_type,
                reason=reason,
            )
            session.add(row)
            session.commit()
            return self._choice_out(row)

    @staticmethod
    def _choice_out(row: MarketChoice) -> MarketChoiceOutcome:
        return MarketChoiceOutcome(
            market_choice_id=row.id,
            market_display_set_id=row.market_display_set_id,
            selection_kind=row.selection_kind,
            market_assessment_id=row.market_assessment_id,
            created_at=row.created_at,
        )
