"""Loop 1 service: screen -> propose -> gate -> governed persistence.

Ordering is load-bearing: the insider screen runs BEFORE the proposer,
so nonpublic text is refused without ever being sent to a model. Legacy
``analyze`` persists only PASS; the v3 proposal path persists bounded attempt
evidence but strips raw input and URL from pre-model safety refusals.
"""

import hashlib
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.contracts import ThesisAnalysisIn, ThesisAnalysisOut
from el.domain.structures import ExtractedStructure
from el.domain.tables import (
    NormalizationAttempt,
    NormalizationDecision,
    SourceSignal,
    ThesisAnalysis,
)
from el.extraction.gate import (
    GATE_POLICY_VERSION as LOOP1_V1_GATE_POLICY_VERSION,
    GateVerdict,
    Loop1Result,
    insider_screen as insider_screen_v1,
    normalized_claim_gate as normalized_claim_gate_v1,
)
from el.extraction.gate_v2 import (
    GATE_POLICY_VERSION as LOOP1_V2_GATE_POLICY_VERSION,
    Loop1V2Result,
    insider_screen as insider_screen_v2,
    normalized_claim_gate as normalized_claim_gate_v2,
)
from el.models.adapter import EXTRACTION_PROMPT_POLICY_VERSION, ProposerAdapter
from el.models.adapter_v2 import (
    EXTRACTION_PROMPT_POLICY_VERSION_V2_READY,
    ExtractionProposalV2,
    ModelOutputValidationFailure,
)


class ExtractionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Loop1Result | Loop1V2Result
    analysis: ThesisAnalysisOut | None = None
    model_adapter: str | None = None
    model_run_id: str | None = None


class NormalizationAttemptOutcome(BaseModel):
    """Immutable v3 proposal evidence; never an accepted thesis by itself."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: uuid.UUID
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
    created_at: datetime


class NormalizationDecisionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: uuid.UUID
    attempt_id: uuid.UUID
    action: str
    thesis_analysis_id: uuid.UUID | None
    created_at: datetime


class NormalizationAttemptNotFound(Exception):
    pass


class NormalizationAttemptConflict(Exception):
    pass


class ExtractionService:
    def __init__(
        self,
        proposer: ProposerAdapter,
        session_factory: sessionmaker[Session],
        *,
        gate_policy_version: str = LOOP1_V1_GATE_POLICY_VERSION,
    ):
        if gate_policy_version not in {
            LOOP1_V1_GATE_POLICY_VERSION,
            LOOP1_V2_GATE_POLICY_VERSION,
        }:
            raise ValueError("unknown Loop 1 gate policy version")
        self._proposer = proposer
        self._sessions = session_factory
        self._gate_policy_version = gate_policy_version
        default_prompt_policy = (
            EXTRACTION_PROMPT_POLICY_VERSION_V2_READY
            if gate_policy_version == LOOP1_V2_GATE_POLICY_VERSION
            else EXTRACTION_PROMPT_POLICY_VERSION
        )
        self._prompt_policy_version = getattr(
            proposer, "prompt_policy_version", default_prompt_policy
        )
        self._system_variant_id = (
            "fitcheck-normalization/"
            f"{self._prompt_policy_version}/{self._gate_policy_version}"
        )

    def _evaluate(self, request: ThesisAnalysisIn) -> ExtractionOutcome:
        screen = (
            insider_screen_v2
            if self._gate_policy_version == LOOP1_V2_GATE_POLICY_VERSION
            else insider_screen_v1
        )
        if screen(request.input_text):
            refusal = (
                Loop1V2Result(
                    verdict=GateVerdict.REFUSED_INSIDER,
                    reasons=["input_matched_nonpublic_information_screen"],
                )
                if self._gate_policy_version == LOOP1_V2_GATE_POLICY_VERSION
                else Loop1Result(
                    verdict=GateVerdict.REFUSED_INSIDER,
                    reasons=["input matched nonpublic-information screen"],
                )
            )
            return ExtractionOutcome(result=refusal)

        try:
            proposed = self._proposer.propose_extraction(request.input_text)
        except ModelOutputValidationFailure as error:
            refusal = (
                Loop1V2Result(
                    verdict=GateVerdict.REJECTED_INVALID,
                    reasons=list(error.reasons),
                )
                if self._gate_policy_version == LOOP1_V2_GATE_POLICY_VERSION
                else Loop1Result(
                    verdict=GateVerdict.REJECTED_INVALID,
                    reasons=list(error.reasons),
                )
            )
            return ExtractionOutcome(
                result=refusal,
                model_adapter=error.model_adapter,
                model_run_id=error.model_run_id,
            )
        if (
            self._gate_policy_version == LOOP1_V2_GATE_POLICY_VERSION
            and not isinstance(proposed.proposal, ExtractionProposalV2)
        ):
            return ExtractionOutcome(
                result=Loop1V2Result(
                    verdict=GateVerdict.REJECTED_INVALID,
                    reasons=["v2_proposal_contract_mismatch"],
                ),
                model_adapter=proposed.model_adapter,
                model_run_id=proposed.model_run_id,
            )
        gate = (
            normalized_claim_gate_v2
            if self._gate_policy_version == LOOP1_V2_GATE_POLICY_VERSION
            else normalized_claim_gate_v1
        )
        result = gate(request.input_text, proposed.proposal)

        return ExtractionOutcome(
            result=result,
            model_adapter=proposed.model_adapter,
            model_run_id=proposed.model_run_id,
        )

    def analyze(self, request: ThesisAnalysisIn) -> ExtractionOutcome:
        """Legacy Loop 1 behavior: a gate PASS immediately persists analysis."""

        outcome = self._evaluate(request)
        result = outcome.result
        if result.verdict is not GateVerdict.PASS:
            return outcome

        assert result.structure is not None
        with self._sessions() as session:
            source_signal_id = None
            if request.source_url:
                signal = SourceSignal(source_type="url", url=request.source_url)
                session.add(signal)
                session.flush()
                source_signal_id = signal.id
            row = ThesisAnalysis(
                source_signal_id=source_signal_id,
                input_text=request.input_text,
                extracted_structure=result.structure.model_dump(mode="json"),
                schema_version=result.structure.schema_version,
                normalized_claim_summary=result.structure.claim_summary,
                client_type=request.client_type.value,
                agent_client_id=request.agent_client_id,
            )
            session.add(row)
            session.commit()
            analysis = ThesisAnalysisOut(
                id=row.id,
                input_text=row.input_text,
                extracted_structure=result.structure,
                normalized_claim_summary=row.normalized_claim_summary,
                client_type=request.client_type,
                created_at=row.created_at,
            )

        return ExtractionOutcome(
            result=result,
            analysis=analysis,
            model_adapter=outcome.model_adapter,
            model_run_id=outcome.model_run_id,
        )

    @staticmethod
    def _input_digest(input_text: str) -> str:
        return hashlib.sha256(input_text.encode("utf-8")).hexdigest()

    @staticmethod
    def _attempt_out(row: NormalizationAttempt) -> NormalizationAttemptOutcome:
        structure = row.proposal.get("structure") if row.proposal else None
        return NormalizationAttemptOutcome(
            attempt_id=row.id,
            predecessor_attempt_id=row.predecessor_attempt_id,
            source_interpretation_id=row.source_interpretation_id,
            source_thesis_candidate_id=row.source_thesis_candidate_id,
            outcome=row.outcome,
            verdict=row.verdict,
            input_digest=row.input_digest,
            normalized_claim_summary=(
                structure.get("claim_summary") if structure else None
            ),
            extracted_structure=structure,
            clarifying_question=row.clarifying_question,
            reasons=list(row.reasons),
            gate_policy_version=row.gate_policy_version,
            prompt_policy_version=row.prompt_policy_version,
            system_variant_id=row.system_variant_id,
            model_adapter=row.model_adapter,
            model_run_id=row.model_run_id,
            created_at=row.created_at,
        )

    @staticmethod
    def _decision_out(
        row: NormalizationDecision,
    ) -> NormalizationDecisionOutcome:
        return NormalizationDecisionOutcome(
            decision_id=row.id,
            attempt_id=row.attempt_id,
            action=row.action,
            thesis_analysis_id=row.thesis_analysis_id,
            created_at=row.created_at,
        )

    def _attempt_values(
        self,
        request: ThesisAnalysisIn,
        outcome: ExtractionOutcome,
        *,
        predecessor_attempt_id: uuid.UUID | None,
        source_interpretation_id: uuid.UUID | None = None,
        source_thesis_candidate_id: uuid.UUID | None = None,
    ) -> dict:
        result = outcome.result
        if result.verdict is GateVerdict.PASS:
            attempt_outcome = "candidate"
        elif result.verdict is GateVerdict.NEEDS_CLARIFICATION:
            attempt_outcome = "clarification"
        else:
            attempt_outcome = "refusal"

        # The pre-model MNPI refusal is useful governed evidence, but retaining
        # the user's sensitive text or URL would defeat the safety boundary.
        retain_input = result.verdict is not GateVerdict.REFUSED_INSIDER
        structure = (
            result.structure.model_dump(mode="json")
            if result.structure is not None and attempt_outcome != "refusal"
            else None
        )
        return {
            "predecessor_attempt_id": predecessor_attempt_id,
            "source_interpretation_id": source_interpretation_id,
            "source_thesis_candidate_id": source_thesis_candidate_id,
            "input_text": request.input_text if retain_input else None,
            "input_digest": (
                self._input_digest(request.input_text) if retain_input else None
            ),
            "source_url": request.source_url if retain_input else None,
            "outcome": attempt_outcome,
            "verdict": result.verdict.value,
            "proposal": {"structure": structure} if structure else None,
            "clarifying_question": result.clarifying_question,
            "reasons": list(result.reasons),
            "gate_policy_version": result.gate_policy_version,
            "prompt_policy_version": self._prompt_policy_version,
            "system_variant_id": self._system_variant_id,
            "model_adapter": outcome.model_adapter,
            "model_run_id": outcome.model_run_id,
            "client_type": request.client_type.value,
            "agent_client_id": request.agent_client_id,
        }

    def propose(self, request: ThesisAnalysisIn) -> NormalizationAttemptOutcome:
        """Persist a proposal attempt without creating ``ThesisAnalysis``."""

        if not request.agent_client_id:
            raise ValueError("v3 normalization requires an actor identity")
        outcome = self._evaluate(request)
        with self._sessions() as session:
            row = NormalizationAttempt(
                **self._attempt_values(
                    request, outcome, predecessor_attempt_id=None
                )
            )
            session.add(row)
            session.commit()
            return self._attempt_out(row)

    def propose_selected(
        self,
        request: ThesisAnalysisIn,
        *,
        source_interpretation_id: uuid.UUID,
        source_thesis_candidate_id: uuid.UUID,
    ) -> NormalizationAttemptOutcome:
        """Normalize one explicitly selected source candidate, at most once."""

        if not request.agent_client_id:
            raise ValueError("v3 normalization requires an actor identity")
        with self._sessions() as session:
            existing = session.scalar(
                select(NormalizationAttempt).where(
                    NormalizationAttempt.source_thesis_candidate_id
                    == source_thesis_candidate_id
                )
            )
            if existing is not None:
                if (
                    existing.source_interpretation_id
                    != source_interpretation_id
                    or existing.client_type != request.client_type.value
                    or existing.agent_client_id != request.agent_client_id
                    or existing.input_digest
                    != self._input_digest(request.input_text)
                ):
                    raise NormalizationAttemptConflict(
                        "selected candidate normalization binding differs"
                    )
                return self._attempt_out(existing)

        outcome = self._evaluate(request)
        with self._sessions() as session:
            row = NormalizationAttempt(
                **self._attempt_values(
                    request,
                    outcome,
                    predecessor_attempt_id=None,
                    source_interpretation_id=source_interpretation_id,
                    source_thesis_candidate_id=source_thesis_candidate_id,
                )
            )
            session.add(row)
            session.commit()
            return self._attempt_out(row)

    def _owned_attempt(
        self,
        session: Session,
        attempt_id: uuid.UUID,
        *,
        client_type: str,
        agent_client_id: str,
    ) -> NormalizationAttempt:
        row = session.get(NormalizationAttempt, attempt_id)
        if (
            row is None
            or row.client_type != client_type
            or row.agent_client_id != agent_client_id
        ):
            raise NormalizationAttemptNotFound()
        return row

    @staticmethod
    def _require_digest(
        row: NormalizationAttempt, expected_input_digest: str | None
    ) -> None:
        if row.input_digest != expected_input_digest:
            raise NormalizationAttemptConflict("normalization attempt is stale")

    def accept(
        self,
        attempt_id: uuid.UUID,
        *,
        client_type: str,
        agent_client_id: str,
        actor_id: str,
        expected_input_digest: str | None,
    ) -> NormalizationDecisionOutcome:
        with self._sessions() as session:
            attempt = self._owned_attempt(
                session,
                attempt_id,
                client_type=client_type,
                agent_client_id=agent_client_id,
            )
            self._require_digest(attempt, expected_input_digest)
            existing = session.scalar(
                select(NormalizationDecision).where(
                    NormalizationDecision.attempt_id == attempt.id
                )
            )
            if existing is not None:
                if existing.action == "accept":
                    return self._decision_out(existing)
                raise NormalizationAttemptConflict(
                    "normalization attempt already has a different decision"
                )
            if attempt.outcome != "candidate" or not attempt.proposal:
                raise NormalizationAttemptConflict(
                    "only a candidate normalization can be accepted"
                )
            if attempt.input_text is None:
                raise NormalizationAttemptConflict(
                    "candidate input was not retained"
                )

            structure = ExtractedStructure.model_validate(
                attempt.proposal["structure"]
            )
            source_signal_id = None
            if attempt.source_url:
                signal = SourceSignal(source_type="url", url=attempt.source_url)
                session.add(signal)
                session.flush()
                source_signal_id = signal.id
            analysis = ThesisAnalysis(
                source_signal_id=source_signal_id,
                input_text=attempt.input_text,
                extracted_structure=structure.model_dump(mode="json"),
                schema_version=structure.schema_version,
                normalized_claim_summary=structure.claim_summary,
                client_type=attempt.client_type,
                agent_client_id=attempt.agent_client_id,
            )
            session.add(analysis)
            session.flush()
            decision = NormalizationDecision(
                attempt_id=attempt.id,
                action="accept",
                thesis_analysis_id=analysis.id,
                actor_id=actor_id,
            )
            session.add(decision)
            session.commit()
            return self._decision_out(decision)

    def reject(
        self,
        attempt_id: uuid.UUID,
        *,
        client_type: str,
        agent_client_id: str,
        actor_id: str,
        expected_input_digest: str | None,
        reason: str | None = None,
    ) -> NormalizationDecisionOutcome:
        with self._sessions() as session:
            attempt = self._owned_attempt(
                session,
                attempt_id,
                client_type=client_type,
                agent_client_id=agent_client_id,
            )
            self._require_digest(attempt, expected_input_digest)
            existing = session.scalar(
                select(NormalizationDecision).where(
                    NormalizationDecision.attempt_id == attempt.id
                )
            )
            if existing is not None:
                if existing.action == "reject":
                    return self._decision_out(existing)
                raise NormalizationAttemptConflict(
                    "normalization attempt already has a different decision"
                )
            decision = NormalizationDecision(
                attempt_id=attempt.id,
                action="reject",
                thesis_analysis_id=None,
                actor_id=actor_id,
                reason=reason,
            )
            session.add(decision)
            session.commit()
            return self._decision_out(decision)

    def revise(
        self,
        predecessor_attempt_id: uuid.UUID,
        request: ThesisAnalysisIn,
        *,
        actor_id: str,
        expected_input_digest: str | None,
    ) -> NormalizationAttemptOutcome:
        if not request.agent_client_id:
            raise ValueError("v3 normalization requires an actor identity")

        # Check ownership and staleness before spending a model call.
        with self._sessions() as session:
            predecessor = self._owned_attempt(
                session,
                predecessor_attempt_id,
                client_type=request.client_type.value,
                agent_client_id=request.agent_client_id,
            )
            self._require_digest(predecessor, expected_input_digest)
            if predecessor.input_digest is None:
                raise NormalizationAttemptConflict(
                    "a safety refusal cannot be revised in place"
                )
            existing = session.scalar(
                select(NormalizationDecision).where(
                    NormalizationDecision.attempt_id == predecessor.id
                )
            )
            if existing is not None:
                if existing.action == "edit":
                    successor = session.scalar(
                        select(NormalizationAttempt).where(
                            NormalizationAttempt.predecessor_attempt_id
                            == predecessor.id
                        )
                    )
                    if (
                        successor is not None
                        and successor.input_digest
                        == self._input_digest(request.input_text)
                    ):
                        return self._attempt_out(successor)
                raise NormalizationAttemptConflict(
                    "normalization attempt already has a decision"
                )

        outcome = self._evaluate(request)
        with self._sessions() as session:
            predecessor = self._owned_attempt(
                session,
                predecessor_attempt_id,
                client_type=request.client_type.value,
                agent_client_id=request.agent_client_id,
            )
            self._require_digest(predecessor, expected_input_digest)
            if session.scalar(
                select(NormalizationDecision.id).where(
                    NormalizationDecision.attempt_id == predecessor.id
                )
            ):
                raise NormalizationAttemptConflict(
                    "normalization attempt changed during revision"
                )
            successor = NormalizationAttempt(
                **self._attempt_values(
                    request,
                    outcome,
                    predecessor_attempt_id=predecessor.id,
                )
            )
            session.add(successor)
            session.flush()
            session.add(
                NormalizationDecision(
                    attempt_id=predecessor.id,
                    action="edit",
                    thesis_analysis_id=None,
                    actor_id=actor_id,
                )
            )
            session.commit()
            return self._attempt_out(successor)
