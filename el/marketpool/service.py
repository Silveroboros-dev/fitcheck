"""Transport-neutral v3 market-pool application service.

The existing retrieval and fit services remain the computation engines. This
service materializes their per-market evidence as first-class assessments,
then applies a separately versioned display policy and records human choice.
"""

import uuid
from datetime import date, datetime
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field
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
    MarketStructureRow,
    NormalizationAttempt,
    NormalizationDecision,
    ThesisAnalysis,
)
from el.domain.structures import ExtractedStructure, MarketStructure
from el.domain.vocabulary import vocabulary_violations
from el.fitgate.service import FitService
from el.models.market_adapter import MARKET_EXTRACTION_POLICY_VERSION
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


_STAGE_CONDITIONS = {
    "announced": "an announcement",
    "launched": "a launch",
    "shipped": "a shipment",
    "adopted": "adoption",
    "measured": "a measured result",
    "resolved": "a resolved outcome",
}


def _raw_subject_names(
    structure: ExtractedStructure | MarketStructure,
) -> tuple[str, ...]:
    """Compare recorded subject identities before display-safe redaction."""

    return tuple(
        entity.name.strip()
        for entity in structure.entities
        if entity.role == "subject" and entity.name.strip()
    )


def _subject_names(structure: ExtractedStructure | MarketStructure) -> str:
    names = _raw_subject_names(structure)
    if not names or any(vocabulary_violations(name) for name in names):
        return "the named subject"
    return ", ".join(names)


def _same_subjects(claim: ExtractedStructure, market: MarketStructure) -> bool:
    claim_names = _raw_subject_names(claim)
    market_names = _raw_subject_names(market)
    return bool(claim_names) and tuple(
        name.casefold() for name in claim_names
    ) == tuple(name.casefold() for name in market_names)


def _safe_fact(value: str | None, fallback: str) -> str:
    candidate = (value or "").strip()
    return candidate if candidate and not vocabulary_violations(candidate) else fallback


def _human_date(value: date) -> str:
    return value.strftime("%B %d, %Y").replace(" 0", " ")


def accepted_thesis_wording(thesis: ThesisAnalysis) -> str:
    """Prefer the full accepted criterion while retaining the copy guard."""
    structure = thesis.extracted_structure
    wording = structure.get("contractible_version") if isinstance(structure, dict) else None
    return _safe_fact(
        wording if isinstance(wording, str) else None,
        thesis.normalized_claim_summary,
    )


RELATEDNESS_PROJECTION_VERSION = "source-bound-relatedness-v1"
RELATEDNESS_PROPOSAL_AUTHORITY = "source_bound_relation_proposal"


class RelatedPairProposal(_Out):
    """Attributable semantic evidence, never a human adjudication or golden."""

    proposal_id: uuid.UUID
    proposer_id: str = Field(min_length=1)
    thesis_analysis_id: uuid.UUID
    accepted_wording_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    rules_capture_id: uuid.UUID
    rules_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_quote: str = Field(min_length=1)
    rules_quote: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


def _validate_related_pair(
    proposal: RelatedPairProposal,
    assessment: MarketAssessment,
    thesis: ThesisAnalysis,
    capture: MarketRulesCapture,
) -> None:
    wording = accepted_thesis_wording(thesis)
    rules = capture.resolution_rules_text
    if (
        proposal.thesis_analysis_id != thesis.id
        or assessment.thesis_analysis_id != thesis.id
        or proposal.market_id != assessment.market_id
        or proposal.market_id != capture.market_id
        or proposal.snapshot_id != assessment.snapshot_id
        or proposal.snapshot_id != capture.snapshot_id
        or proposal.rules_capture_id != capture.id
        or assessment.rules_capture_id != capture.id
        or proposal.accepted_wording_sha256 != sha256(wording.encode()).hexdigest()
        or proposal.rules_sha256 != sha256(rules.encode()).hexdigest()
        or not proposal.claim_quote.strip()
        or proposal.claim_quote not in wording
        or not proposal.rules_quote.strip()
        or proposal.rules_quote not in rules
        or not proposal.proposer_id.strip()
        or not proposal.rationale.strip()
        or vocabulary_violations(proposal.rationale)
    ):
        raise MarketPoolConflict("relatedness proposal is not bound to this thesis/rules pair")


def _stacking_only(payload: dict) -> bool:
    """Separate rejection of clean fit from a semantic claim of unrelatedness."""
    checks = payload.get("checks") or []
    hard = sum(check.get("hard") is True for check in checks)
    return (
        payload.get("authority") == "deterministic_only"
        and payload.get("ceiling") == "no_clean_expression"
        and payload.get("published") == "no_clean_expression"
        and (payload.get("advisory") or {}).get("status") == "not_configured"
        and hard >= 2
        and hard == payload.get("hard_fails")
        and all(check.get("cap") in {None, "direct", "indirect", "weak_proxy"}
                for check in checks)
        and any(check.get("cap") == "weak_proxy" for check in checks)
    )


def relatedness_projection(proposals: list[RelatedPairProposal]):
    """Constructor-bound successor projection; no global lexical shortcuts."""
    by_pair = {}
    for proposal in proposals:
        key = (proposal.thesis_analysis_id, proposal.market_id)
        if key in by_pair:
            raise MarketPoolConflict("duplicate relatedness proposal target")
        by_pair[key] = proposal

    def project(assessment, thesis, capture):
        proposal = by_pair.get((thesis.id, assessment.market_id))
        if proposal is None:
            return assessment
        _validate_related_pair(proposal, assessment, thesis, capture)
        payload = (assessment.provenance or {}).get("per_market") or {}
        if (
            assessment.pair_class != "not_an_expression"
            or assessment.authority != "deterministic_only"
            or not _stacking_only(payload)
        ):
            return assessment
        basis = {
            "schema_version": RELATEDNESS_PROJECTION_VERSION,
            "predecessor_pair_class": assessment.pair_class,
            "authority": RELATEDNESS_PROPOSAL_AUTHORITY,
            "proposal": proposal.model_dump(mode="json"),
        }
        assessment.pair_class = "weak_proxy"
        assessment.authority = RELATEDNESS_PROPOSAL_AUTHORITY
        assessment.fit_confidence = None
        assessment.provenance = {
            **assessment.provenance,
            "pair_projection_policy_version": RELATEDNESS_PROJECTION_VERSION,
            "relatedness_basis": basis,
        }
        return assessment

    return project


def relatedness_evidence(assessment, thesis, capture) -> dict | None:
    """Validate retained proposal bindings on both HTTP and MCP read paths."""
    provenance = assessment.provenance or {}
    basis = provenance.get("relatedness_basis")
    if basis is None:
        if (assessment.authority == RELATEDNESS_PROPOSAL_AUTHORITY
                or provenance.get("pair_projection_policy_version") == RELATEDNESS_PROJECTION_VERSION):
            raise MarketPoolConflict("relatedness evidence is missing")
        return None
    if (
        not isinstance(basis, dict)
        or set(basis) != {"schema_version", "predecessor_pair_class", "authority", "proposal"}
        or basis["schema_version"] != RELATEDNESS_PROJECTION_VERSION
        or provenance.get("pair_projection_policy_version") != RELATEDNESS_PROJECTION_VERSION
        or basis["predecessor_pair_class"] != "not_an_expression"
        or basis["authority"] != RELATEDNESS_PROPOSAL_AUTHORITY
        or assessment.authority != RELATEDNESS_PROPOSAL_AUTHORITY
        or assessment.pair_class != "weak_proxy"
        or not _stacking_only(provenance.get("per_market") or {})
    ):
        raise MarketPoolConflict("relatedness projection provenance mismatch")
    try:
        proposal = RelatedPairProposal.model_validate(basis["proposal"])
    except ValueError as exc:
        raise MarketPoolConflict("invalid relatedness proposal") from exc
    _validate_related_pair(proposal, assessment, thesis, capture)
    return basis


def _structured_condition(structure: ExtractedStructure | MarketStructure) -> str:
    horizon = (
        structure.horizon.window_end
        if isinstance(structure, ExtractedStructure)
        else structure.horizon.resolution_date
    )
    return (
        f"{_STAGE_CONDITIONS[structure.event_stage.value]} for "
        f"{_safe_fact(structure.metric.what, 'the stated outcome')} "
        f"for {_subject_names(structure)}, by {_human_date(horizon)}"
    )


def _contract_condition(market: MarketStructure) -> str:
    details = [f"This contract tests {_structured_condition(market)}."]
    if market.threshold is not None:
        details.append(
            f"It requires {_safe_fact(market.threshold, 'a stated value')}."
        )
    if market.direction is not None:
        details.append(
            "It is about "
            f"{_safe_fact(market.direction, 'a stated direction')} outcomes."
        )
    return " ".join(details)


def _accepted_thesis_text(claim: ExtractedStructure) -> str:
    accepted = claim.contractible_version.strip() or claim.claim_summary.strip()
    if accepted and not vocabulary_violations(accepted):
        return f"Your accepted thesis: {accepted}"
    return (
        "Your accepted thesis is shown above. Its wording cannot be repeated "
        "here, so this comparison uses only the available details."
    )


def _side_text(payload: dict) -> str:
    if payload.get("published") in {
        "weak_proxy", "no_clean_expression", "not_an_expression"
    }:
        return "This contract's outcome does not settle your thesis."
    if payload.get("published") not in {"direct", "indirect"}:
        return (
            "The recorded fit result does not establish which contract outcome "
            "corresponds to your thesis."
        )
    side = payload.get("thesis_side")
    if side == "yes":
        return "The recorded fit result maps this contract to the YES outcome for your thesis."
    if side == "no":
        return "The recorded fit result maps this contract to the NO outcome for your thesis."
    return (
        "The recorded fit result does not establish which contract outcome "
        "corresponds to your thesis."
    )


def _assessment_text(
    claim: ExtractedStructure | None,
    market: MarketStructure | None,
    payload: dict,
) -> tuple[str, str]:
    """Explain an already classified pair from its frozen structures."""

    if market is None:
        captures = (
            "This contract's displayed resolution terms are available, but its "
            "condition details are unavailable."
        )
        misses = (
            "Its stage, metric, or date evidence is unavailable, so FitCheck "
            "cannot state a precise comparison with your thesis."
        )
    elif claim is None:
        captures = _contract_condition(market)
        misses = (
            "FitCheck does not have enough accepted-thesis detail to make a "
            "precise comparison with this contract."
        )
    else:
        captures = _contract_condition(market)
        differences: list[str] = [_accepted_thesis_text(claim)]
        cautions: list[str] = []
        same_subjects = _same_subjects(claim, market)
        if not same_subjects:
            market_subjects = _subject_names(market)
            claim_subjects = _subject_names(claim)
            if (
                market_subjects != "the named subject"
                and claim_subjects != "the named subject"
            ):
                differences.append(
                    f"It names {market_subjects}; your thesis names {claim_subjects}."
                )
            else:
                differences.append(
                    "The recorded subject wording differs; a subject match has "
                    "not been established."
                )
        stage_differs = market.event_stage != claim.event_stage
        if stage_differs:
            differences.append(
                "It resolves "
                f"{_STAGE_CONDITIONS[market.event_stage.value]}; compare that "
                "condition with the complete accepted thesis above."
            )
        market_metric = _safe_fact(market.metric.what, "the stated outcome")
        metric_differs = market.metric.what.casefold() != claim.metric.what.casefold()
        if metric_differs:
            differences.append(
                f"It asks about {market_metric}. The complete accepted thesis "
                "above defines the outcome you want to test."
            )
        horizon_differs = market.horizon.resolution_date != claim.horizon.window_end
        if horizon_differs:
            differences.append(
                f"It resolves by {_human_date(market.horizon.resolution_date)}; "
                f"your thesis is due {_human_date(claim.horizon.window_end)}."
            )
        if market.threshold is not None:
            cautions.append(
                "A matching threshold in your thesis has not been established."
            )
        if market.direction is not None:
            cautions.append(
                "A matching direction in your thesis has not been established."
            )
        if claim.mechanism.is_composite or claim.mechanism.asserted_causal_chain:
            cautions.append(
                "Your thesis includes a causal or composite condition; the "
                "available contract structure does not establish how it is covered."
            )
        capped_checks = {
            check.get("check_id")
            for check in payload.get("checks") or []
            if check.get("cap") is not None
        }
        metric_identity_uncertain = any(
            check.get("check_id") == "M1"
            and check.get("status") in {"inconclusive", "unknown", "fail"}
            for check in payload.get("checks") or []
        )
        if "E1" in capped_checks and same_subjects:
            cautions.append(
                "The available evidence does not establish that the named subjects "
                "are equivalent."
            )
        if ("M1" in capped_checks or metric_identity_uncertain) and not metric_differs:
            cautions.append(
                "The available evidence does not establish that the contract outcome "
                "measures the same thing as your thesis."
            )
        if "S1" in capped_checks and not stage_differs:
            cautions.append(
                "The available evidence does not establish that the contract and "
                "thesis use the same event stage."
            )
        if "H1" in capped_checks and not horizon_differs:
            cautions.append(
                "The available evidence does not establish that the contract date "
                "answers the same time window as your thesis."
            )
        if "M2" in capped_checks or "M3" in capped_checks:
            cautions.append(
                "The available evidence does not establish that both outcomes have "
                "the same level of objective measurement."
            )
        if "X1" in capped_checks or "X2" in capped_checks:
            cautions.append(
                "The available evidence does not establish that one contract tests "
                "every condition in your thesis."
            )
        if stage_differs and metric_differs:
            differences.append(
                "The recorded contract condition does not establish all "
                "conditions of the accepted thesis."
            )
        misses = " ".join([*differences, *cautions, _side_text(payload)])
    for value in (captures, misses):
        if vocabulary_violations(value):
            raise MarketPoolConflict(
                "market assessment text violated product vocabulary"
            )
    return captures, misses


def _claim_structure(thesis: ThesisAnalysis) -> ExtractedStructure | None:
    try:
        return ExtractedStructure.model_validate(thesis.extracted_structure)
    except ValueError:
        return None


def _market_structure(
    session: Session, capture: MarketRulesCapture
) -> MarketStructure | None:
    row = session.scalar(
        select(MarketStructureRow)
        .where(
            MarketStructureRow.market_id == capture.market_id,
            MarketStructureRow.contract_terms_hash == capture.contract_terms_hash,
            MarketStructureRow.resolution_rules_hash == capture.resolution_rules_hash,
            MarketStructureRow.schema_version == 1,
            MarketStructureRow.extraction_policy_version
            == MARKET_EXTRACTION_POLICY_VERSION,
        )
        .order_by(MarketStructureRow.created_at.desc())
    )
    if row is None:
        return None
    try:
        return MarketStructure.model_validate(row.structure)
    except ValueError:
        return None


class MarketPoolService:
    def __init__(
        self,
        retrieval: RetrievalService,
        fit: FitService,
        session_factory: sessionmaker[Session],
        *,
        assessment_projection=None,
    ):
        self._assessment_projection = assessment_projection
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
            thesis = session.get(ThesisAnalysis, thesis_analysis_id)
            if thesis is None:
                raise MarketPoolConflict("accepted thesis disappeared before projection")
            claim = _claim_structure(thesis)
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
                captures, misses = _assessment_text(
                    claim, _market_structure(session, capture), payload
                )
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
                # Optional constructor-bound local projection; ordinary fit
                # evidence is retained before the assessment is persisted.
                if self._assessment_projection is not None:
                    assessment = self._assessment_projection(assessment, thesis, capture)
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
