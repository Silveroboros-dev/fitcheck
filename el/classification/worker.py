"""Private provider-independent classification submission and worker path."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.classification.contracts import (
    CandidateIndexPin,
    CandidateIndexPort,
    ClassificationPayload,
    ClassificationPinManifest,
    ClassificationWorkerResult,
    FitPins,
    RetrievalPins,
    SnapshotPin,
    StructurePins,
    ThesisPin,
)
from el.classification.planner import (
    ClassificationPlan,
    ClassificationPlanError,
    build_classification_plan,
)
from el.domain.structures import ExtractedStructure, MarketStructure
from el.domain.tables import (
    ActiveMarketUniverse,
    CandidateSet,
    CandidateSetMember,
    FitCard,
    Job,
    MarketRecommendation,
    MarketRulesCapture,
    MarketSnapshot,
    MarketStructureRow,
    MarketUniverseSnapshot,
    RejectedMarketRow,
    ThesisAnalysis,
)
from el.fitgate.checks import TOKEN_RULES_VERSION
from el.fitgate.policy import FIT_GATE_POLICY_VERSION, FitPolicy
from el.jobs import FailureKind, FencedWriteSession, JobClaim, JobStatus, JobStore
from el.marketstructure.gate import GATE_POLICY_VERSION as STRUCTURE_GATE_VERSION
from el.marketstructure.service import SCHEMA_VERSION as MARKET_SCHEMA_VERSION
from el.models.market_adapter import (
    MARKET_EXTRACTION_POLICY_VERSION,
    MarketProposerResult,
    MarketStructureProposer,
)
from el.retrieval.agent_retrieval_index import (
    AGENT_RETRIEVAL_BACKEND,
    AGENT_RETRIEVAL_INDEX_POLICY_VERSION,
    DEFAULT_AGENT_RETRIEVAL_TIMEOUT_SECONDS,
    AgentRetrievalCandidateIndex,
    AgentRetrievalTransport,
)
from el.retrieval.candidate_contracts import (
    CandidateIndexError,
    CandidateIndexIntegrityError,
    CandidateIndexPermanentError,
    CandidateIndexTransientError,
    CandidateQueryResult,
)
from el.retrieval.candidate_index import (
    CANDIDATE_INDEX_POLICY_VERSION,
    CandidateIndexIdentityConflict,
    SqliteCandidateIndex,
)
from el.retrieval.gate import GATE_POLICY_VERSION as RETRIEVAL_GATE_VERSION
from el.retrieval.gate import Loop2Policy
from el.retrieval.ranking import RANKING_POLICY_VERSION

CLASSIFICATION_JOB_TYPE = "fit_classification"
SQLITE_CANDIDATE_INDEX_BACKEND = "sqlite_lexical"


class ClassificationLeaseLost(RuntimeError):
    pass


class ClassificationPermanentError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoundMarketStructureProposer:
    """Trusted wiring that binds a proposer to explicit runtime identity."""

    delegate: MarketStructureProposer
    model_adapter: str
    model_version: str
    prompt_version: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.model_adapter,
                self.model_version,
                self.prompt_version,
            )
        ):
            raise ValueError("bound proposer identity must be nonblank")

    def propose_market_structure(self, **kwargs) -> MarketProposerResult:
        result = self.delegate.propose_market_structure(**kwargs)
        if result.model_adapter != self.model_adapter:
            raise ClassificationPermanentError(
                "proposer result differs from its bound adapter identity"
            )
        return result


@dataclass(frozen=True)
class ClassificationRuntimeConfig:
    """The exact semantics this worker binary is willing to execute."""

    code_version: str
    structure_model_adapter: str
    structure_model_version: str
    structure_prompt_version: str
    query_limit: int = 50
    structure_limit: int = 15
    retrieval_policy: Loop2Policy = field(default_factory=Loop2Policy)
    fit_policy: FitPolicy = field(default_factory=FitPolicy)

    def __post_init__(self) -> None:
        for value in (
            self.code_version,
            self.structure_model_adapter,
            self.structure_model_version,
            self.structure_prompt_version,
        ):
            if not value.strip():
                raise ValueError("classification runtime versions must be nonblank")
        if not 1 <= self.query_limit <= 200:
            raise ValueError("query_limit must be between 1 and 200")
        if not 1 <= self.structure_limit <= self.query_limit:
            raise ValueError("structure_limit must be within the query limit")
        if self.fit_policy.composite_coverage is not None:
            raise ValueError(
                "classification worker v1 does not support composite sidecars"
            )

    def retrieval_pins(self) -> RetrievalPins:
        return RetrievalPins(
            ranking_policy_version=RANKING_POLICY_VERSION,
            eligibility_policy_version=RETRIEVAL_GATE_VERSION,
            min_liquidity_usd=self.retrieval_policy.min_liquidity_usd,
            min_taxonomy_confidence=(
                self.retrieval_policy.min_taxonomy_confidence
            ),
            horizon_slack_days=self.retrieval_policy.horizon_slack_days,
            query_limit=self.query_limit,
        )

    def structure_pins(self) -> StructurePins:
        return StructurePins(
            schema_version=MARKET_SCHEMA_VERSION,
            extraction_policy_version=MARKET_EXTRACTION_POLICY_VERSION,
            gate_policy_version=STRUCTURE_GATE_VERSION,
            model_adapter=self.structure_model_adapter,
            model_version=self.structure_model_version,
            prompt_version=self.structure_prompt_version,
            structure_limit=self.structure_limit,
        )

    def fit_pins(self) -> FitPins:
        return FitPins(
            gate_policy_version=FIT_GATE_POLICY_VERSION,
            token_rules_version=TOKEN_RULES_VERSION,
            alias_rules_version=self.fit_policy.alias_rules_version,
            stacking_threshold=self.fit_policy.stacking_threshold,
            escalation_confidence_floor=(
                self.fit_policy.escalation_confidence_floor
            ),
            horizon_tolerances=dict(self.fit_policy.horizon_tolerances),
            m1_direction_guard=self.fit_policy.m1_direction_guard,
            advisory_enabled=False,
            advisory_model_version="disabled",
            advisory_prompt_version="disabled",
        )


class ClassificationJobSubmitter:
    """Pins the active immutable snapshot before creating a logical job."""

    def __init__(
        self,
        jobs: JobStore,
        session_factory: sessionmaker[Session],
        config: ClassificationRuntimeConfig,
    ):
        self._jobs = jobs
        self._sessions = session_factory
        self._config = config

    def submit(
        self,
        thesis_analysis_id: uuid.UUID,
        *,
        provider: str,
        venue: str,
        candidate_index: CandidateIndexPin,
        owner_client_type: str,
        owner_actor_id: str,
        idempotency_key: str,
        owner_user_id: uuid.UUID | None = None,
        submitted_by_api_client_id: uuid.UUID | None = None,
        max_attempts: int = 3,
        priority: int = 100,
        available_at: datetime | None = None,
        deadline_at: datetime | None = None,
        correlation_id: str | None = None,
        now: datetime | None = None,
    ):
        with self._sessions() as session:
            analysis = session.get(ThesisAnalysis, thesis_analysis_id)
            if analysis is None:
                raise ValueError("thesis analysis not found")
            pointer = session.get(
                ActiveMarketUniverse,
                {"provider": provider, "venue": venue},
            )
            if pointer is None:
                raise ValueError("active market universe not found")
            snapshot = session.get(MarketUniverseSnapshot, pointer.snapshot_id)
            if snapshot is None:
                raise ValueError("active market universe snapshot not found")
            pins = self._pins(analysis, snapshot, candidate_index)

        payload = ClassificationPayload(
            thesis_analysis_id=thesis_analysis_id
        ).model_dump(mode="json")
        return self._jobs.submit_or_get(
            job_type=CLASSIFICATION_JOB_TYPE,
            owner_client_type=owner_client_type,
            owner_actor_id=owner_actor_id,
            owner_user_id=owner_user_id,
            submitted_by_api_client_id=submitted_by_api_client_id,
            idempotency_key=idempotency_key,
            payload=payload,
            pinned_manifest=pins.model_dump(mode="json"),
            max_attempts=max_attempts,
            priority=priority,
            available_at=available_at,
            deadline_at=deadline_at,
            correlation_id=correlation_id,
            now=now,
        )

    def _pins(
        self,
        analysis: ThesisAnalysis,
        snapshot: MarketUniverseSnapshot,
        candidate_index: CandidateIndexPin,
    ) -> ClassificationPinManifest:
        if candidate_index.snapshot_id != snapshot.id:
            raise ValueError("candidate index is not for the active snapshot")
        if candidate_index.policy_version != _index_policy_for_backend(
            candidate_index.backend
        ):
            raise ValueError("candidate index policy is unsupported")
        descriptor = candidate_index.managed_descriptor
        if (
            descriptor is not None
            and descriptor.expected_open_object_count
            != snapshot.open_market_count
        ):
            raise ValueError(
                "managed candidate index object count differs from snapshot"
            )
        return ClassificationPinManifest(
            thesis=ThesisPin(
                thesis_analysis_id=analysis.id,
                schema_version=analysis.schema_version,
                content_sha256=_thesis_sha256(analysis),
            ),
            snapshot=SnapshotPin(
                snapshot_id=snapshot.id,
                provider=snapshot.provider,
                venue=snapshot.venue,
                cutoff_utc=_aware_database_time(snapshot.cutoff_utc),
                content_sha256=snapshot.content_sha256,
                artifact_sha256=snapshot.artifact_sha256,
                manifest_sha256=_json_sha256(snapshot.manifest),
                artifact_uri=snapshot.artifact_uri,
                normalization_policy_version=(
                    snapshot.normalization_policy_version
                ),
                validation_policy_version=snapshot.validation_policy_version,
            ),
            candidate_index=candidate_index,
            retrieval=self._config.retrieval_pins(),
            structure=self._config.structure_pins(),
            fit=self._config.fit_pins(),
            code_version=self._config.code_version,
        )


IndexResolver = Callable[[ClassificationPinManifest], CandidateIndexPort]


class LocalSqliteIndexResolver:
    """Resolve only explicitly pinned local ``file:`` index artifacts."""

    def __call__(
        self, pins: ClassificationPinManifest
    ) -> SqliteCandidateIndex:
        if pins.candidate_index.backend != SQLITE_CANDIDATE_INDEX_BACKEND:
            raise CandidateIndexPermanentError(
                "SQLite resolver cannot open the pinned candidate backend"
            )
        if pins.candidate_index.index_uri is None:
            raise CandidateIndexPermanentError(
                "SQLite candidate index URI is missing"
            )
        parsed = urlparse(pins.candidate_index.index_uri)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            raise CandidateIndexIdentityConflict(
                "classification worker requires a local file index URI"
            )
        path = Path(unquote(parsed.path))
        return SqliteCandidateIndex.open(
            path,
            expected_snapshot_id=pins.snapshot.snapshot_id,
            expected_content_sha256=pins.snapshot.content_sha256,
            expected_artifact_sha256=pins.snapshot.artifact_sha256,
            expected_index_sha256=pins.candidate_index.index_sha256,
            expected_policy_version=pins.candidate_index.policy_version,
        )


@dataclass(frozen=True)
class AgentRetrievalIndexResolver:
    """Construct a managed index handle around an injected API transport."""

    transport: AgentRetrievalTransport
    timeout_seconds: float = DEFAULT_AGENT_RETRIEVAL_TIMEOUT_SECONDS

    def __call__(
        self, pins: ClassificationPinManifest
    ) -> AgentRetrievalCandidateIndex:
        return self.resolve_pinned(
            candidate_pin=pins.candidate_index,
            snapshot=pins.snapshot,
        )

    def resolve_pinned(
        self,
        *,
        candidate_pin: CandidateIndexPin,
        snapshot: SnapshotPin,
    ) -> AgentRetrievalCandidateIndex:
        """Resolve the same descriptor boundary for workers and live spikes."""

        descriptor = candidate_pin.managed_descriptor
        if candidate_pin.backend != AGENT_RETRIEVAL_BACKEND or descriptor is None:
            raise CandidateIndexPermanentError(
                "Agent Retrieval resolver requires a managed descriptor pin"
            )
        if candidate_pin.index_sha256 != descriptor.canonical_sha256:
            raise CandidateIndexIntegrityError(
                "Agent Retrieval descriptor changed after submission"
            )
        if (
            candidate_pin.snapshot_id != snapshot.snapshot_id
            or descriptor.snapshot_id != snapshot.snapshot_id
            or descriptor.content_sha256 != snapshot.content_sha256
            or descriptor.artifact_sha256 != snapshot.artifact_sha256
        ):
            raise CandidateIndexIntegrityError(
                "Agent Retrieval descriptor differs from the snapshot pin"
            )
        return AgentRetrievalCandidateIndex(
            transport=self.transport,
            collection_resource=descriptor.collection_resource,
            snapshot_id=snapshot.snapshot_id,
            content_sha256=snapshot.content_sha256,
            artifact_sha256=snapshot.artifact_sha256,
            index_sha256=candidate_pin.index_sha256,
            object_manifest_sha256=descriptor.object_manifest_sha256,
            expected_open_object_count=(
                descriptor.expected_open_object_count
            ),
            collection_create_time=descriptor.create_time,
            collection_update_time=descriptor.update_time,
            index_policy_version=candidate_pin.policy_version,
            search_mode=descriptor.search_mode,
            embedding_config=descriptor.embedding_config,
            fusion_policy_version=descriptor.fusion_policy_version,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class BackendRoutingIndexResolver:
    """Route a pinned backend without consulting mutable active configuration."""

    sqlite_resolver: IndexResolver = field(
        default_factory=LocalSqliteIndexResolver
    )
    agent_retrieval_resolver: IndexResolver | None = None

    def __call__(self, pins: ClassificationPinManifest) -> CandidateIndexPort:
        backend = pins.candidate_index.backend
        if backend == SQLITE_CANDIDATE_INDEX_BACKEND:
            return self.sqlite_resolver(pins)
        if backend == AGENT_RETRIEVAL_BACKEND:
            if self.agent_retrieval_resolver is None:
                raise CandidateIndexPermanentError(
                    "Agent Retrieval backend is not configured"
                )
            return self.agent_retrieval_resolver(pins)
        raise CandidateIndexPermanentError(
            "candidate index backend is unsupported"
        )


class ClassificationWorker:
    def __init__(
        self,
        *,
        jobs: JobStore,
        session_factory: sessionmaker[Session],
        index_resolver: IndexResolver,
        proposer: BoundMarketStructureProposer,
        config: ClassificationRuntimeConfig,
        worker_id: str,
        lease_seconds: int = 90,
    ):
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds < 3:
            raise ValueError("classification lease must be at least 3 seconds")
        self._jobs = jobs
        self._sessions = session_factory
        self._index_resolver = index_resolver
        self._proposer = proposer
        self._config = config
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    def run_once(self, *, now: datetime | None = None) -> ClassificationWorkerResult:
        claim = self._jobs.claim_due(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            job_types=[CLASSIFICATION_JOB_TYPE],
            now=now,
        )
        if claim is None:
            return ClassificationWorkerResult(claimed=False, status="idle")

        try:
            payload = ClassificationPayload.model_validate(claim.payload)
            pins = ClassificationPinManifest.model_validate(claim.pinned_manifest)
            self._verify_runtime(pins)
            self._verify_proposer_identity(pins)
            self._checkpoint(claim, "validating_pins", now)
            structure = self._load_pinned_inputs(payload, pins)
            index = self._index_resolver(pins)
            self._verify_index_handle(index, pins)
            self._checkpoint(claim, "querying_index", now)
            query = index.query(structure, limit=pins.retrieval.query_limit)
            self._verify_query_result(query, pins)
            self._checkpoint(claim, "planning_candidates", now)
            plan = build_classification_plan(
                claim=structure,
                query=query,
                pins=pins,
                proposer=self._proposer,
                checkpoint=lambda stage: self._checkpoint(claim, stage, now),
                judged_at=now or _utcnow(),
                attempt_trace_id=claim.attempt_trace_id,
            )
            self._checkpoint(claim, "finalizing_bundle", now)
            outcome: dict | None = None

            def commit_action(
                session: FencedWriteSession, job: Job
            ) -> dict:
                nonlocal outcome
                outcome = self._persist_plan(
                    session,
                    job,
                    pins=pins,
                    plan=plan,
                )
                return outcome

            if not self._jobs.succeed_with(claim, commit_action, now=now):
                return self._lost(claim)
            assert outcome is not None
            return ClassificationWorkerResult(
                claimed=True,
                status="succeeded",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                fit_card_id=uuid.UUID(outcome["fit_card_id"]),
                market_recommendation_id=uuid.UUID(
                    outcome["market_recommendation_id"]
                ),
                fit_class=outcome["fit_class"],
                recommended_market_id=outcome["recommended_market_id"],
            )
        except ClassificationLeaseLost:
            return self._lost(claim)
        except CandidateIndexTransientError:
            return self._fail(
                claim,
                kind=FailureKind.TRANSIENT,
                code="candidate_index_transient",
                message="candidate retrieval is temporarily unavailable",
                now=now,
            )
        except CandidateIndexIntegrityError:
            return self._fail(
                claim,
                kind=FailureKind.NEEDS_OPERATOR,
                code="candidate_index_integrity",
                message="candidate retrieval conflicts with pinned evidence",
                now=now,
            )
        except CandidateIndexPermanentError:
            return self._fail(
                claim,
                kind=FailureKind.PERMANENT,
                code="candidate_index_permanent",
                message="candidate retrieval configuration is invalid",
                now=now,
            )
        except CandidateIndexError:
            return self._fail(
                claim,
                kind=FailureKind.PERMANENT,
                code="candidate_index_error",
                message="candidate retrieval failed",
                now=now,
            )
        except (
            ClassificationPermanentError,
            ClassificationPlanError,
            ValidationError,
            ValueError,
        ):
            return self._fail(
                claim,
                kind=FailureKind.PERMANENT,
                code="classification_input_invalid",
                message="classification input or pinned evidence is invalid",
                now=now,
            )
        except Exception:
            return self._fail(
                claim,
                kind=FailureKind.TRANSIENT,
                code="classification_attempt_failed",
                message="classification attempt failed",
                now=now,
            )

    def _verify_runtime(self, pins: ClassificationPinManifest) -> None:
        expected = (
            self._config.retrieval_pins(),
            self._config.structure_pins(),
            self._config.fit_pins(),
            self._config.code_version,
        )
        observed = (pins.retrieval, pins.structure, pins.fit, pins.code_version)
        if observed != expected:
            raise ClassificationPermanentError(
                "worker binary cannot execute the pinned semantic versions"
            )
        if pins.candidate_index.policy_version != _index_policy_for_backend(
            pins.candidate_index.backend
        ):
            raise ClassificationPermanentError("unsupported candidate index policy")

    def _verify_proposer_identity(
        self, pins: ClassificationPinManifest
    ) -> None:
        observed = (
            self._proposer.model_adapter,
            self._proposer.model_version,
            self._proposer.prompt_version,
        )
        expected = (
            pins.structure.model_adapter,
            pins.structure.model_version,
            pins.structure.prompt_version,
        )
        if observed != expected:
            raise ClassificationPermanentError(
                "configured proposer differs from the pinned identity"
            )

    def _load_pinned_inputs(
        self,
        payload: ClassificationPayload,
        pins: ClassificationPinManifest,
    ) -> ExtractedStructure:
        if payload.thesis_analysis_id != pins.thesis.thesis_analysis_id:
            raise ClassificationPermanentError(
                "payload and pinned thesis identities differ"
            )
        with self._sessions() as session:
            analysis = session.get(ThesisAnalysis, payload.thesis_analysis_id)
            snapshot = session.get(
                MarketUniverseSnapshot,
                pins.snapshot.snapshot_id,
            )
            if analysis is None or snapshot is None:
                raise ClassificationPermanentError("pinned input is missing")
            if (
                analysis.id != pins.thesis.thesis_analysis_id
                or analysis.schema_version != pins.thesis.schema_version
                or _thesis_sha256(analysis) != pins.thesis.content_sha256
            ):
                raise ClassificationPermanentError("pinned thesis changed")
            observed = (
                snapshot.provider,
                snapshot.venue,
                _aware_database_time(snapshot.cutoff_utc),
                snapshot.content_sha256,
                snapshot.artifact_sha256,
                _json_sha256(snapshot.manifest),
                snapshot.artifact_uri,
                snapshot.normalization_policy_version,
                snapshot.validation_policy_version,
            )
            expected = (
                pins.snapshot.provider,
                pins.snapshot.venue,
                pins.snapshot.cutoff_utc,
                pins.snapshot.content_sha256,
                pins.snapshot.artifact_sha256,
                pins.snapshot.manifest_sha256,
                pins.snapshot.artifact_uri,
                pins.snapshot.normalization_policy_version,
                pins.snapshot.validation_policy_version,
            )
            if observed != expected:
                raise ClassificationPermanentError("pinned snapshot changed")
            descriptor = pins.candidate_index.managed_descriptor
            if (
                descriptor is not None
                and descriptor.expected_open_object_count
                != snapshot.open_market_count
            ):
                raise CandidateIndexIntegrityError(
                    "managed candidate index population differs from snapshot"
                )
            structure = ExtractedStructure.model_validate(
                analysis.extracted_structure
            )
            return structure

    @staticmethod
    def _verify_index_handle(
        index: CandidateIndexPort,
        pins: ClassificationPinManifest,
    ) -> None:
        observed = (
            index.backend,
            index.snapshot_id,
            index.content_sha256,
            index.artifact_sha256,
            index.index_policy_version,
            index.index_sha256,
        )
        expected = (
            pins.candidate_index.backend,
            pins.snapshot.snapshot_id,
            pins.snapshot.content_sha256,
            pins.snapshot.artifact_sha256,
            pins.candidate_index.policy_version,
            pins.candidate_index.index_sha256,
        )
        if observed != expected:
            raise CandidateIndexIntegrityError("candidate index pin mismatch")

    @staticmethod
    def _verify_query_result(
        query: CandidateQueryResult,
        pins: ClassificationPinManifest,
    ) -> None:
        observed = (
            query.backend,
            query.snapshot_id,
            query.content_sha256,
            query.artifact_sha256,
            query.index_sha256,
            query.index_policy_version,
            query.limit,
        )
        expected = (
            pins.candidate_index.backend,
            pins.snapshot.snapshot_id,
            pins.snapshot.content_sha256,
            pins.snapshot.artifact_sha256,
            pins.candidate_index.index_sha256,
            pins.candidate_index.policy_version,
            pins.retrieval.query_limit,
        )
        if observed != expected:
            raise CandidateIndexIntegrityError("candidate query pin mismatch")

    def _checkpoint(
        self,
        claim: JobClaim,
        stage: str,
        now: datetime | None,
    ) -> None:
        stage = _bounded_stage(stage)
        if not self._jobs.heartbeat(
            claim,
            lease_seconds=self._lease_seconds,
            stage=stage,
            now=now,
        ):
            raise ClassificationLeaseLost("classification authority lost")

    def _persist_plan(
        self,
        session: FencedWriteSession,
        job: Job,
        *,
        pins: ClassificationPinManifest,
        plan: ClassificationPlan,
    ) -> dict:
        analysis = session.scalar(
            select(ThesisAnalysis)
            .where(ThesisAnalysis.id == pins.thesis.thesis_analysis_id)
            .with_for_update()
        )
        if (
            analysis is None
            or analysis.schema_version != pins.thesis.schema_version
            or _thesis_sha256(analysis) != pins.thesis.content_sha256
        ):
            raise ClassificationPermanentError(
                "thesis changed before fenced finalization"
            )
        legacy_snapshot = session.get(MarketSnapshot, pins.snapshot.snapshot_id)
        legacy_retrieval_id = f"universe_{pins.snapshot.content_sha256[:16]}"
        if legacy_snapshot is None:
            session.add(
                MarketSnapshot(
                    id=pins.snapshot.snapshot_id,
                    venue_id=pins.snapshot.venue,
                    as_of_ts=pins.snapshot.cutoff_utc,
                    retrieval_id=legacy_retrieval_id,
                )
            )
        elif (
            legacy_snapshot.venue_id != pins.snapshot.venue
            or _aware_database_time(legacy_snapshot.as_of_ts)
            != pins.snapshot.cutoff_utc
            or legacy_snapshot.retrieval_id != legacy_retrieval_id
        ):
            raise ClassificationPermanentError(
                "legacy snapshot bridge conflicts with pinned universe"
            )

        capture_times: dict[str, datetime] = {}
        for member in plan.members:
            evidence = member.evidence
            capture = session.scalar(
                select(MarketRulesCapture).where(
                    MarketRulesCapture.market_id == evidence.record.market_id,
                    MarketRulesCapture.snapshot_id == pins.snapshot.snapshot_id,
                )
            )
            if capture is None:
                session.add(
                    MarketRulesCapture(
                        id=uuid.uuid4(),
                        market_id=evidence.record.market_id,
                        snapshot_id=pins.snapshot.snapshot_id,
                        contract_terms_text=evidence.record.title,
                        resolution_rules_text=evidence.record.resolution_rules,
                        contract_terms_hash=evidence.contract_terms_hash,
                        resolution_rules_hash=evidence.resolution_rules_hash,
                        rules_captured_at=evidence.snapshot_ts,
                    )
                )
                capture_times[evidence.record.market_id] = evidence.snapshot_ts
            elif (
                capture.contract_terms_text != evidence.record.title
                or capture.resolution_rules_text != evidence.record.resolution_rules
                or capture.contract_terms_hash != evidence.contract_terms_hash
                or capture.resolution_rules_hash
                != evidence.resolution_rules_hash
                or _aware_database_time(capture.rules_captured_at)
                != evidence.snapshot_ts
            ):
                raise ClassificationPermanentError(
                    "rules capture conflicts with pinned candidate evidence"
                )
            else:
                capture_times[evidence.record.market_id] = _aware_database_time(
                    capture.rules_captured_at
                )

        for structure in plan.structures:
            existing = session.scalar(
                select(MarketStructureRow).where(
                    MarketStructureRow.market_id
                    == structure.evidence.record.market_id,
                    MarketStructureRow.contract_terms_hash
                    == structure.evidence.contract_terms_hash,
                    MarketStructureRow.resolution_rules_hash
                    == structure.evidence.resolution_rules_hash,
                    MarketStructureRow.schema_version
                    == pins.structure.schema_version,
                    MarketStructureRow.extraction_policy_version
                    == pins.structure.extraction_policy_version,
                )
            )
            if existing is None:
                session.add(
                    MarketStructureRow(
                        id=uuid.uuid4(),
                        market_id=structure.evidence.record.market_id,
                        contract_terms_hash=(
                            structure.evidence.contract_terms_hash
                        ),
                        resolution_rules_hash=(
                            structure.evidence.resolution_rules_hash
                        ),
                        snapshot_id=pins.snapshot.snapshot_id,
                        schema_version=pins.structure.schema_version,
                        structure=structure.structure.model_dump(mode="json"),
                        extraction_policy_version=(
                            pins.structure.extraction_policy_version
                        ),
                    )
                )
            else:
                cached = MarketStructure.model_validate(existing.structure).model_copy(
                    update={"snapshot_id": pins.snapshot.snapshot_id}
                )
                if cached != structure.structure:
                    raise ClassificationPermanentError(
                        "market structure cache conflicts with planned output"
                    )

        candidate_set_id = uuid.uuid4()
        fit_card_id = uuid.uuid4()
        recommendation_id = uuid.uuid4()
        session.add(
            CandidateSet(
                id=candidate_set_id,
                thesis_analysis_id=analysis.id,
                snapshot_id=pins.snapshot.snapshot_id,
                retrieval_id=plan.retrieval_id,
                job_id=job.id,
            )
        )
        session.add_all(
            CandidateSetMember(
                id=uuid.uuid4(),
                candidate_set_id=candidate_set_id,
                market_id=member.evidence.record.market_id,
                rank=member.rank,
                retrieval_score=member.retrieval_score,
                current_probability=None,
                eligibility_flags=member.eligibility_flags,
                excluded_reason=member.excluded_reason,
            )
            for member in plan.members
        )
        session.add(
            FitCard(
                id=fit_card_id,
                thesis_analysis_id=analysis.id,
                candidate_set_id=candidate_set_id,
                semantic_fit_class=plan.thesis.fit_class.value,
                recommended_market_id=plan.thesis.recommended_market_id,
                what_it_captures=plan.what_it_captures,
                what_it_misses=plan.what_it_misses,
                horizon_match=plan.horizon_match,
                resolution_risk=plan.resolution_risk,
                fit_confidence=None,
                provenance=plan.provenance,
                job_id=job.id,
            )
        )
        recommended_evidence = next(
            (
                member.evidence
                for member in plan.members
                if member.evidence.record.market_id
                == plan.thesis.recommended_market_id
            ),
            None,
        )
        session.add(
            MarketRecommendation(
                id=recommendation_id,
                thesis_analysis_id=analysis.id,
                recommended_market_id=plan.thesis.recommended_market_id,
                expression_type=plan.thesis.fit_class.value,
                fit_score=None,
                fit_reason=plan.what_it_captures,
                why_now=None,
                crowding_note=None,
                as_of_ts=(
                    recommended_evidence.snapshot_ts
                    if recommended_evidence
                    else pins.snapshot.cutoff_utc
                ),
                snapshot_id=pins.snapshot.snapshot_id,
                retrieval_id=plan.retrieval_id,
                venue_id=pins.snapshot.venue,
                contract_terms_hash=(
                    recommended_evidence.contract_terms_hash
                    if recommended_evidence
                    else None
                ),
                resolution_rules_hash=(
                    recommended_evidence.resolution_rules_hash
                    if recommended_evidence
                    else None
                ),
                rules_captured_at=(
                    capture_times[recommended_evidence.record.market_id]
                    if recommended_evidence
                    else None
                ),
                provenance=plan.provenance,
                fit_card_id=fit_card_id,
                job_id=job.id,
            )
        )
        session.add_all(
            RejectedMarketRow(
                id=uuid.uuid4(),
                market_recommendation_id=recommendation_id,
                market_id=market_id,
                reason=reason,
            )
            for market_id, reason in plan.rejection_reasons
        )
        session.flush()
        return {
            "fit_card_id": str(fit_card_id),
            "market_recommendation_id": str(recommendation_id),
            "candidate_set_id": str(candidate_set_id),
            "snapshot_id": pins.snapshot.snapshot_id,
            "query_digest": plan.query_digest,
            "fit_class": plan.thesis.fit_class.value,
            "recommended_market_id": plan.thesis.recommended_market_id,
            "rejected_count": len(plan.rejection_reasons),
            "structure_rejected_count": len(plan.structure_rejections),
        }

    def _fail(
        self,
        claim: JobClaim,
        *,
        kind: FailureKind,
        code: str,
        message: str,
        now: datetime | None,
    ) -> ClassificationWorkerResult:
        if not self._jobs.fail(
            claim,
            kind=kind,
            code=code,
            safe_message=message,
            now=now,
        ):
            return self._lost(claim)
        job = self._jobs.get(claim.job_id)
        status = "failed"
        if job.status == JobStatus.RETRY_WAIT.value:
            status = "retry_wait"
        elif job.status == JobStatus.NEEDS_OPERATOR.value:
            status = "needs_operator"
        return ClassificationWorkerResult(
            claimed=True,
            status=status,
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
        )

    @staticmethod
    def _lost(claim: JobClaim) -> ClassificationWorkerResult:
        return ClassificationWorkerResult(
            claimed=True,
            status="lost_authority",
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
        )


def _json_sha256(value: dict) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _index_policy_for_backend(backend: str) -> str:
    if backend == SQLITE_CANDIDATE_INDEX_BACKEND:
        return CANDIDATE_INDEX_POLICY_VERSION
    if backend == AGENT_RETRIEVAL_BACKEND:
        return AGENT_RETRIEVAL_INDEX_POLICY_VERSION
    raise ValueError("candidate index backend is unsupported")


def _thesis_sha256(analysis: ThesisAnalysis) -> str:
    return _json_sha256(
        {
            "id": str(analysis.id),
            "source_signal_id": (
                str(analysis.source_signal_id)
                if analysis.source_signal_id is not None
                else None
            ),
            "input_text": analysis.input_text,
            "extracted_structure": analysis.extracted_structure,
            "schema_version": analysis.schema_version,
            "normalized_claim_summary": analysis.normalized_claim_summary,
            "client_type": analysis.client_type,
            "agent_client_id": analysis.agent_client_id,
        }
    )


def _bounded_stage(stage: str) -> str:
    if len(stage) <= 64:
        return stage
    digest = hashlib.sha256(stage.encode()).hexdigest()[:16]
    return f"structure_checkpoint_{digest}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware_database_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
