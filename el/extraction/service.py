"""Loop 1 service: screen -> propose -> gate -> persist on PASS.

Ordering is load-bearing: the insider screen runs BEFORE the proposer,
so nonpublic text is refused without ever being sent to a model, and
refused/ambiguous/invalid inputs are never persisted (blueprint §11).
"""

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from el.domain.contracts import ThesisAnalysisIn, ThesisAnalysisOut
from el.domain.tables import SourceSignal, ThesisAnalysis
from el.extraction.gate import (
    GateVerdict,
    Loop1Result,
    insider_screen,
    normalized_claim_gate,
)
from el.models.adapter import ProposerAdapter


class ExtractionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Loop1Result
    analysis: ThesisAnalysisOut | None = None
    model_adapter: str | None = None
    model_run_id: str | None = None


class ExtractionService:
    def __init__(
        self,
        proposer: ProposerAdapter,
        session_factory: sessionmaker[Session],
    ):
        self._proposer = proposer
        self._sessions = session_factory

    def analyze(self, request: ThesisAnalysisIn) -> ExtractionOutcome:
        if insider_screen(request.input_text):
            return ExtractionOutcome(
                result=Loop1Result(
                    verdict=GateVerdict.REFUSED_INSIDER,
                    reasons=["input matched nonpublic-information screen"],
                )
            )

        proposed = self._proposer.propose_extraction(request.input_text)
        result = normalized_claim_gate(request.input_text, proposed.proposal)

        if result.verdict is not GateVerdict.PASS:
            return ExtractionOutcome(
                result=result,
                model_adapter=proposed.model_adapter,
                model_run_id=proposed.model_run_id,
            )

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
            model_adapter=proposed.model_adapter,
            model_run_id=proposed.model_run_id,
        )
