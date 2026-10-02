"""Loop 2 service: load structure -> retrieve -> rank -> gate -> persist.

Persisted, not transient (blueprint §5): retrieval-recall evals,
rejected-market auditability, and debugging all read from
candidate_sets / candidate_set_members. Every retrieved candidate lands
with rank, score, eligibility flags, and excluded_reason — eligible
candidates rank first, ineligible ones follow (never silently dropped).

Snapshot identity is get-or-create: a snapshot_id seen twice keeps its
first row (snapshot identity is immutable); rules captures are unique
per (market_id, snapshot_id).
"""

import uuid
from datetime import datetime
from functools import wraps
from hashlib import sha256

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.structures import ExtractedStructure
from el.domain.tables import (
    CandidateSet,
    CandidateSetMember,
    MarketRulesCapture,
    MarketSnapshot,
    ThesisAnalysis,
)
from el.observability import get_event_emitter, get_telemetry
from el.retrieval.gate import (
    GATE_POLICY_VERSION,
    EligibilityVerdict,
    Loop2Policy,
    evaluate_eligibility,
)
from el.retrieval.provider import (
    CandidateMarketRecord,
    MarketProvider,
)
from el.retrieval.ranking import RANKING_POLICY_VERSION, rank_candidates
from el.retrieval.scope import candidate_set_retrieval_scope


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CandidateOut(_Out):
    market_id: str
    title: str
    rank: int
    retrieval_score: float
    eligible: bool
    eligibility_flags: dict[str, str]
    excluded_reason: str | None = None


class RetrievalOutcome(_Out):
    candidate_set_id: uuid.UUID
    thesis_analysis_id: uuid.UUID
    snapshot_id: str
    retrieval_id: str
    as_of_ts: datetime
    mode: str
    candidates: list[CandidateOut]
    retrieved_count: int
    eligible_count: int
    gate_policy_version: str = GATE_POLICY_VERSION
    ranking_policy_version: str = RANKING_POLICY_VERSION


class RetrievalProviderFailure(RuntimeError):
    """Safe current-runtime boundary for a provider failure.

    The historic provider implementation remains byte-pinned for governed
    holdout replay. Product retrieval deliberately removes its raw exception
    chain before it can reach an API error handler or another logging sink.
    """


def _is_legacy_polydata_provider(provider: MarketProvider) -> bool:
    """Identify the private legacy provider without importing it publicly.

    The public candidate deliberately replaces the live provider module, so
    importing its private-only class here would make a direct shared service
    file unimportable. Only that historic implementation can emit raw provider
    exception detail; other provider-specific, bounded domain outcomes must
    preserve their own behavior.
    """

    provider_type = type(provider)
    return (
        provider_type.__module__ == "el.retrieval.provider"
        and provider_type.__name__ == "PolyDataMarketProvider"
    )


def _trace_retrieval(method):
    @wraps(method)
    def _wrapped(*args, **kwargs):
        telemetry = get_telemetry()
        with telemetry.span("fitcheck.retrieval.fetch") as span:
            span.set_attributes({"fitcheck.operation": "retrieval"})
            try:
                result = method(*args, **kwargs)
            except RetrievalProviderFailure:
                span.set_attributes({"fitcheck.outcome": "failed"})
                get_event_emitter().emit(
                    event_name="fitcheck.retrieval.operation",
                    severity="ERROR",
                    operation="retrieval",
                    stage="operation",
                    task_outcome="failed",
                    traceparent=telemetry.current_traceparent(),
                    error_code="provider_retrieval_failed",
                )
                raise
            except Exception:
                span.set_attributes({"fitcheck.outcome": "failed"})
                get_event_emitter().emit(
                    event_name="fitcheck.retrieval.operation",
                    severity="ERROR",
                    operation="retrieval",
                    stage="operation",
                    task_outcome="failed",
                    traceparent=telemetry.current_traceparent(),
                )
                raise
            span.set_attributes({"fitcheck.outcome": "succeeded"})
            get_event_emitter().emit(
                event_name="fitcheck.retrieval.operation",
                severity="INFO",
                operation="retrieval",
                stage="operation",
                transport_outcome="read_succeeded",
                task_outcome="succeeded",
                traceparent=telemetry.current_traceparent(),
            )
            return result

    return _wrapped


class RetrievalService:
    def __init__(
        self,
        provider: MarketProvider,
        session_factory: sessionmaker[Session],
        policy: Loop2Policy = Loop2Policy(),
    ):
        self._provider = provider
        self._sessions = session_factory
        self._policy = policy

    @_trace_retrieval
    def retrieve_candidates(
        self, thesis_analysis_id: uuid.UUID
    ) -> RetrievalOutcome:
        with self._sessions() as session:
            analysis = session.get(ThesisAnalysis, thesis_analysis_id)
            if analysis is None:
                raise ValueError(
                    f"thesis_analysis {thesis_analysis_id} not found"
                )
            structure = ExtractedStructure.model_validate(
                analysis.extracted_structure
            )

            provider_failed = False
            try:
                result = self._provider.retrieve(structure)
            except Exception:
                # Leave the exception suite before raising the safe boundary:
                # ``raise ... from None`` suppresses rendering but retains
                # ``__context__`` for a later unsafe logger.
                if _is_legacy_polydata_provider(self._provider):
                    provider_failed = True
                else:
                    raise
            if provider_failed:
                raise RetrievalProviderFailure("market provider retrieval failed")
            ranked = rank_candidates(structure, result.markets)
            verdicts = [
                evaluate_eligibility(record, structure, self._policy)
                for record, _ in ranked
            ]

            # Eligible candidates first (ranked order preserved within
            # each group); rank is 1-based over the final order.
            ordered = [
                (record, score, verdict)
                for (record, score), verdict in zip(ranked, verdicts)
                if verdict.eligible
            ] + [
                (record, score, verdict)
                for (record, score), verdict in zip(ranked, verdicts)
                if not verdict.eligible
            ]

            self._ensure_snapshot(session, result)
            candidate_set = CandidateSet(
                thesis_analysis_id=thesis_analysis_id,
                snapshot_id=result.snapshot_id,
                retrieval_id=result.retrieval_id,
                retrieval_scope=candidate_set_retrieval_scope(
                    result,
                    gate_eligible_count=sum(
                        1 for verdict in verdicts if verdict.eligible
                    ),
                ).model_dump(mode="json"),
            )
            session.add(candidate_set)
            session.flush()

            candidates: list[CandidateOut] = []
            for rank, (record, score, verdict) in enumerate(ordered, start=1):
                session.add(
                    CandidateSetMember(
                        candidate_set_id=candidate_set.id,
                        market_id=record.market_id,
                        rank=rank,
                        retrieval_score=score,
                        # Frozen YES probability as of this snapshot (step 6):
                        # the substrate for odds-at-entry, captured once.
                        current_probability=record.current_probability,
                        eligibility_flags=verdict.flags,
                        excluded_reason=verdict.excluded_reason,
                    )
                )
                self._ensure_rules_capture(session, record, result.snapshot_id)
                candidates.append(
                    CandidateOut(
                        market_id=record.market_id,
                        title=record.title,
                        rank=rank,
                        retrieval_score=score,
                        eligible=verdict.eligible,
                        eligibility_flags=verdict.flags,
                        excluded_reason=verdict.excluded_reason,
                    )
                )
            session.commit()
            candidate_set_id = candidate_set.id

        return RetrievalOutcome(
            candidate_set_id=candidate_set_id,
            thesis_analysis_id=thesis_analysis_id,
            snapshot_id=result.snapshot_id,
            retrieval_id=result.retrieval_id,
            as_of_ts=result.as_of_ts,
            mode=result.mode,
            candidates=candidates,
            retrieved_count=len(candidates),
            eligible_count=sum(1 for c in candidates if c.eligible),
        )

    def _ensure_snapshot(self, session: Session, result) -> None:
        if session.get(MarketSnapshot, result.snapshot_id) is None:
            session.add(
                MarketSnapshot(
                    id=result.snapshot_id,
                    venue_id=result.mode,
                    as_of_ts=result.as_of_ts,
                    retrieval_id=result.retrieval_id,
                )
            )
            session.flush()

    def _ensure_rules_capture(
        self,
        session: Session,
        record: CandidateMarketRecord,
        snapshot_id: str,
    ) -> None:
        exists = session.execute(
            select(MarketRulesCapture.id).where(
                MarketRulesCapture.market_id == record.market_id,
                MarketRulesCapture.snapshot_id == snapshot_id,
            )
        ).first()
        if exists:
            return
        session.add(
            MarketRulesCapture(
                market_id=record.market_id,
                snapshot_id=snapshot_id,
                contract_terms_text=record.title,
                resolution_rules_text=record.resolution_rules,
                contract_terms_hash=_sha(record.title),
                resolution_rules_hash=_sha(record.resolution_rules),
            )
        )


def _sha(text: str) -> str:
    return sha256(text.encode()).hexdigest()
