"""Product-UI API layer — human-flavored composition over the loop services.

Mirrors ``el.mcp.tools.McpTools`` for a local human_ui actor: no new
fit/ledger/odds logic, marshaling only. Reuses the typed loop errors and the
A7 vocabulary guard from ``el.mcp`` (they are loop semantics, not transport
semantics). Differences from the MCP surface, per
docs/agent-guided-ui-contract-v0.md:

- actor is the single local human (client_type=human_ui; saves are attested);
- client_ref is ``product_ui`` (honest provenance, never "mcp");
- the fit response separates the gate verdict from candidate metadata and
  carries the recommended market's title/rules snapshot for the card;
- fixture-mode proposer misses surface as an explicit unavailable state.
"""

from datetime import timezone
from hashlib import sha256
import json
import uuid
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.contracts import ThesisAnalysisIn
from el.domain.enums import (
    ClientType,
    ConvictionLevel,
    ExposureBucket,
    FitClass,
    PriorConfidence,
    ReviewSource,
    ReviewStatus,
)
from el.domain.tables import (
    CandidateSet,
    CandidateSetMember,
    FitCard,
    LedgerEntry,
    MarketAssessment,
    MarketDisplayItem,
    MarketDisplaySet,
    MarketRecommendation,
    MarketRulesCapture,
    MarketSnapshot,
    NormalizationAttempt,
    NormalizationDecision,
    RejectedMarketRow,
    ReviewCandidate,
    SourceCandidateChoice,
    SourceInterpretation,
    SourceInterpretationRequest,
    SourceThesisCandidate,
    ThesisAnalysis,
)
from el.extraction.service import NormalizationAttemptNotFound
from el.marketpool.service import MarketPoolConflict, MarketPoolNotFound
from el.mcp.contracts import (
    BlindPriorRequired,
    NotFound,
    SaveRejected,
    ToolRefused,
)
from el.mcp.vocab_guard import assert_a7_clean
from el.product.wiring import (
    CLIENT_REF,
    MULTI_THESIS_FIXTURE,
    HARBOR_CLARIFICATION_ANSWER,
    HARBOR_CLARIFICATION_QUESTION,
    ORCHARD_CLARIFICATION_ANSWER,
    ORCHARD_CLARIFICATION_QUESTION,
    HumanActor,
    ProductServices,
)
from el.retrieval.scope import (
    CandidateSetRetrievalScope,
    persisted_candidate_set_scope,
)
from el.sourceinterpretation.service import (
    SourceCandidateChoiceConflict,
    SourceInterpretationNotFound,
)


class ProposerUnavailable(Exception):
    """Model service unavailable (dead adapter, missing credentials, or a
    fixture-mode input outside the fixture set). Explicit state — AC-7."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset()


class IntakeResult(_Out):
    thesis_analysis_id: uuid.UUID
    normalized_claim_summary: str
    extracted_structure: dict
    input_text: str
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"input_text", "extracted_structure.entities[].name"}
    )


class NormalizationAttemptUiOut(_Out):
    normalization_attempt_id: uuid.UUID
    predecessor_attempt_id: uuid.UUID | None
    source_interpretation_id: uuid.UUID | None
    source_thesis_candidate_id: uuid.UUID | None
    outcome: str
    verdict: str
    input_digest: str | None
    normalized_claim_summary: str | None
    extracted_structure: dict | None
    clarifying_question: str | None
    reasons: list[str]
    gate_policy_version: str
    prompt_policy_version: str
    system_variant_id: str
    model_adapter: str | None
    model_run_id: str | None
    created_at: str
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"extracted_structure.entities[].name", "reasons[]"}
    )


class NormalizationDecisionUiOut(_Out):
    normalization_decision_id: uuid.UUID
    normalization_attempt_id: uuid.UUID
    action: str
    thesis_analysis_id: uuid.UUID | None
    created_at: str


class RestoredSourceUiOut(_Out):
    """Historical source context, never an active input or source choice."""

    thesis_analysis_id: uuid.UUID
    normalization_decision_id: uuid.UUID
    source_interpretation_request_id: uuid.UUID | None
    source_interpretation_id: uuid.UUID
    source_thesis_candidate_id: uuid.UUID
    source_candidate_choice_id: uuid.UUID
    original_source_text: str
    selected_source_quote: str
    accepted_normalization_input: str
    source_url: str | None


class AcceptedThesisStateUiOut(_Out):
    """Read-only evidence of one locally accepted v3 thesis."""

    thesis_analysis_id: uuid.UUID
    accepted_thesis_summary: str
    normalization_decision_id: uuid.UUID
    accepted_at: str
    acceptance_origin: Literal["human_ui"]
    restored_source: RestoredSourceUiOut | None
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {
            "restored_source.original_source_text",
            "restored_source.selected_source_quote",
            "restored_source.accepted_normalization_input",
            "restored_source.source_url",
        }
    )


class SourceThesisCandidateUiOut(_Out):
    source_thesis_candidate_id: uuid.UUID
    ordinal: int
    selected_source_quote: str
    source_quote_digest: str
    claim_summary: str
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"selected_source_quote"}
    )


class SourceInterpretationUiOut(_Out):
    source_interpretation_id: uuid.UUID
    outcome: str
    input_digest: str | None
    reasons: list[str]
    prompt_policy_version: str
    system_variant_id: str
    model_adapter: str | None
    model_run_id: str | None
    candidates: list[SourceThesisCandidateUiOut]
    created_at: str
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"candidates[].selected_source_quote"}
    )


class SourceCandidateChoiceUiOut(_Out):
    source_candidate_choice_id: uuid.UUID
    source_interpretation_id: uuid.UUID
    selection_kind: str
    source_thesis_candidate_id: uuid.UUID | None
    created_at: str


class BlindPriorOut(_Out):
    thesis_analysis_id: uuid.UUID
    conviction_event_id: uuid.UUID | None
    status: str


class MarketSnapshotOut(_Out):
    market_id: str
    title: str | None
    resolution_rules: str | None
    # Market text is quoted venue source, not system copy.
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"title", "resolution_rules"}
    )


class RejectedMarketUiOut(_Out):
    market_id: str
    title: str | None
    reason: str
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset({"title"})


class CandidateEvidenceOut(_Out):
    """Retrieval provenance — candidate evidence, never the verdict (AC-3)."""

    candidate_set_id: uuid.UUID
    snapshot_id: str
    candidate_count: int
    rejected_count: int
    skipped_ineligible: int
    structures_extracted: int


class FitCardOut(_Out):
    fit_card_id: uuid.UUID
    thesis_analysis_id: uuid.UUID
    semantic_fit_class: str
    recommended_market_id: str | None
    recommended_market: MarketSnapshotOut | None
    current_odds: float | None
    odds_side: str | None
    what_it_captures: str | None
    what_it_misses: str | None
    horizon_match: str | None
    resolution_risk: str | None
    fit_confidence: float | None
    draft_contract_recommended: bool
    rejected_markets: list[RejectedMarketUiOut]
    candidate_evidence: CandidateEvidenceOut
    provenance: dict
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {
            "recommended_market.title",
            "recommended_market.resolution_rules",
            "rejected_markets[].title",
        }
    )


class MarketAssessmentUiOut(_Out):
    market_assessment_id: uuid.UUID
    candidate_set_member_id: uuid.UUID
    market_id: str
    market_title: str
    resolution_conditions: str
    pair_class: str
    retrieval_rank: int
    display_rank: int
    what_it_captures: str
    what_it_misses: str
    horizon_match: str | None
    resolution_risk: str | None
    fit_confidence: float | None
    authority: str
    snapshot_id: str
    rules_capture_id: uuid.UUID
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"market_title", "resolution_conditions"}
    )


class MarketPoolUiOut(_Out):
    market_display_set_id: uuid.UUID
    fit_card_id: uuid.UUID
    thesis_analysis_id: uuid.UUID
    accepted_thesis_summary: str
    candidate_set_id: uuid.UUID
    retrieval_scope: CandidateSetRetrievalScope | None
    snapshot_id: str
    snapshot_as_of: str
    display_policy_version: str
    assessed_count: int
    target_count: int
    displayed_count: int
    assessment_complete: bool
    system_pool_outcome: str
    incomplete_reasons: list[str]
    candidate_markets: list[MarketAssessmentUiOut]
    # Only captured contract text is quoted source. The normalized thesis
    # summary and observed query provenance are checked system output.
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {
            "candidate_markets[].market_title",
            "candidate_markets[].resolution_conditions",
        }
    )


class MarketChoiceUiOut(_Out):
    market_choice_id: uuid.UUID
    market_display_set_id: uuid.UUID
    selection_kind: str
    market_assessment_id: uuid.UUID | None
    created_at: str


class DraftPreviewOut(_Out):
    generated: bool
    gate_verdict: str | None
    label: str | None = None
    draft_contract_id: uuid.UUID | None = None
    proposed_title: str | None = None
    proposed_resolution_logic: str | None = None
    resolution_source: str | None = None
    resolution_deadline: str | None = None
    subject_entity: str | None = None


class LedgerEntryUiOut(_Out):
    id: uuid.UUID
    thesis_analysis_id: uuid.UUID | None
    thesis_summary: str | None
    user_justification: str | None
    linked_market_id: str | None
    linked_market_title: str | None
    odds_at_entry: float | None
    odds_at_entry_side: str | None
    fit_class: str | None
    attestation_status: str | None
    status: str | None
    client_type: str | None
    created_at: str | None
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"user_justification", "linked_market_title"}
    )


class CorrectionOut(_Out):
    """Acknowledgment only: the card this correction is about is unchanged."""

    review_candidate_id: uuid.UUID
    status: str
    already_recorded: bool


class ProductApi:
    def __init__(self, services: ProductServices, actor: HumanActor):
        self._s = services
        self._actor = actor
        self._sessions: sessionmaker[Session] = services.session_factory

    @property
    def mode(self) -> str:
        return self._s.mode

    def _guard(self, model: _Out) -> _Out:
        assert_a7_clean(model.model_dump(mode="json"), source_paths=model.SOURCE_PATHS)
        return model

    @staticmethod
    def _iso_utc(value) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    # --- ownership (same no-existence-leak rule as the MCP surface) --------
    def _require_thesis_access(self, thesis_analysis_id: uuid.UUID) -> None:
        with self._sessions() as s:
            t = s.get(ThesisAnalysis, thesis_analysis_id)
        if (
            t is None
            or t.agent_client_id != self._actor.agent_client_id
            or t.client_type != self._actor.client_type.value
        ):
            raise NotFound("thesis_analysis not found")

    def _require_fit_card_access(self, fit_card_id: uuid.UUID) -> uuid.UUID:
        with self._sessions() as s:
            card = s.get(FitCard, fit_card_id)
            thesis_analysis_id = card.thesis_analysis_id if card else None
        if thesis_analysis_id is None:
            raise NotFound("fit_card not found")
        self._require_thesis_access(thesis_analysis_id)
        return thesis_analysis_id

    @staticmethod
    def _normalization_not_found(exc: Exception) -> None:
        raise NotFound("normalization_attempt not found") from exc

    def _attempt_out(self, outcome) -> NormalizationAttemptUiOut:
        return self._guard(
            NormalizationAttemptUiOut(
                normalization_attempt_id=outcome.attempt_id,
                predecessor_attempt_id=outcome.predecessor_attempt_id,
                source_interpretation_id=outcome.source_interpretation_id,
                source_thesis_candidate_id=(
                    outcome.source_thesis_candidate_id
                ),
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
                created_at=self._iso_utc(outcome.created_at),
            )
        )

    def _decision_out(self, outcome) -> NormalizationDecisionUiOut:
        return self._guard(
            NormalizationDecisionUiOut(
                normalization_decision_id=outcome.decision_id,
                normalization_attempt_id=outcome.attempt_id,
                action=outcome.action,
                thesis_analysis_id=outcome.thesis_analysis_id,
                created_at=self._iso_utc(outcome.created_at),
            )
        )

    # --- Product Discovery v3 normalization confirmation -------------------
    def interpret_source(
        self, input_text: str, *, source_url: str | None = None
    ) -> SourceInterpretationUiOut:
        try:
            outcome = self._s.source_interpretation.interpret(
                input_text,
                source_url=source_url,
                client_type=self._actor.client_type.value,
                agent_client_id=self._actor.agent_client_id,
            )
        except KeyError:
            raise ProposerUnavailable(
                "model service unavailable — fixture mode only recognizes the "
                "checked-in fixture sources; paste one of those or run in "
                "gemini mode"
            )
        return self._guard(
            SourceInterpretationUiOut(
                source_interpretation_id=outcome.source_interpretation_id,
                outcome=outcome.outcome,
                input_digest=outcome.input_digest,
                reasons=outcome.reasons,
                prompt_policy_version=outcome.prompt_policy_version,
                system_variant_id=outcome.system_variant_id,
                model_adapter=outcome.model_adapter,
                model_run_id=outcome.model_run_id,
                candidates=[
                    SourceThesisCandidateUiOut(
                        source_thesis_candidate_id=(
                            candidate.source_thesis_candidate_id
                        ),
                        ordinal=candidate.ordinal,
                        selected_source_quote=(
                            candidate.selected_source_quote
                        ),
                        source_quote_digest=candidate.source_quote_digest,
                        claim_summary=candidate.claim_summary,
                    )
                    for candidate in outcome.candidates
                ],
                created_at=self._iso_utc(outcome.created_at),
            )
        )

    def choose_source_candidate(
        self,
        source_interpretation_id: uuid.UUID,
        *,
        selection_kind: str,
        source_thesis_candidate_id: uuid.UUID | None,
        reason: str | None = None,
    ) -> SourceCandidateChoiceUiOut:
        try:
            outcome = self._s.source_interpretation.choose(
                source_interpretation_id,
                selection_kind=selection_kind,
                source_thesis_candidate_id=source_thesis_candidate_id,
                actor_id=self._actor.actor_id,
                client_type=self._actor.client_type.value,
                agent_client_id=self._actor.agent_client_id,
                reason=reason,
            )
        except SourceInterpretationNotFound as exc:
            raise NotFound("source interpretation not found") from exc
        return self._guard(
            SourceCandidateChoiceUiOut(
                source_candidate_choice_id=outcome.source_candidate_choice_id,
                source_interpretation_id=outcome.source_interpretation_id,
                selection_kind=outcome.selection_kind,
                source_thesis_candidate_id=(
                    outcome.source_thesis_candidate_id
                ),
                created_at=self._iso_utc(outcome.created_at),
            )
        )

    def propose_selected_normalization(
        self, source_thesis_candidate_id: uuid.UUID
    ) -> NormalizationAttemptUiOut:
        try:
            selected = self._s.source_interpretation.require_selected_candidate(
                source_thesis_candidate_id,
                client_type=self._actor.client_type.value,
                agent_client_id=self._actor.agent_client_id,
            )
            outcome = self._s.extraction.propose_selected(
                ThesisAnalysisIn(
                    input_text=selected.selected_source_quote,
                    source_url=selected.source_url,
                    client_type=self._actor.client_type,
                    agent_client_id=self._actor.agent_client_id,
                ),
                source_interpretation_id=selected.source_interpretation_id,
                source_thesis_candidate_id=(
                    selected.source_thesis_candidate_id
                ),
            )
        except SourceInterpretationNotFound as exc:
            raise NotFound("source thesis candidate not found") from exc
        except KeyError:
            raise ProposerUnavailable(
                "model service unavailable — selected fixture candidate has no "
                "checked-in normalization fixture"
            )
        return self._attempt_out(outcome)

    def propose_normalization(
        self, input_text: str, *, source_url: str | None = None
    ) -> NormalizationAttemptUiOut:
        try:
            outcome = self._s.extraction.propose(
                ThesisAnalysisIn(
                    input_text=input_text,
                    source_url=source_url,
                    client_type=self._actor.client_type,
                    agent_client_id=self._actor.agent_client_id,
                )
            )
        except KeyError:
            raise ProposerUnavailable(
                "model service unavailable — fixture mode only recognizes the "
                "checked-in fixture theses; paste one of those or run in "
                "gemini mode"
            )
        return self._attempt_out(outcome)

    def accept_normalization(
        self,
        attempt_id: uuid.UUID,
        *,
        expected_input_digest: str | None,
    ) -> NormalizationDecisionUiOut:
        try:
            outcome = self._s.extraction.accept(
                attempt_id,
                client_type=self._actor.client_type.value,
                agent_client_id=self._actor.agent_client_id,
                actor_id=self._actor.actor_id,
                expected_input_digest=expected_input_digest,
            )
        except NormalizationAttemptNotFound as exc:
            self._normalization_not_found(exc)
        return self._decision_out(outcome)

    def get_accepted_thesis_state(
        self, thesis_analysis_id: uuid.UUID
    ) -> AcceptedThesisStateUiOut:
        """Read an owned acceptance and its verified historical source chain."""

        missing = NotFound("accepted thesis state not found")
        if self._actor.client_type != ClientType.HUMAN_UI:
            raise missing
        with self._sessions() as session:
            thesis = session.get(ThesisAnalysis, thesis_analysis_id)
            if (
                thesis is None
                or thesis.client_type != ClientType.HUMAN_UI.value
                or thesis.agent_client_id != self._actor.agent_client_id
            ):
                raise missing
            decision = session.scalar(
                select(NormalizationDecision).where(
                    NormalizationDecision.thesis_analysis_id == thesis_analysis_id,
                    NormalizationDecision.action == "accept",
                )
            )
            if decision is None or decision.actor_id != self._actor.actor_id:
                raise missing
            attempt = session.get(NormalizationAttempt, decision.attempt_id)
            if (
                attempt is None
                or attempt.outcome != "candidate"
                or attempt.client_type != ClientType.HUMAN_UI.value
                or attempt.agent_client_id != self._actor.agent_client_id
                or attempt.input_text != thesis.input_text
            ):
                raise missing
            source = self._restored_source_for_acceptance(
                session, thesis.id, decision.id, attempt
            )
            return self._guard(
                AcceptedThesisStateUiOut(
                    thesis_analysis_id=thesis.id,
                    accepted_thesis_summary=thesis.normalized_claim_summary,
                    normalization_decision_id=decision.id,
                    accepted_at=self._iso_utc(decision.created_at),
                    acceptance_origin=ClientType.HUMAN_UI.value,
                    restored_source=source,
                )
            )

    def _restored_source_for_acceptance(
        self,
        session: Session,
        thesis_id: uuid.UUID,
        decision_id: uuid.UUID,
        accepted_attempt: NormalizationAttempt,
    ) -> RestoredSourceUiOut | None:
        """Follow accepted edits to the selected source, failing closed on drift."""

        missing = NotFound("accepted thesis state not found")
        attempt = accepted_attempt
        visited: set[uuid.UUID] = set()
        for _ in range(32):
            if (
                attempt.id in visited
                or attempt.client_type != ClientType.HUMAN_UI.value
                or attempt.agent_client_id != self._actor.agent_client_id
            ):
                raise missing
            visited.add(attempt.id)
            has_source = (
                attempt.source_interpretation_id is not None
                or attempt.source_thesis_candidate_id is not None
            )
            if has_source:
                if (
                    attempt.source_interpretation_id is None
                    or attempt.source_thesis_candidate_id is None
                    or attempt.predecessor_attempt_id is not None
                ):
                    raise missing
                break
            if attempt.predecessor_attempt_id is None:
                return None
            prior = session.get(NormalizationAttempt, attempt.predecessor_attempt_id)
            prior_decision = session.scalar(
                select(NormalizationDecision).where(
                    NormalizationDecision.attempt_id == attempt.predecessor_attempt_id
                )
            )
            if (
                prior is None
                or prior_decision is None
                or prior_decision.action != "edit"
                or prior_decision.actor_id != self._actor.actor_id
            ):
                raise missing
            attempt = prior
        else:
            raise missing

        interpretation = session.get(
            SourceInterpretation, attempt.source_interpretation_id
        )
        candidate = session.get(
            SourceThesisCandidate, attempt.source_thesis_candidate_id
        )
        choice = session.scalar(
            select(SourceCandidateChoice).where(
                SourceCandidateChoice.source_interpretation_id
                == attempt.source_interpretation_id
            )
        )
        if (
            interpretation is None
            or interpretation.outcome != "candidates"
            or interpretation.client_type != ClientType.HUMAN_UI.value
            or interpretation.agent_client_id != self._actor.agent_client_id
            or interpretation.input_text is None
            or candidate is None
            or candidate.source_interpretation_id != interpretation.id
            or choice is None
            or choice.selection_kind != "candidate"
            or choice.source_thesis_candidate_id != candidate.id
            or choice.client_type != ClientType.HUMAN_UI.value
            or choice.actor_id != self._actor.actor_id
            or attempt.input_text != candidate.selected_source_quote
            or attempt.input_digest != candidate.source_quote_digest
        ):
            raise missing

        source_text = interpretation.input_text
        quote = candidate.selected_source_quote
        if (
            not source_text
            or not quote
            or quote not in source_text
            or interpretation.input_digest
            != sha256(source_text.encode("utf-8")).hexdigest()
            or candidate.source_quote_digest
            != sha256(quote.encode("utf-8")).hexdigest()
        ):
            raise missing
        request_id = interpretation.source_interpretation_request_id
        if request_id is not None:
            request = session.get(SourceInterpretationRequest, request_id)
            if (
                request is None
                or request.owner_client_type != ClientType.HUMAN_UI.value
                or request.owner_actor_id != self._actor.actor_id
                or request.agent_client_id != self._actor.agent_client_id
                or request.input_text != source_text
                or request.input_digest != interpretation.input_digest
                or request.source_url != interpretation.source_url
            ):
                raise missing

        return RestoredSourceUiOut(
            thesis_analysis_id=thesis_id,
            normalization_decision_id=decision_id,
            source_interpretation_request_id=request_id,
            source_interpretation_id=interpretation.id,
            source_thesis_candidate_id=candidate.id,
            source_candidate_choice_id=choice.id,
            original_source_text=source_text,
            selected_source_quote=quote,
            accepted_normalization_input=accepted_attempt.input_text,
            source_url=interpretation.source_url,
        )

    def reject_normalization(
        self,
        attempt_id: uuid.UUID,
        *,
        expected_input_digest: str | None,
        reason: str | None = None,
    ) -> NormalizationDecisionUiOut:
        try:
            outcome = self._s.extraction.reject(
                attempt_id,
                client_type=self._actor.client_type.value,
                agent_client_id=self._actor.agent_client_id,
                actor_id=self._actor.actor_id,
                expected_input_digest=expected_input_digest,
                reason=reason,
            )
        except NormalizationAttemptNotFound as exc:
            self._normalization_not_found(exc)
        return self._decision_out(outcome)

    def revise_normalization(
        self,
        attempt_id: uuid.UUID,
        *,
        input_text: str,
        source_url: str | None = None,
        expected_input_digest: str | None,
    ) -> NormalizationAttemptUiOut:
        try:
            outcome = self._s.extraction.revise(
                attempt_id,
                ThesisAnalysisIn(
                    input_text=input_text,
                    source_url=source_url,
                    client_type=self._actor.client_type,
                    agent_client_id=self._actor.agent_client_id,
                ),
                actor_id=self._actor.actor_id,
                expected_input_digest=expected_input_digest,
            )
        except NormalizationAttemptNotFound as exc:
            self._normalization_not_found(exc)
        except KeyError:
            raise ProposerUnavailable(
                "fixture mode cannot interpret custom clarification wording — "
                "use the checked-in fixture answer or run in gemini mode"
            )
        return self._attempt_out(outcome)

    # --- Product Discovery v3 top-three market pool ------------------------
    def assess_market_pool(
        self, thesis_analysis_id: uuid.UUID
    ) -> MarketPoolUiOut:
        try:
            outcome = self._s.market_pool.build(
                thesis_analysis_id,
                client_type=self._actor.client_type.value,
                agent_client_id=self._actor.agent_client_id,
            )
        except MarketPoolNotFound as exc:
            raise NotFound("confirmed thesis not found") from exc
        return self._market_pool_out(outcome.market_display_set_id)

    def _market_pool_out(
        self, market_display_set_id: uuid.UUID
    ) -> MarketPoolUiOut:
        with self._sessions() as session:
            display_set = session.get(
                MarketDisplaySet, market_display_set_id
            )
            if display_set is None:
                raise NotFound("market pool not found")
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
                    MarketDisplayItem.market_display_set_id == display_set.id
                )
                .order_by(MarketDisplayItem.display_rank)
            ).all()
            cards: list[MarketAssessmentUiOut] = []
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
                    MarketAssessmentUiOut(
                        market_assessment_id=assessment.id,
                        candidate_set_member_id=(
                            assessment.candidate_set_member_id
                        ),
                        market_id=assessment.market_id,
                        market_title=capture.contract_terms_text,
                        resolution_conditions=capture.resolution_rules_text,
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
                raise MarketPoolConflict("display ranks are not contiguous")
            return self._guard(
                MarketPoolUiOut(
                    market_display_set_id=display_set.id,
                    fit_card_id=display_set.fit_card_id,
                    thesis_analysis_id=display_set.thesis_analysis_id,
                    accepted_thesis_summary=thesis.normalized_claim_summary,
                    candidate_set_id=display_set.candidate_set_id,
                    retrieval_scope=retrieval_scope,
                    snapshot_id=display_set.snapshot_id,
                    snapshot_as_of=self._iso_utc(snapshot.as_of_ts),
                    display_policy_version=display_set.display_policy_version,
                    assessed_count=display_set.assessed_count,
                    target_count=display_set.target_count,
                    displayed_count=display_set.displayed_count,
                    assessment_complete=display_set.assessment_complete,
                    system_pool_outcome=display_set.system_pool_outcome,
                    incomplete_reasons=list(display_set.incomplete_reasons),
                    candidate_markets=cards,
                )
            )

    def choose_market(
        self,
        market_display_set_id: uuid.UUID,
        *,
        selection_kind: str,
        market_assessment_id: uuid.UUID | None,
        reason: str | None = None,
    ) -> MarketChoiceUiOut:
        try:
            outcome = self._s.market_pool.choose(
                market_display_set_id,
                selection_kind=selection_kind,
                market_assessment_id=market_assessment_id,
                actor_id=self._actor.actor_id,
                client_type=self._actor.client_type.value,
                agent_client_id=self._actor.agent_client_id,
                reason=reason,
            )
        except MarketPoolNotFound as exc:
            raise NotFound("market pool not found") from exc
        return self._guard(
            MarketChoiceUiOut(
                market_choice_id=outcome.market_choice_id,
                market_display_set_id=outcome.market_display_set_id,
                selection_kind=outcome.selection_kind,
                market_assessment_id=outcome.market_assessment_id,
                created_at=self._iso_utc(outcome.created_at),
            )
        )

    def record_assessment_feedback(
        self,
        market_assessment_id: uuid.UUID,
        *,
        feedback_kind: str,
        note: str,
    ) -> CorrectionOut:
        if feedback_kind not in {"wrong_class", "not_an_expression", "other"}:
            raise ValueError("unsupported assessment feedback kind")
        if not note.strip():
            raise ValueError("feedback note is required")
        try:
            assessment = self._s.market_pool.require_displayed_assessment(
                market_assessment_id,
                client_type=self._actor.client_type.value,
                agent_client_id=self._actor.agent_client_id,
            )
        except MarketPoolNotFound as exc:
            raise NotFound("market assessment not found") from exc
        return self._intake(
            object_type="market_assessment",
            object_id=assessment.id,
            source=(
                ReviewSource.USER_REJECTION
                if feedback_kind == "not_an_expression"
                else ReviewSource.USER_CORRECTION
            ),
            payload={
                "market_id": assessment.market_id,
                "pair_class": assessment.pair_class,
                "feedback_kind": feedback_kind,
                "notes": note.strip(),
            },
        )

    # --- S1 intake ----------------------------------------------------------
    def intake(self, input_text: str) -> IntakeResult:
        try:
            outcome = self._s.extraction.analyze(
                ThesisAnalysisIn(
                    input_text=input_text,
                    client_type=self._actor.client_type,
                    agent_client_id=self._actor.agent_client_id,
                )
            )
        except KeyError:
            raise ProposerUnavailable(
                "model service unavailable — fixture mode only recognizes the "
                "checked-in fixture theses; paste one of those or run in "
                "gemini mode"
            )
        if outcome.analysis is None:
            raise ToolRefused(outcome.result.reasons)
        a = outcome.analysis
        return self._guard(
            IntakeResult(
                thesis_analysis_id=a.id,
                normalized_claim_summary=a.normalized_claim_summary,
                extracted_structure=a.extracted_structure.model_dump(mode="json"),
                input_text=a.input_text,
            )
        )

    # --- S2 blind prior -----------------------------------------------------
    def submit_blind_prior(
        self,
        thesis_analysis_id: uuid.UUID,
        *,
        prior_probability: float,
        prior_confidence: str | None = None,
        prior_reason: str | None = None,
    ) -> BlindPriorOut:
        self._require_thesis_access(thesis_analysis_id)
        outcome = self._s.ledger.submit_blind_prior(
            thesis_analysis_id,
            client_type=self._actor.client_type,
            actor_id=self._actor.actor_id,
            client_ref=CLIENT_REF,
            prior_probability=prior_probability,
            prior_confidence=(
                PriorConfidence(prior_confidence) if prior_confidence else None
            ),
            prior_reason=prior_reason,
            agent_client_id=self._actor.agent_client_id,
        )
        return self._guard(
            BlindPriorOut(
                thesis_analysis_id=thesis_analysis_id,
                conviction_event_id=outcome.conviction_event_id,
                status=outcome.status,
            )
        )

    # --- S3 classify → Market Fit Card --------------------------------------
    def classify(self, thesis_analysis_id: uuid.UUID) -> FitCardOut:
        self._require_thesis_access(thesis_analysis_id)
        if not self._s.ledger.has_blind_prior(
            thesis_analysis_id,
            client_type=self._actor.client_type,
            actor_id=self._actor.actor_id,
        ):
            raise BlindPriorRequired(thesis_analysis_id)
        retrieval = self._s.retrieval.retrieve_candidates(thesis_analysis_id)
        outcome = self._s.fit.classify_fit(
            thesis_analysis_id, retrieval.candidate_set_id
        )
        reveal = self._s.ledger.reveal_current_odds(
            outcome.fit_card_id,
            client_type=self._actor.client_type,
            actor_id=self._actor.actor_id,
            client_ref=CLIENT_REF,
        )
        # Titles are retrieval-record data (frozen snapshot), not persisted on
        # structures; take them from THIS request's retrieval outcome so the
        # card can never show a title from another chain.
        titles = {c.market_id: c.title for c in retrieval.candidates}
        with self._sessions() as s:
            card = s.get(FitCard, outcome.fit_card_id)
            prov = card.provenance or {}
            candidate_count = (
                s.scalar(
                    select(func.count())
                    .select_from(CandidateSetMember)
                    .where(
                        CandidateSetMember.candidate_set_id
                        == outcome.candidate_set_id
                    )
                )
                or 0
            )
            result = FitCardOut(
                fit_card_id=card.id,
                thesis_analysis_id=card.thesis_analysis_id,
                semantic_fit_class=card.semantic_fit_class,
                recommended_market_id=card.recommended_market_id,
                recommended_market=self._market_snapshot(
                    s,
                    card.recommended_market_id,
                    titles=titles,
                    snapshot_id=retrieval.snapshot_id,
                ),
                current_odds=reveal.current_odds,
                odds_side=reveal.side if reveal.revealed else None,
                what_it_captures=card.what_it_captures,
                what_it_misses=card.what_it_misses,
                horizon_match=card.horizon_match,
                resolution_risk=card.resolution_risk,
                fit_confidence=card.fit_confidence,
                draft_contract_recommended=outcome.draft_contract_recommended,
                rejected_markets=self._rejected_markets(
                    s,
                    card.id,
                    titles=titles,
                ),
                candidate_evidence=CandidateEvidenceOut(
                    candidate_set_id=outcome.candidate_set_id,
                    snapshot_id=retrieval.snapshot_id,
                    candidate_count=candidate_count,
                    rejected_count=outcome.rejected_count,
                    skipped_ineligible=outcome.skipped_ineligible,
                    structures_extracted=outcome.structures_extracted,
                ),
                provenance={
                    "gate_policy_version": prov.get("gate_policy_version"),
                    "authority": prov.get("authority"),
                    "confidence_source": prov.get("confidence_source"),
                    "thesis_side": prov.get("thesis_side"),
                    "client_type": self._actor.client_type.value,
                    "client_ref": CLIENT_REF,
                },
            )
        return self._guard(result)

    def _market_snapshot(
        self,
        s: Session,
        market_id: str | None,
        *,
        titles: dict[str, str | None],
        snapshot_id: str,
    ) -> MarketSnapshotOut | None:
        if market_id is None:
            return None
        capture = s.scalars(
            select(MarketRulesCapture).where(
                MarketRulesCapture.market_id == market_id,
                MarketRulesCapture.snapshot_id == snapshot_id,
            )
        ).first()
        return MarketSnapshotOut(
            market_id=market_id,
            title=titles.get(market_id),
            resolution_rules=capture.resolution_rules_text if capture else None,
        )

    def _rejected_markets(
        self,
        s: Session,
        fit_card_id: uuid.UUID,
        *,
        titles: dict[str, str | None],
    ) -> list[RejectedMarketUiOut]:
        rec = s.scalar(
            select(MarketRecommendation).where(
                MarketRecommendation.fit_card_id == fit_card_id
            )
        )
        # Unbound legacy cards fail closed instead of borrowing another run's
        # rejection evidence through a thesis-wide "latest" query.
        if rec is None:
            return []
        rows = s.scalars(
            select(RejectedMarketRow).where(
                RejectedMarketRow.market_recommendation_id == rec.id
            )
        ).all()
        return [
            RejectedMarketUiOut(
                market_id=r.market_id,
                title=titles.get(r.market_id),
                reason=r.reason,
            )
            for r in rows
        ]

    # --- no-clean draft preview ---------------------------------------------
    def draft_preview(self, fit_card_id: uuid.UUID) -> DraftPreviewOut:
        self._require_fit_card_access(fit_card_id)
        outcome = self._s.draft.generate(fit_card_id)
        if not outcome.generated:
            return self._guard(
                DraftPreviewOut(generated=False, gate_verdict=outcome.gate_verdict)
            )
        from el.domain.tables import DraftContract

        with self._sessions() as s:
            d = s.get(DraftContract, outcome.draft_contract_id)
            return self._guard(
                DraftPreviewOut(
                    generated=True,
                    gate_verdict="pass",
                    label="shape-valid draft candidate",
                    draft_contract_id=d.id,
                    proposed_title=d.proposed_title,
                    proposed_resolution_logic=d.proposed_resolution_logic,
                    resolution_source=d.resolution_source,
                    resolution_deadline=(
                        d.resolution_deadline.isoformat()
                        if d.resolution_deadline
                        else None
                    ),
                    subject_entity=d.subject_entity,
                )
            )

    # --- S4 save ------------------------------------------------------------
    def save_ledger_entry(
        self,
        fit_card_id: uuid.UUID,
        *,
        conviction_level: str,
        intended_exposure_bucket: str,
        user_justification: str,
    ) -> LedgerEntryUiOut:
        self._require_fit_card_access(fit_card_id)
        outcome = self._s.ledger.create_ledger_entry(
            fit_card_id,
            user_id=self._actor.user_id,
            client_type=self._actor.client_type,
            actor_id=self._actor.actor_id,
            client_ref=CLIENT_REF,
            conviction_level=ConvictionLevel(conviction_level),
            intended_exposure_bucket=ExposureBucket(intended_exposure_bucket),
            user_justification=user_justification,
            agent_client_id=self._actor.agent_client_id,
        )
        if not outcome.saved and outcome.status == "rejected_incomplete":
            raise SaveRejected(outcome.violations)
        return self.get_ledger_entry(outcome.ledger_entry_id)

    # --- S5 read-back ---------------------------------------------------------
    def get_ledger_entry(self, ledger_entry_id: uuid.UUID) -> LedgerEntryUiOut:
        with self._sessions() as s:
            entry = s.get(LedgerEntry, ledger_entry_id)
            if entry is None or entry.user_id != self._actor.user_id:
                raise NotFound("ledger_entry not found")
            return self._guard(self._to_ledger_out(s, entry))

    def list_ledger_entries(self) -> list[LedgerEntryUiOut]:
        with self._sessions() as s:
            rows = s.scalars(
                select(LedgerEntry)
                .where(LedgerEntry.user_id == self._actor.user_id)
                .order_by(LedgerEntry.created_at.desc())
            ).all()
            return [self._guard(self._to_ledger_out(s, e)) for e in rows]

    def _to_ledger_out(self, s: Session, entry: LedgerEntry) -> LedgerEntryUiOut:
        # Titles are not persisted (frozen-snapshot record data); read-back
        # shows the durable market_id and leaves the title best-effort empty.
        snap = None
        return LedgerEntryUiOut(
            id=entry.id,
            thesis_analysis_id=entry.thesis_analysis_id,
            thesis_summary=entry.thesis_summary,
            user_justification=entry.user_justification,
            linked_market_id=entry.linked_market_id,
            linked_market_title=snap.title if snap else None,
            odds_at_entry=entry.odds_at_entry,
            odds_at_entry_side=entry.odds_at_entry_side,
            fit_class=entry.fit_class,
            attestation_status=entry.attestation_status,
            status=entry.status,
            client_type=entry.client_type,
            created_at=entry.created_at.isoformat() if entry.created_at else None,
        )

    # --- correction loop (docs/correction-loop-ui-contract-v0.md) ----------
    # Mirrors the MCP _intake semantics for the human actor: mint a PENDING
    # review_candidates row, dedupe on identical payloads, mutate NOTHING
    # else. Candidates leave pending only via the existing human triage
    # machinery, which this surface never calls.
    def correct_fit(
        self, fit_card_id: uuid.UUID, *, corrected_class: str, note: str
    ) -> "CorrectionOut":
        if not note.strip():
            raise ValueError("note is required")
        corrected = FitClass(corrected_class)  # closed vocabulary or ValueError
        self._require_fit_card_access(fit_card_id)
        return self._intake(
            object_type="fit_card",
            object_id=fit_card_id,
            source=ReviewSource.USER_CORRECTION,
            payload={"corrected_class": corrected.value, "notes": note.strip()},
        )

    def reject_market(
        self, fit_card_id: uuid.UUID, *, market_id: str, reason: str
    ) -> "CorrectionOut":
        if not reason.strip():
            raise ValueError("reason is required")
        self._require_fit_card_access(fit_card_id)
        with self._sessions() as s:
            card = s.get(FitCard, fit_card_id)
            surfaced = set()
            if card.recommended_market_id:
                surfaced.add(card.recommended_market_id)
            rec = s.scalar(
                select(MarketRecommendation).where(
                    MarketRecommendation.fit_card_id == card.id
                )
            )
            if rec is not None:
                surfaced.update(
                    r.market_id
                    for r in s.scalars(
                        select(RejectedMarketRow).where(
                            RejectedMarketRow.market_recommendation_id == rec.id
                        )
                    )
                )
        if market_id not in surfaced:
            raise ValueError("market was not part of this card")
        return self._intake(
            object_type="market_rejection",
            object_id=fit_card_id,
            source=ReviewSource.USER_REJECTION,
            payload={"market_id": market_id, "reason": reason.strip()},
        )

    def _intake(
        self, *, object_type: str, object_id: uuid.UUID, source: ReviewSource,
        payload: dict,
    ) -> "CorrectionOut":
        # Deterministic note encoding, matching the MCP intake shape, so the
        # Step-8 queue sees one provenance format regardless of surface.
        note = json.dumps(
            {
                "actor_id": self._actor.actor_id,
                "client_type": self._actor.client_type.value,
                **{k: v for k, v in payload.items() if v is not None},
            },
            sort_keys=True,
        )
        with self._sessions() as s:
            for c in s.scalars(
                select(ReviewCandidate).where(
                    ReviewCandidate.object_type == object_type,
                    ReviewCandidate.object_id == object_id,
                    ReviewCandidate.source == source.value,
                )
            ).all():
                if c.reviewer_notes == note:
                    return self._guard(
                        CorrectionOut(
                            review_candidate_id=c.id,
                            status=c.status,
                            already_recorded=True,
                        )
                    )
            row = ReviewCandidate(
                object_type=object_type,
                object_id=object_id,
                source=source.value,
                status=ReviewStatus.PENDING.value,
                reviewer_notes=note,
            )
            s.add(row)
            s.flush()
            result = CorrectionOut(
                review_candidate_id=row.id,
                status=ReviewStatus.PENDING.value,
                already_recorded=False,
            )
            s.commit()
            return self._guard(result)

    def stats(self) -> dict[str, Any]:
        with self._sessions() as s:
            return {
                "mode": self._s.mode,
                "fixture_source_examples": (
                    [MULTI_THESIS_FIXTURE]
                    if self._s.mode == "fixture"
                    else []
                ),
                "fixture_clarification_examples": (
                    [
                        {
                            "question": HARBOR_CLARIFICATION_QUESTION,
                            "answer": HARBOR_CLARIFICATION_ANSWER,
                        },
                        {
                            "question": ORCHARD_CLARIFICATION_QUESTION,
                            "answer": ORCHARD_CLARIFICATION_ANSWER,
                        },
                    ]
                    if self._s.mode == "fixture"
                    else []
                ),
                "ledger_entries": s.scalar(
                    select(func.count())
                    .select_from(LedgerEntry)
                    .where(LedgerEntry.user_id == self._actor.user_id)
                )
                or 0,
                "review_candidates": s.scalar(
                    select(func.count()).select_from(ReviewCandidate)
                )
                or 0,
            }
