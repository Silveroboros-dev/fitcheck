"""Additive Product Discovery v3.1 MCP adapter.

This module performs transport projection only. Source interpretation,
normalization, confirmation, market-pool assessment, ownership, staleness,
idempotency, and choice semantics remain in their existing application
services. MCP authentication establishes an agent principal; calls that relay
user decisions are labelled ``agent_relay`` and never direct human
attestation.
"""

from __future__ import annotations

import uuid
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.contracts import ThesisAnalysisIn
from el.domain.tables import (
    CandidateSet,
    MarketAssessment,
    MarketDisplayItem,
    MarketDisplaySet,
    MarketRulesCapture,
    MarketSnapshot,
    ThesisAnalysis,
)
from el.extraction.service import (
    NormalizationAttemptConflict,
    NormalizationAttemptNotFound,
)
from el.jobs import IdempotencyConflict, JobNotFound
from el.marketpool.service import (
    MarketPoolConflict,
    MarketPoolNotFound,
    project_pool_outcome,
)
from el.mcp.auth import Principal
from el.mcp.contracts import (
    Conflict,
    IdempotencyConflictError,
    NotFound,
    Unavailable,
    V3MarketAssessmentResult,
    V3MarketChoiceResult,
    V3MarketPoolResult,
    V3NormalizationAttemptResult,
    V3NormalizationDecisionResult,
    V3SourceCandidateChoiceResult,
    V3SourceInterpretationJobResult,
    V3SourceInterpretationResult,
    V3SourceThesisCandidateResult,
)
from el.mcp.vocab_guard import assert_a7_clean
from el.retrieval.scope import persisted_candidate_set_scope
from el.sourceinterpretation.service import (
    SourceCandidateChoiceConflict,
    SourceInterpretationNotFound,
)


def _utc(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class McpV3Tools:
    """Thin v3 composition over existing transport-neutral services."""

    def __init__(
        self,
        *,
        source_interpretation,
        source_interpretation_jobs,
        extraction,
        market_pool,
        session_factory,
    ):
        self._source = source_interpretation
        self._source_jobs = source_interpretation_jobs
        self._extraction = extraction
        self._market_pool = market_pool
        self._sessions: sessionmaker[Session] = session_factory

    def _guard(self, model):
        assert_a7_clean(
            model.model_dump(mode="json"), source_paths=model.SOURCE_PATHS
        )
        return model

    @staticmethod
    def _source_out(outcome) -> V3SourceInterpretationResult:
        return V3SourceInterpretationResult(
            source_interpretation_id=outcome.source_interpretation_id,
            outcome=outcome.outcome,
            input_digest=outcome.input_digest,
            reasons=outcome.reasons,
            prompt_policy_version=outcome.prompt_policy_version,
            system_variant_id=outcome.system_variant_id,
            model_adapter=outcome.model_adapter,
            model_run_id=outcome.model_run_id,
            candidates=[
                V3SourceThesisCandidateResult(
                    source_thesis_candidate_id=(
                        candidate.source_thesis_candidate_id
                    ),
                    ordinal=candidate.ordinal,
                    selected_source_quote=candidate.selected_source_quote,
                    source_quote_digest=candidate.source_quote_digest,
                    claim_summary=candidate.claim_summary,
                )
                for candidate in outcome.candidates
            ],
            created_at=_utc(outcome.created_at),
        )

    def _job_out(self, status, *, created: bool | None):
        status_interpretation = getattr(status, "interpretation", None)
        interpretation = (
            self._source_out(status_interpretation)
            if status_interpretation is not None
            else None
        )
        return self._guard(
            V3SourceInterpretationJobResult(
                job_id=status.job_id,
                source_interpretation_request_id=(
                    status.source_interpretation_request_id
                ),
                input_digest=status.input_digest,
                status=status.status,
                stage=status.stage,
                created=created,
                error_code=getattr(status, "error_code", None),
                safe_error_message=getattr(status, "safe_error_message", None),
                interpretation=interpretation,
                created_at=_utc(status.created_at),
                updated_at=_utc(status.updated_at),
            )
        )

    def submit_source_interpretation(
        self,
        principal: Principal,
        *,
        input_text: str,
        source_url: str | None,
        idempotency_key: str,
    ) -> V3SourceInterpretationJobResult:
        try:
            status = self._source_jobs.submit(
                input_text,
                source_url=source_url,
                idempotency_key=idempotency_key,
                owner_client_type=principal.client_type.value,
                owner_actor_id=principal.actor_id,
                agent_client_id=principal.agent_client_id,
                owner_user_id=principal.user_id,
            )
        except IdempotencyConflict as exc:
            raise IdempotencyConflictError(str(exc)) from exc
        return self._job_out(status, created=status.created)

    def get_source_interpretation_job(
        self, principal: Principal, *, job_id: uuid.UUID
    ) -> V3SourceInterpretationJobResult:
        try:
            status = self._source_jobs.get_owned(
                job_id,
                owner_client_type=principal.client_type.value,
                owner_actor_id=principal.actor_id,
                agent_client_id=principal.agent_client_id,
            )
        except (JobNotFound, SourceInterpretationNotFound) as exc:
            raise NotFound("source interpretation job not found") from exc
        return self._job_out(status, created=None)

    def get_source_interpretation_job_by_idempotency(
        self, principal: Principal, *, idempotency_key: str
    ) -> V3SourceInterpretationJobResult:
        try:
            status = self._source_jobs.get_by_idempotency_owned(
                idempotency_key,
                owner_client_type=principal.client_type.value,
                owner_actor_id=principal.actor_id,
                agent_client_id=principal.agent_client_id,
            )
        except (JobNotFound, SourceInterpretationNotFound) as exc:
            raise NotFound("source interpretation job not found") from exc
        return self._job_out(status, created=None)

    def choose_source_candidate(
        self,
        principal: Principal,
        *,
        source_interpretation_id: uuid.UUID,
        selection_kind: str,
        source_thesis_candidate_id: uuid.UUID | None,
        reason: str | None = None,
    ) -> V3SourceCandidateChoiceResult:
        try:
            outcome = self._source.choose(
                source_interpretation_id,
                selection_kind=selection_kind,
                source_thesis_candidate_id=source_thesis_candidate_id,
                actor_id=principal.actor_id,
                client_type=principal.client_type.value,
                agent_client_id=principal.agent_client_id,
                reason=reason,
            )
        except SourceInterpretationNotFound as exc:
            raise NotFound("source interpretation not found") from exc
        except SourceCandidateChoiceConflict as exc:
            raise Conflict(
                "source candidate choice conflicts with recorded state"
            ) from exc
        return self._guard(
            V3SourceCandidateChoiceResult(
                source_candidate_choice_id=outcome.source_candidate_choice_id,
                source_interpretation_id=outcome.source_interpretation_id,
                selection_kind=outcome.selection_kind,
                source_thesis_candidate_id=(
                    outcome.source_thesis_candidate_id
                ),
                created_at=_utc(outcome.created_at),
            )
        )

    @staticmethod
    def _attempt_out(outcome) -> V3NormalizationAttemptResult:
        return V3NormalizationAttemptResult(
            normalization_attempt_id=outcome.attempt_id,
            predecessor_attempt_id=outcome.predecessor_attempt_id,
            source_interpretation_id=outcome.source_interpretation_id,
            source_thesis_candidate_id=outcome.source_thesis_candidate_id,
            outcome=outcome.outcome,
            verdict=outcome.verdict,
            input_digest=outcome.input_digest,
            normalized_claim_summary=outcome.normalized_claim_summary,
            extracted_structure=outcome.extracted_structure,
            clarifying_question=outcome.clarifying_question,
            reasons=outcome.reasons,
            gate_policy_version=outcome.gate_policy_version,
            prompt_policy_version=outcome.prompt_policy_version,
            system_variant_id=outcome.system_variant_id,
            model_adapter=outcome.model_adapter,
            model_run_id=outcome.model_run_id,
            created_at=_utc(outcome.created_at),
        )

    def propose_selected_normalization(
        self,
        principal: Principal,
        *,
        source_thesis_candidate_id: uuid.UUID,
    ) -> V3NormalizationAttemptResult:
        try:
            selected = self._source.require_selected_candidate(
                source_thesis_candidate_id,
                client_type=principal.client_type.value,
                agent_client_id=principal.agent_client_id,
            )
            outcome = self._extraction.propose_selected(
                ThesisAnalysisIn(
                    input_text=selected.selected_source_quote,
                    source_url=selected.source_url,
                    client_type=principal.client_type,
                    agent_client_id=principal.agent_client_id,
                ),
                source_interpretation_id=selected.source_interpretation_id,
                source_thesis_candidate_id=(
                    selected.source_thesis_candidate_id
                ),
            )
        except SourceInterpretationNotFound as exc:
            raise NotFound("source thesis candidate not found") from exc
        except SourceCandidateChoiceConflict as exc:
            raise Conflict(
                "source candidate has no matching recorded selection"
            ) from exc
        except NormalizationAttemptConflict as exc:
            raise Conflict(
                "selected candidate normalization binding conflicts with "
                "recorded state"
            ) from exc
        except KeyError as exc:
            raise Unavailable(
                "selected fixture candidate has no normalization fixture"
            ) from exc
        return self._guard(self._attempt_out(outcome))

    @staticmethod
    def _decision_out(outcome) -> V3NormalizationDecisionResult:
        return V3NormalizationDecisionResult(
            normalization_decision_id=outcome.decision_id,
            normalization_attempt_id=outcome.attempt_id,
            action=outcome.action,
            thesis_analysis_id=outcome.thesis_analysis_id,
            created_at=_utc(outcome.created_at),
        )

    def revise_normalization(
        self,
        principal: Principal,
        *,
        normalization_attempt_id: uuid.UUID,
        expected_input_digest: str,
        input_text: str,
        source_url: str | None = None,
    ) -> V3NormalizationAttemptResult:
        try:
            outcome = self._extraction.revise(
                normalization_attempt_id,
                ThesisAnalysisIn(
                    input_text=input_text,
                    source_url=source_url,
                    client_type=principal.client_type,
                    agent_client_id=principal.agent_client_id,
                ),
                actor_id=principal.actor_id,
                expected_input_digest=expected_input_digest,
            )
        except NormalizationAttemptNotFound as exc:
            raise NotFound("normalization attempt not found") from exc
        except NormalizationAttemptConflict as exc:
            raise Conflict(str(exc)) from exc
        except KeyError as exc:
            raise Unavailable(
                "fixture mode cannot interpret this revised input"
            ) from exc
        return self._guard(self._attempt_out(outcome))

    def accept_normalization(
        self,
        principal: Principal,
        *,
        normalization_attempt_id: uuid.UUID,
        expected_input_digest: str,
    ) -> V3NormalizationDecisionResult:
        try:
            outcome = self._extraction.accept(
                normalization_attempt_id,
                client_type=principal.client_type.value,
                agent_client_id=principal.agent_client_id,
                actor_id=principal.actor_id,
                expected_input_digest=expected_input_digest,
            )
        except NormalizationAttemptNotFound as exc:
            raise NotFound("normalization attempt not found") from exc
        except NormalizationAttemptConflict as exc:
            raise Conflict(str(exc)) from exc
        return self._guard(self._decision_out(outcome))

    def reject_normalization(
        self,
        principal: Principal,
        *,
        normalization_attempt_id: uuid.UUID,
        expected_input_digest: str,
        reason: str | None = None,
    ) -> V3NormalizationDecisionResult:
        try:
            outcome = self._extraction.reject(
                normalization_attempt_id,
                client_type=principal.client_type.value,
                agent_client_id=principal.agent_client_id,
                actor_id=principal.actor_id,
                expected_input_digest=expected_input_digest,
                reason=reason,
            )
        except NormalizationAttemptNotFound as exc:
            raise NotFound("normalization attempt not found") from exc
        except NormalizationAttemptConflict as exc:
            raise Conflict(str(exc)) from exc
        return self._guard(self._decision_out(outcome))

    def assess_market_pool(
        self, principal: Principal, *, thesis_analysis_id: uuid.UUID
    ) -> V3MarketPoolResult:
        try:
            outcome = self._market_pool.build(
                thesis_analysis_id,
                client_type=principal.client_type.value,
                agent_client_id=principal.agent_client_id,
            )
        except MarketPoolNotFound as exc:
            raise NotFound("confirmed thesis not found") from exc
        except MarketPoolConflict as exc:
            raise Conflict(str(exc)) from exc
        return self._market_pool_out(principal, outcome.market_display_set_id)

    def _market_pool_out(
        self, principal: Principal, market_display_set_id: uuid.UUID
    ) -> V3MarketPoolResult:
        # Ownership is checked through the confirmed thesis before rendering;
        # all displayed rows are then revalidated against the exact set.
        try:
            with self._sessions() as session:
                display_set = self._market_pool._owned_display_set(
                    session,
                    market_display_set_id,
                    client_type=principal.client_type.value,
                    agent_client_id=principal.agent_client_id,
                )
                snapshot = session.get(MarketSnapshot, display_set.snapshot_id)
                if snapshot is None:
                    raise MarketPoolConflict("market pool snapshot is missing")
                thesis = session.get(ThesisAnalysis, display_set.thesis_analysis_id)
                if thesis is None:
                    raise MarketPoolConflict("market pool accepted thesis is missing")
                candidate_set = session.get(CandidateSet, display_set.candidate_set_id)
                if (
                    candidate_set is None
                    or candidate_set.thesis_analysis_id != display_set.thesis_analysis_id
                    or candidate_set.snapshot_id != display_set.snapshot_id
                ):
                    raise MarketPoolConflict("market pool candidate-set binding is invalid")
                try:
                    retrieval_scope = persisted_candidate_set_scope(
                        candidate_set.retrieval_scope
                    )
                except ValueError as exc:
                    raise MarketPoolConflict("candidate retrieval scope is invalid") from exc
                if retrieval_scope is not None and (
                    retrieval_scope.snapshot_id != candidate_set.snapshot_id
                    or retrieval_scope.retrieval_id != candidate_set.retrieval_id
                ):
                    raise MarketPoolConflict("candidate retrieval scope is not bound")
                items = session.scalars(
                    select(MarketDisplayItem)
                    .where(
                        MarketDisplayItem.market_display_set_id
                        == display_set.id
                    )
                    .order_by(MarketDisplayItem.display_rank)
                ).all()
                if len(items) != display_set.displayed_count:
                    raise MarketPoolConflict(
                        "displayed market count does not match bound items"
                    )
                if (
                    display_set.displayed_count > 3
                    or display_set.assessed_count < display_set.displayed_count
                    or (
                        display_set.assessment_complete
                        and display_set.displayed_count
                        != display_set.target_count
                    )
                    or (
                        display_set.system_pool_outcome
                        == "no_clean_expression"
                        and (
                            not display_set.assessment_complete
                            or display_set.displayed_count == 0
                        )
                    )
                ):
                    raise MarketPoolConflict(
                        "market pool completeness binding is invalid"
                    )
                cards: list[V3MarketAssessmentResult] = []
                for item in items:
                    assessment = session.get(
                        MarketAssessment, item.market_assessment_id
                    )
                    if assessment is None:
                        raise MarketPoolConflict(
                            "display item assessment is missing"
                        )
                    capture = session.get(
                        MarketRulesCapture, assessment.rules_capture_id
                    )
                    if (
                        assessment.thesis_analysis_id
                        != display_set.thesis_analysis_id
                        or assessment.candidate_set_id
                        != display_set.candidate_set_id
                        or assessment.fit_card_id != display_set.fit_card_id
                        or assessment.snapshot_id != display_set.snapshot_id
                        or capture is None
                        or capture.market_id != assessment.market_id
                        or capture.snapshot_id != assessment.snapshot_id
                    ):
                        raise MarketPoolConflict(
                            "cross-bound market assessment cannot be rendered"
                        )
                    cards.append(
                        V3MarketAssessmentResult(
                            market_assessment_id=assessment.id,
                            candidate_set_member_id=(
                                assessment.candidate_set_member_id
                            ),
                            market_id=assessment.market_id,
                            market_title=capture.contract_terms_text,
                            resolution_conditions=(
                                capture.resolution_rules_text
                            ),
                            pair_class=assessment.pair_class,
                            retrieval_rank=assessment.retrieval_rank,
                            display_rank=item.display_rank,
                            what_it_captures=assessment.what_it_captures,
                            what_it_misses=assessment.what_it_misses,
                            horizon_match=assessment.horizon_match,
                            resolution_risk=assessment.resolution_risk,
                            fit_confidence=assessment.fit_confidence,
                            authority=assessment.authority,
                            snapshot_id=assessment.snapshot_id,
                            rules_capture_id=assessment.rules_capture_id,
                        )
                    )
                if [card.display_rank for card in cards] != list(
                    range(1, len(cards) + 1)
                ):
                    raise MarketPoolConflict(
                        "display ranks are not contiguous"
                    )
                if len({card.market_id for card in cards}) != len(cards):
                    raise MarketPoolConflict(
                        "displayed market identities are not unique"
                    )
                projected_complete, projected_outcome, projected_reasons = (
                    project_pool_outcome(
                        [card.pair_class for card in cards],
                        target_count=display_set.target_count,
                    )
                )
                if (
                    projected_complete != display_set.assessment_complete
                    or projected_outcome != display_set.system_pool_outcome
                    or projected_reasons
                    != list(display_set.incomplete_reasons)
                ):
                    raise MarketPoolConflict(
                        "market pool outcome binding is invalid"
                    )
                result = V3MarketPoolResult(
                    market_display_set_id=display_set.id,
                    fit_card_id=display_set.fit_card_id,
                    thesis_analysis_id=display_set.thesis_analysis_id,
                    accepted_thesis_summary=thesis.normalized_claim_summary,
                    candidate_set_id=display_set.candidate_set_id,
                    retrieval_scope=retrieval_scope,
                    snapshot_id=display_set.snapshot_id,
                    snapshot_as_of=_utc(snapshot.as_of_ts),
                    display_policy_version=display_set.display_policy_version,
                    assessed_count=display_set.assessed_count,
                    target_count=display_set.target_count,
                    displayed_count=display_set.displayed_count,
                    assessment_complete=display_set.assessment_complete,
                    system_pool_outcome=display_set.system_pool_outcome,
                    incomplete_reasons=list(display_set.incomplete_reasons),
                    candidate_markets=cards,
                )
        except MarketPoolNotFound as exc:
            raise NotFound("market pool not found") from exc
        except MarketPoolConflict as exc:
            raise Conflict(str(exc)) from exc
        return self._guard(result)

    def choose_market(
        self,
        principal: Principal,
        *,
        market_display_set_id: uuid.UUID,
        selection_kind: str,
        market_assessment_id: uuid.UUID | None,
        reason: str | None = None,
    ) -> V3MarketChoiceResult:
        try:
            outcome = self._market_pool.choose(
                market_display_set_id,
                selection_kind=selection_kind,
                market_assessment_id=market_assessment_id,
                actor_id=principal.actor_id,
                client_type=principal.client_type.value,
                agent_client_id=principal.agent_client_id,
                reason=reason,
            )
        except MarketPoolNotFound as exc:
            raise NotFound("market pool not found") from exc
        except MarketPoolConflict as exc:
            raise Conflict(
                "market choice conflicts with recorded display state"
            ) from exc
        return self._guard(
            V3MarketChoiceResult(
                market_choice_id=outcome.market_choice_id,
                market_display_set_id=outcome.market_display_set_id,
                selection_kind=outcome.selection_kind,
                market_assessment_id=outcome.market_assessment_id,
                created_at=_utc(outcome.created_at),
            )
        )
