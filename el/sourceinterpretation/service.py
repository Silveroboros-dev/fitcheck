"""Source text -> candidates -> explicit human selection.

The model supplies bounded candidate evidence. Deterministic checks enforce
exact quotation, source order, uniqueness and vocabulary. A separate immutable
row records the human choice; no candidate is silently promoted to a thesis.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.tables import (
    SourceCandidateChoice,
    SourceInterpretation,
    SourceThesisCandidate,
)
from el.jobs import FencedWriteSession
from el.domain.vocabulary import vocabulary_violations
from el.extraction.gate_v2 import insider_screen
from el.models.source_adapter import (
    SOURCE_INTERPRETATION_PROMPT_POLICY_VERSION,
    SourceInterpreterAdapter,
)


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceCandidateOutcome(_Out):
    source_thesis_candidate_id: uuid.UUID
    ordinal: int
    selected_source_quote: str
    source_quote_digest: str
    claim_summary: str
    created_at: datetime


class SourceInterpretationOutcome(_Out):
    source_interpretation_id: uuid.UUID
    source_interpretation_request_id: uuid.UUID | None
    job_id: uuid.UUID | None
    outcome: str
    input_digest: str | None
    reasons: list[str]
    prompt_policy_version: str
    system_variant_id: str
    model_adapter: str | None
    model_run_id: str | None
    candidates: list[SourceCandidateOutcome]
    created_at: datetime


class SourceCandidateChoiceOutcome(_Out):
    source_candidate_choice_id: uuid.UUID
    source_interpretation_id: uuid.UUID
    selection_kind: str
    source_thesis_candidate_id: uuid.UUID | None
    created_at: datetime


class SelectedSourceCandidate(_Out):
    source_interpretation_id: uuid.UUID
    source_thesis_candidate_id: uuid.UUID
    selected_source_quote: str
    source_url: str | None


class ComputedSourceCandidate(_Out):
    ordinal: int
    selected_source_quote: str
    source_quote_digest: str
    claim_summary: str


class SourceInterpretationComputation(_Out):
    input_text: str | None
    input_digest: str | None
    source_url: str | None
    outcome: str
    reasons: list[str]
    prompt_policy_version: str
    system_variant_id: str
    model_adapter: str | None
    model_run_id: str | None
    candidates: list[ComputedSourceCandidate]


class SourceInterpretationNotFound(Exception):
    pass


class SourceCandidateChoiceConflict(Exception):
    pass


class SourceCandidateValidationError(ValueError):
    """A returned candidate conflicts with deterministic source invariants."""


class SourceInterpretationService:
    def __init__(
        self,
        interpreter: SourceInterpreterAdapter,
        session_factory: sessionmaker[Session],
    ):
        self._interpreter = interpreter
        self._sessions = session_factory
        self._prompt_policy_version = getattr(
            interpreter,
            "prompt_policy_version",
            SOURCE_INTERPRETATION_PROMPT_POLICY_VERSION,
        )
        self._system_variant_id = (
            "fitcheck-source-interpretation/" + self._prompt_policy_version
        )

    def execution_pins(self) -> dict[str, str | bool | int]:
        return {
            "prompt_policy_version": self._prompt_policy_version,
            "system_variant_id": self._system_variant_id,
            "response_schema_version": 1,
            "adapter_kind": self._interpreter.adapter_kind,
            "model_id": self._interpreter.model_id,
            "has_external_effect": self._interpreter.has_external_effect,
            "runtime_contract_version": "source-interpretation-worker-v1",
        }

    @property
    def has_external_effect(self) -> bool:
        return self._interpreter.has_external_effect

    @staticmethod
    def digest_input(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _candidate_out(row: SourceThesisCandidate) -> SourceCandidateOutcome:
        return SourceCandidateOutcome(
            source_thesis_candidate_id=row.id,
            ordinal=row.ordinal,
            selected_source_quote=row.selected_source_quote,
            source_quote_digest=row.source_quote_digest,
            claim_summary=row.claim_summary,
            created_at=row.created_at,
        )

    def _out(
        self, session: Session, row: SourceInterpretation
    ) -> SourceInterpretationOutcome:
        candidates = session.scalars(
            select(SourceThesisCandidate)
            .where(SourceThesisCandidate.source_interpretation_id == row.id)
            .order_by(SourceThesisCandidate.ordinal)
        ).all()
        return SourceInterpretationOutcome(
            source_interpretation_id=row.id,
            source_interpretation_request_id=(
                row.source_interpretation_request_id
            ),
            job_id=row.job_id,
            outcome=row.outcome,
            input_digest=row.input_digest,
            reasons=list(row.reasons),
            prompt_policy_version=row.prompt_policy_version,
            system_variant_id=row.system_variant_id,
            model_adapter=row.model_adapter,
            model_run_id=row.model_run_id,
            candidates=[self._candidate_out(value) for value in candidates],
            created_at=row.created_at,
        )

    def privacy_refusal_computation(self) -> SourceInterpretationComputation:
        return SourceInterpretationComputation(
            input_text=None,
            input_digest=None,
            source_url=None,
            outcome="refusal",
            reasons=["input_matched_nonpublic_information_screen"],
            prompt_policy_version=self._prompt_policy_version,
            system_variant_id=self._system_variant_id,
            model_adapter=None,
            model_run_id=None,
            candidates=[],
        )

    def compute(
        self,
        input_text: str,
        *,
        source_url: str | None,
    ) -> SourceInterpretationComputation:
        if insider_screen(input_text):
            return self.privacy_refusal_computation()
        return self.compute_allowed(input_text, source_url=source_url)

    def compute_allowed(
        self,
        input_text: str,
        *,
        source_url: str | None,
    ) -> SourceInterpretationComputation:
        if insider_screen(input_text):
            raise SourceCandidateValidationError(
                "screened source cannot enter model computation"
            )
        proposed = self._interpreter.propose_candidates(input_text)
        proposal = proposed.proposal
        validated: list[ComputedSourceCandidate] = []
        cursor = 0
        seen_quotes: set[str] = set()
        for ordinal, candidate in enumerate(proposal.candidate_items, start=1):
            quote = candidate.selected_source_quote
            summary = candidate.claim_summary
            if not quote.strip() or not summary.strip():
                raise SourceCandidateValidationError(
                    "source candidates must contain non-empty text"
                )
            position = input_text.find(quote, cursor)
            if position < 0:
                raise SourceCandidateValidationError(
                    "source candidate quote is not an exact substring in source order"
                )
            digest = self.digest_input(quote)
            if digest in seen_quotes:
                raise SourceCandidateValidationError(
                    "source candidate quotes must be unique"
                )
            violations = vocabulary_violations(summary)
            if violations:
                raise SourceCandidateValidationError(
                    "source candidate summary violates A7 vocabulary: "
                    + ", ".join(violations)
                )
            seen_quotes.add(digest)
            cursor = position + len(quote)
            validated.append(
                ComputedSourceCandidate(
                    ordinal=ordinal,
                    selected_source_quote=quote,
                    source_quote_digest=digest,
                    claim_summary=summary,
                )
            )

        return SourceInterpretationComputation(
            input_text=input_text,
            input_digest=self.digest_input(input_text),
            source_url=source_url,
            outcome=proposal.mode.value,
            reasons=list(proposal.refusal_reasons),
            prompt_policy_version=self._prompt_policy_version,
            system_variant_id=self._system_variant_id,
            model_adapter=proposed.model_adapter,
            model_run_id=proposed.model_run_id,
            candidates=validated,
        )

    def persist_in_session(
        self,
        session: Session | FencedWriteSession,
        computation: SourceInterpretationComputation,
        *,
        client_type: str,
        agent_client_id: str,
        source_interpretation_request_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
    ) -> SourceInterpretationOutcome:
        if not agent_client_id:
            raise ValueError("source interpretation requires an actor identity")
        if (source_interpretation_request_id is None) != (job_id is None):
            raise ValueError("async source binding requires request and job ids")

        row = SourceInterpretation(
            source_interpretation_request_id=source_interpretation_request_id,
            job_id=job_id,
            input_text=computation.input_text,
            input_digest=computation.input_digest,
            source_url=computation.source_url,
            outcome=computation.outcome,
            reasons=list(computation.reasons),
            prompt_policy_version=computation.prompt_policy_version,
            system_variant_id=computation.system_variant_id,
            model_adapter=computation.model_adapter,
            model_run_id=computation.model_run_id,
            client_type=client_type,
            agent_client_id=agent_client_id,
        )
        session.add(row)
        session.flush()
        for candidate in computation.candidates:
            session.add(
                SourceThesisCandidate(
                    source_interpretation_id=row.id,
                    ordinal=candidate.ordinal,
                    selected_source_quote=candidate.selected_source_quote,
                    source_quote_digest=candidate.source_quote_digest,
                    claim_summary=candidate.claim_summary,
                )
            )
        session.flush()
        return self._out(session, row)

    def persist(
        self,
        computation: SourceInterpretationComputation,
        *,
        client_type: str,
        agent_client_id: str,
    ) -> SourceInterpretationOutcome:
        with self._sessions() as session:
            outcome = self.persist_in_session(
                session,
                computation,
                client_type=client_type,
                agent_client_id=agent_client_id,
            )
            session.commit()
            return outcome

    def interpret(
        self,
        input_text: str,
        *,
        source_url: str | None,
        client_type: str,
        agent_client_id: str,
    ) -> SourceInterpretationOutcome:
        return self.persist(
            self.compute(input_text, source_url=source_url),
            client_type=client_type,
            agent_client_id=agent_client_id,
        )

    def _owned_interpretation(
        self,
        session: Session,
        interpretation_id: uuid.UUID,
        *,
        client_type: str,
        agent_client_id: str,
    ) -> SourceInterpretation:
        row = session.get(SourceInterpretation, interpretation_id)
        if (
            row is None
            or row.client_type != client_type
            or row.agent_client_id != agent_client_id
        ):
            raise SourceInterpretationNotFound()
        return row

    def get_owned(
        self,
        interpretation_id: uuid.UUID,
        *,
        client_type: str,
        agent_client_id: str,
    ) -> SourceInterpretationOutcome:
        with self._sessions() as session:
            row = self._owned_interpretation(
                session,
                interpretation_id,
                client_type=client_type,
                agent_client_id=agent_client_id,
            )
            return self._out(session, row)

    @staticmethod
    def _choice_out(
        row: SourceCandidateChoice,
    ) -> SourceCandidateChoiceOutcome:
        return SourceCandidateChoiceOutcome(
            source_candidate_choice_id=row.id,
            source_interpretation_id=row.source_interpretation_id,
            selection_kind=row.selection_kind,
            source_thesis_candidate_id=row.source_thesis_candidate_id,
            created_at=row.created_at,
        )

    def choose(
        self,
        interpretation_id: uuid.UUID,
        *,
        selection_kind: str,
        source_thesis_candidate_id: uuid.UUID | None,
        actor_id: str,
        client_type: str,
        agent_client_id: str,
        reason: str | None = None,
    ) -> SourceCandidateChoiceOutcome:
        if selection_kind not in {"candidate", "none"}:
            raise ValueError("unsupported source candidate selection kind")
        if (selection_kind == "candidate") != (
            source_thesis_candidate_id is not None
        ):
            raise ValueError("source candidate selection shape is invalid")

        with self._sessions() as session:
            interpretation = self._owned_interpretation(
                session,
                interpretation_id,
                client_type=client_type,
                agent_client_id=agent_client_id,
            )
            if interpretation.outcome != "candidates":
                raise SourceCandidateChoiceConflict(
                    "a refused source interpretation cannot be selected"
                )
            if source_thesis_candidate_id is not None:
                candidate = session.get(
                    SourceThesisCandidate, source_thesis_candidate_id
                )
                if (
                    candidate is None
                    or candidate.source_interpretation_id != interpretation.id
                ):
                    raise SourceCandidateChoiceConflict(
                        "candidate is not displayed by this source interpretation"
                    )
            existing = session.scalar(
                select(SourceCandidateChoice).where(
                    SourceCandidateChoice.source_interpretation_id
                    == interpretation.id
                )
            )
            if existing is not None:
                if (
                    existing.selection_kind == selection_kind
                    and existing.source_thesis_candidate_id
                    == source_thesis_candidate_id
                ):
                    return self._choice_out(existing)
                raise SourceCandidateChoiceConflict(
                    "source interpretation already has a different choice"
                )
            choice = SourceCandidateChoice(
                source_interpretation_id=interpretation.id,
                selection_kind=selection_kind,
                source_thesis_candidate_id=source_thesis_candidate_id,
                actor_id=actor_id,
                client_type=client_type,
                reason=reason,
            )
            session.add(choice)
            session.commit()
            return self._choice_out(choice)

    def require_selected_candidate(
        self,
        candidate_id: uuid.UUID,
        *,
        client_type: str,
        agent_client_id: str,
    ) -> SelectedSourceCandidate:
        with self._sessions() as session:
            candidate = session.get(SourceThesisCandidate, candidate_id)
            if candidate is None:
                raise SourceInterpretationNotFound()
            interpretation = self._owned_interpretation(
                session,
                candidate.source_interpretation_id,
                client_type=client_type,
                agent_client_id=agent_client_id,
            )
            choice = session.scalar(
                select(SourceCandidateChoice).where(
                    SourceCandidateChoice.source_interpretation_id
                    == interpretation.id
                )
            )
            if (
                choice is None
                or choice.selection_kind != "candidate"
                or choice.source_thesis_candidate_id != candidate.id
            ):
                raise SourceCandidateChoiceConflict(
                    "candidate has not been selected by the human"
                )
            return SelectedSourceCandidate(
                source_interpretation_id=interpretation.id,
                source_thesis_candidate_id=candidate.id,
                selected_source_quote=candidate.selected_source_quote,
                source_url=interpretation.source_url,
            )
