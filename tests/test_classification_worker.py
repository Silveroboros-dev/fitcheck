"""Private classification-worker acceptance tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.orm import sessionmaker

from el.classification import (
    AgentRetrievalIndexResolver,
    BackendRoutingIndexResolver,
    BoundMarketStructureProposer,
    CandidateIndexPin,
    ClassificationJobSubmitter,
    ClassificationPinManifest,
    ClassificationRuntimeConfig,
    ClassificationWorker,
    LocalSqliteIndexResolver,
    ManagedAgentRetrievalDescriptor,
)
from el.domain.contracts import FitCardOut, RejectedMarket
from el.domain.structures import (
    ClaimHorizon,
    Entity,
    ExtractedStructure,
    MarketStructure,
    MarketHorizon,
    Mechanism,
    Metric,
)
from el.domain.enums import EventStage
from el.domain.tables import (
    ActiveMarketUniverse,
    Base,
    CandidateSet,
    CandidateSetMember,
    FitCard,
    Job,
    JobAttempt,
    MarketRecommendation,
    MarketRulesCapture,
    MarketSnapshot,
    MarketStructureRow,
    MarketUniverseSnapshot,
    RejectedMarketRow,
    ThesisAnalysis,
)
from el.jobs import JobStatus, JobStore, canonical_payload_hash
from el.fitgate.m1_subject_only import (
    M1_SUBJECT_ONLY_POLICY_VERSION,
    fit_policy_from_pins,
    gate_policy_version,
    ordinary_discovery_fit_policy,
)
from el.fitgate.policy import FitPolicy
from el.models.market_adapter import FixtureMarketStructureProposer
from el.retrieval.agent_retrieval_index import (
    AGENT_RETRIEVAL_INDEX_POLICY_VERSION,
    agent_retrieval_object_schema_descriptor,
)
from el.retrieval.candidate_contracts import (
    CandidateIndexIntegrityError,
    CandidateIndexPermanentError,
    CandidateIndexTransientError,
)
from el.retrieval.candidate_index import SqliteCandidateIndex
from el.retrieval.snapshot_contracts import (
    NormalizedUniverseMarket,
    SnapshotKey,
    SnapshotValidationPolicy,
    StagedSnapshot,
)
from el.retrieval.snapshot_validation import (
    build_manifest,
    canonical_row_json,
    stage_fixture_jsonl_rows,
    validate_staged_snapshot,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"


def test_new_classification_runtime_pins_m1_successor_by_default():
    config = ClassificationRuntimeConfig(
        code_version="classification-test-build-v1",
        structure_model_adapter="fixture",
        structure_model_version="fixture-goldens-v1",
        structure_prompt_version="fixture-prompt-v1",
    )

    assert config.fit_policy.gate_policy_version == M1_SUBJECT_ONLY_POLICY_VERSION
    pins = config.fit_pins()
    assert pins.gate_policy_version == M1_SUBJECT_ONLY_POLICY_VERSION
    assert "m1_subject_only_residual_guard" not in pins.model_dump(mode="json")
    restored = fit_policy_from_pins(
        gate_policy_version=pins.gate_policy_version,
        stacking_threshold=pins.stacking_threshold,
        escalation_confidence_floor=pins.escalation_confidence_floor,
        horizon_tolerances=dict(pins.horizon_tolerances),
        alias_rules_version=pins.alias_rules_version,
        m1_direction_guard=pins.m1_direction_guard,
    )
    assert restored.gate_policy_version == M1_SUBJECT_ONLY_POLICY_VERSION


def test_legacy_predecessor_manifest_reenqueues_with_its_original_hash(
    tmp_path,
):
    environment = _environment(tmp_path, fit_policy=FitPolicy())
    first = environment.submit(key="legacy-predecessor-retry")
    job = environment.jobs.get(first.job_id)
    raw_manifest = job.pinned_manifest

    # This is the exact v1 predecessor JSON a queued job stores. Parsing it
    # must not add a successor-only key before the retry is hashed.
    assert (
        "m1_subject_only_residual_guard" not in raw_manifest["fit"]
    )
    assert (
        ClassificationPinManifest.model_validate(raw_manifest).model_dump(
            mode="json"
        )
        == raw_manifest
    )
    assert job.payload_hash == canonical_payload_hash(
        job.payload,
        raw_manifest,
        execution_guarantees={
            "max_attempts": 3,
            "priority": 100,
            "available_at": {"mode": "submission_time"},
            "deadline_at": None,
        },
    )

    retry = environment.submit(key="legacy-predecessor-retry")
    assert not retry.created
    assert retry.job_id == first.job_id

    result = environment.worker().run_once(now=NOW)
    assert result.status == "succeeded"
    with environment.sessions() as session:
        card = session.scalars(select(FitCard)).one()
        assert card.provenance["per_market"]["mkt_gemini_lmsys_1"][
            "gate_policy_version"
        ] == gate_policy_version(FitPolicy())


def test_successor_manifest_derives_guard_from_policy_identity_at_runtime(tmp_path):
    environment = _environment(
        tmp_path,
        fit_policy=ordinary_discovery_fit_policy(),
    )
    submitted = environment.submit(key="successor-policy-runtime")
    job = environment.jobs.get(submitted.job_id)

    assert job.pinned_manifest["fit"]["gate_policy_version"] == (
        M1_SUBJECT_ONLY_POLICY_VERSION
    )
    assert "m1_subject_only_residual_guard" not in job.pinned_manifest["fit"]

    result = environment.worker().run_once(now=NOW)
    assert result.status == "succeeded"
    with environment.sessions() as session:
        card = session.scalars(select(FitCard)).one()
        assert card.provenance["per_market"]["mkt_gemini_lmsys_1"][
            "gate_policy_version"
        ] == M1_SUBJECT_ONLY_POLICY_VERSION


def _claim(**overrides) -> ExtractedStructure:
    values = {
        "claim_summary": (
            "Gemini ranks #1 on LMSYS Chatbot Arena by the end of 2026."
        ),
        "entities": [
            Entity(name="Google Gemini", role="subject"),
            Entity(name="LMSYS Chatbot Arena", role="venue"),
        ],
        "event_stage": "measured",
        "metric": Metric(
            what="LMSYS Chatbot Arena #1 rank",
            measured_by="LMSYS leaderboard",
            objective=True,
        ),
        "horizon": ClaimHorizon(
            window_end=date(2026, 12, 31), precision="day"
        ),
        "mechanism": Mechanism(),
        "stance": "yes",
        "resolution_source_class": "leaderboard",
        "contractible_version": (
            "Will a Google Gemini model rank #1 on LMSYS?"
        ),
    }
    values.update(overrides)
    return ExtractedStructure(**values)


def _phase0_rows() -> list[dict]:
    fixture = json.loads(
        (FIXTURES / "retrieval" / "frozen_snapshot_phase0.json").read_text()
    )
    snapshot_ts = fixture["as_of_ts"]
    return [
        {
            "market_id": row["market_id"],
            "title": row["title"],
            "slug": row["market_id"],
            "description": row["description"],
            "resolution_rules": row["resolution_rules"],
            "outcomes": row["outcomes"],
            "token_ids": [f"{row['market_id']}-yes", f"{row['market_id']}-no"],
            "close_date": row["close_date"],
            "closed_time": None,
            "snapshot_ts": snapshot_ts,
            "is_open": True,
            "volume_usd": None,
            "taxonomy_l1": row["taxonomy_l1"],
            "taxonomy_confidence": row["taxonomy_confidence"],
            "tags": row["tags"],
            "source_url": row["source_url"],
        }
        for row in fixture["markets"]
    ]


def _phase0_structures() -> dict[str, MarketStructure]:
    raw = json.loads(
        (FIXTURES / "markets" / "golden_market_structures.json").read_text()
    )
    return {
        row["market_id"]: MarketStructure.model_validate(row)
        for row in raw["structures"]
    }


def _market_row(
    market_id: str,
    *,
    snapshot_ts: str = "2026-05-22T00:00:00+00:00",
) -> dict:
    row = dict(_phase0_rows()[0])
    row.update(
        {
            "market_id": market_id,
            "slug": market_id,
            "token_ids": [f"{market_id}-yes", f"{market_id}-no"],
            "snapshot_ts": snapshot_ts,
        }
    )
    return row


def _sessions(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'classification.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@dataclass
class _Environment:
    sessions: sessionmaker
    jobs: JobStore
    analysis_id: uuid.UUID
    index_path: Path
    index_sha256: str
    snapshot_id: str
    content_sha256: str
    artifact_sha256: str
    open_market_count: int
    config: ClassificationRuntimeConfig
    proposer: object
    owner_client_type: str = "api"
    owner_actor_id: str = "actor-test"

    def sqlite_index_pin(self) -> CandidateIndexPin:
        return CandidateIndexPin(
            snapshot_id=self.snapshot_id,
            index_uri=self.index_path.resolve().as_uri(),
            index_sha256=self.index_sha256,
            policy_version="lexical-postings-v1",
        )

    def submit(
        self,
        *,
        key: str = "classification-1",
        max_attempts: int = 3,
        candidate_index: CandidateIndexPin | None = None,
    ):
        return ClassificationJobSubmitter(
            self.jobs, self.sessions, self.config
        ).submit(
            self.analysis_id,
            provider="fixture",
            venue="test",
            candidate_index=candidate_index or self.sqlite_index_pin(),
            owner_client_type=self.owner_client_type,
            owner_actor_id=self.owner_actor_id,
            idempotency_key=key,
            max_attempts=max_attempts,
            now=NOW,
        )

    def worker(self, proposer=None, index_resolver=None):
        return ClassificationWorker(
            jobs=self.jobs,
            session_factory=self.sessions,
            index_resolver=index_resolver or LocalSqliteIndexResolver(),
            proposer=proposer or self.proposer,
            config=self.config,
            worker_id="classification-worker-test",
            lease_seconds=30,
        )


def _environment(
    tmp_path: Path,
    *,
    claim: ExtractedStructure | None = None,
    rows: list[dict] | None = None,
    structures: dict[str, MarketStructure] | None = None,
    query_limit: int | None = None,
    structure_limit: int | None = None,
    fit_policy: FitPolicy | None = None,
) -> _Environment:
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    seed_job = jobs.submit_or_get(
        job_type="market_universe_refresh",
        owner_client_type="system",
        owner_actor_id="snapshot-seed",
        idempotency_key="seed",
        payload={"provider": "fixture", "venue": "test"},
        pinned_manifest={"normalization_policy_version": "market-v1"},
        now=NOW,
    )
    claim = claim or _claim()
    rows = rows or _phase0_rows()
    structures = structures or _phase0_structures()

    artifact = stage_fixture_jsonl_rows(rows, tmp_path / "universe.jsonl")
    staged = StagedSnapshot(
        path=artifact,
        key=SnapshotKey(provider="fixture", venue="test"),
        cutoff_utc=datetime(2026, 5, 22, tzinfo=timezone.utc),
        normalization_policy_version="market-normalization-v1",
        source_versions={"fixture": "phase0"},
    )
    validation_policy = SnapshotValidationPolicy(
        version="classification-fixture-v1",
        minimum_row_count=1,
    )
    report = validate_staged_snapshot(staged, validation_policy)
    assert report.passed
    manifest = build_manifest(
        staged,
        report,
        validation_policy,
        artifact_uri=artifact.resolve().as_uri(),
        generated_at=NOW,
    )
    index_path = tmp_path / "candidate.sqlite"
    built = SqliteCandidateIndex.build(
        manifest=manifest,
        artifact_path=artifact,
        index_path=index_path,
    )

    with sessions.begin() as session:
        analysis = ThesisAnalysis(
            input_text=claim.claim_summary,
            extracted_structure=claim.model_dump(mode="json"),
            normalized_claim_summary=claim.claim_summary,
            client_type="human_ui",
        )
        session.add(analysis)
        session.flush()
        session.add(
            MarketUniverseSnapshot(
                id=manifest.snapshot_id,
                provider=manifest.provider,
                venue=manifest.venue,
                cutoff_utc=manifest.cutoff_utc,
                content_sha256=manifest.content_sha256,
                membership_sha256=manifest.membership_sha256,
                artifact_uri=manifest.artifact_uri,
                artifact_format=manifest.artifact_format,
                artifact_sha256=manifest.artifact_sha256,
                artifact_bytes=manifest.artifact_bytes,
                row_count=manifest.row_count,
                unique_market_count=manifest.unique_market_count,
                open_market_count=manifest.open_market_count,
                normalization_policy_version=(
                    manifest.normalization_policy_version
                ),
                validation_policy_version=manifest.validation_policy_version,
                source_versions=manifest.source_versions,
                manifest=manifest.model_dump(mode="json"),
                created_by_job_id=seed_job.job_id,
            )
        )
        session.flush()
        session.add(
            ActiveMarketUniverse(
                provider=manifest.provider,
                venue=manifest.venue,
                snapshot_id=manifest.snapshot_id,
                generation=1,
                promoted_by_job_id=seed_job.job_id,
                promoted_at=NOW,
            )
        )
        analysis_id = analysis.id

    config = ClassificationRuntimeConfig(
        code_version="classification-test-build-v1",
        structure_model_adapter="fixture",
        structure_model_version="fixture-goldens-v1",
        structure_prompt_version="fixture-prompt-v1",
        query_limit=query_limit or min(10, len(rows)),
        structure_limit=structure_limit or min(10, len(rows)),
        fit_policy=fit_policy or FitPolicy(),
    )
    proposer = BoundMarketStructureProposer(
        delegate=FixtureMarketStructureProposer(structures),
        model_adapter=config.structure_model_adapter,
        model_version=config.structure_model_version,
        prompt_version=config.structure_prompt_version,
    )
    return _Environment(
        sessions=sessions,
        jobs=jobs,
        analysis_id=analysis_id,
        index_path=index_path,
        index_sha256=built.index_sha256,
        snapshot_id=manifest.snapshot_id,
        content_sha256=manifest.content_sha256,
        artifact_sha256=manifest.artifact_sha256,
        open_market_count=manifest.open_market_count,
        config=config,
        proposer=proposer,
    )


def _managed_descriptor(
    environment: _Environment | None = None,
    *,
    expected_open_object_count: int | None = None,
    **overrides,
) -> ManagedAgentRetrievalDescriptor:
    values = {
        "project_id": "fitcheck-spike-project",
        "location": "us-central1",
        "api_version": "v1",
        "collection_resource": (
            "projects/fitcheck-spike-project/locations/us-central1/"
            "collections/fixture-markets"
        ),
        "snapshot_id": (
            environment.snapshot_id if environment is not None else "snapshot-1"
        ),
        "content_sha256": (
            environment.content_sha256 if environment is not None else "1" * 64
        ),
        "artifact_sha256": (
            environment.artifact_sha256 if environment is not None else "2" * 64
        ),
        "create_time": NOW - timedelta(minutes=5),
        "update_time": NOW,
        "object_manifest_sha256": hashlib.sha256(
            (
                environment.snapshot_id
                if environment is not None
                else "snapshot-1"
            ).encode("utf-8")
        ).hexdigest(),
        "expected_open_object_count": (
            expected_open_object_count
            if expected_open_object_count is not None
            else environment.open_market_count
            if environment is not None
            else 3
        ),
        "object_schema": agent_retrieval_object_schema_descriptor(),
        "query_policy_version": AGENT_RETRIEVAL_INDEX_POLICY_VERSION,
        "search_mode": "text",
        "embedding_config": "disabled",
        "fusion_policy_version": "disabled",
    }
    values.update(overrides)
    return ManagedAgentRetrievalDescriptor.model_validate(values)


def _managed_pin(
    environment: _Environment,
    descriptor: ManagedAgentRetrievalDescriptor | None = None,
) -> CandidateIndexPin:
    descriptor = descriptor or _managed_descriptor(environment)
    return CandidateIndexPin(
        backend="google_agent_retrieval",
        snapshot_id=environment.snapshot_id,
        index_sha256=descriptor.canonical_sha256,
        policy_version=AGENT_RETRIEVAL_INDEX_POLICY_VERSION,
        managed_descriptor=descriptor,
    )


def _managed_hit(row: dict, *, score: float) -> dict:
    market = NormalizedUniverseMarket.model_validate(row)
    return {
        "market_id": market.market_id,
        "snapshot_id": None,
        "content_sha256": None,
        "row_sha256": hashlib.sha256(
            canonical_row_json(market).encode("utf-8")
        ).hexdigest(),
        "is_open": market.is_open,
        "market": market.model_dump(mode="json"),
        "score": score,
    }


class _RecordingAgentRetrievalTransport:
    def __init__(self, hits: list[dict], *, request_id: str):
        self.hits = hits
        self.request_id = request_id
        self.requests = []

    def search(self, request):
        self.requests.append(request)
        filters = request.filters
        return {
            "collection_resource": request.collection_resource,
            "collection_create_time": (
                request.expected_collection_create_time
            ),
            "collection_update_time": (
                request.expected_collection_update_time
            ),
            "snapshot_id": filters.snapshot_id,
            "content_sha256": filters.content_sha256,
            "object_schema_version": request.object_schema_version,
            "object_manifest_sha256": (
                request.expected_object_manifest_sha256
            ),
            "open_object_count": request.expected_open_object_count,
            "hits": [
                {
                    **hit,
                    "snapshot_id": filters.snapshot_id,
                    "content_sha256": filters.content_sha256,
                }
                for hit in self.hits
            ],
            "request_id": self.request_id,
        }


class _FailingCandidateIndex:
    def __init__(self, pins, error: Exception):
        self.backend = pins.candidate_index.backend
        self.snapshot_id = pins.snapshot.snapshot_id
        self.content_sha256 = pins.snapshot.content_sha256
        self.artifact_sha256 = pins.snapshot.artifact_sha256
        self.index_policy_version = pins.candidate_index.policy_version
        self.index_sha256 = pins.candidate_index.index_sha256
        self.error = error

    def query(self, _structure, *, limit: int):
        raise self.error


class _BackendOverrideIndex:
    def __init__(
        self,
        delegate,
        *,
        handle_backend: str | None = None,
        query_backend: str | None = None,
    ):
        self._delegate = delegate
        self.backend = handle_backend or delegate.backend
        self.snapshot_id = delegate.snapshot_id
        self.content_sha256 = delegate.content_sha256
        self.artifact_sha256 = delegate.artifact_sha256
        self.index_policy_version = delegate.index_policy_version
        self.index_sha256 = delegate.index_sha256
        self._query_backend = query_backend

    def query(self, structure, *, limit: int):
        result = self._delegate.query(structure, limit=limit)
        if self._query_backend is not None:
            result = result.model_copy(update={"backend": self._query_backend})
        return result


def _domain_counts(environment: _Environment) -> dict[str, int]:
    tables = {
        "legacy_snapshots": MarketSnapshot,
        "captures": MarketRulesCapture,
        "structures": MarketStructureRow,
        "candidate_sets": CandidateSet,
        "members": CandidateSetMember,
        "cards": FitCard,
        "recommendations": MarketRecommendation,
        "rejections": RejectedMarketRow,
    }
    with environment.sessions() as session:
        return {
            name: session.scalar(select(func.count()).select_from(table))
            for name, table in tables.items()
        }


def test_managed_descriptor_is_canonical_and_rejects_drifted_identity():
    descriptor = _managed_descriptor()
    rebuilt = ManagedAgentRetrievalDescriptor.model_validate(
        descriptor.model_dump(mode="json")
    )

    assert rebuilt.canonical_sha256 == descriptor.canonical_sha256
    assert len(descriptor.canonical_sha256) == 64
    assert descriptor.model_copy(
        update={"object_manifest_sha256": "4" * 64}
    ).canonical_sha256 != descriptor.canonical_sha256
    with pytest.raises(ValidationError, match="project/location"):
        _managed_descriptor(
            collection_resource=(
                "projects/different-project/locations/us-central1/"
                "collections/fixture-markets"
            )
        )
    with pytest.raises(ValidationError, match="descriptor SHA"):
        CandidateIndexPin(
            backend="google_agent_retrieval",
            snapshot_id=descriptor.snapshot_id,
            index_sha256="0" * 64,
            policy_version=AGENT_RETRIEVAL_INDEX_POLICY_VERSION,
            managed_descriptor=descriptor,
        )
    with pytest.raises(ValidationError, match="descriptor policies"):
        CandidateIndexPin(
            backend="google_agent_retrieval",
            snapshot_id=descriptor.snapshot_id,
            index_sha256=descriptor.canonical_sha256,
            policy_version="different-shadow-policy",
            managed_descriptor=descriptor,
        )
    with pytest.raises(ValidationError, match="object schema"):
        _managed_descriptor(
            object_schema={
                "version": "fitcheck-market-object-v1",
                "data_schema": {"type": "object", "properties": {}},
            }
        )


def test_direct_job_pins_active_snapshot_and_commits_one_complete_bundle(tmp_path):
    environment = _environment(tmp_path)
    submitted = environment.submit()
    job = environment.jobs.get(submitted.job_id)

    assert set(job.payload) == {"thesis_analysis_id"}
    assert job.pinned_manifest["snapshot"]["snapshot_id"] == (
        environment.snapshot_id
    )
    assert job.pinned_manifest["candidate_index"]["index_sha256"] == (
        environment.index_sha256
    )
    assert job.pinned_manifest["structure"]["model_version"]
    assert job.pinned_manifest["fit"]["advisory_enabled"] is False

    # A retry never consults a mutable active pointer after submission.
    with environment.sessions.begin() as session:
        session.execute(
            ActiveMarketUniverse.__table__.delete().where(
                ActiveMarketUniverse.provider == "fixture",
                ActiveMarketUniverse.venue == "test",
            )
        )

    result = environment.worker().run_once(now=NOW)

    assert result.status == "succeeded"
    assert result.fit_class == "direct"
    assert result.recommended_market_id == "mkt_gemini_lmsys_1"
    with environment.sessions() as session:
        candidate_set = session.scalars(select(CandidateSet)).one()
        card = session.scalars(select(FitCard)).one()
        recommendation = session.scalars(select(MarketRecommendation)).one()
        members = session.scalars(
            select(CandidateSetMember).order_by(CandidateSetMember.rank)
        ).all()
        assert candidate_set.job_id == submitted.job_id
        assert card.job_id == submitted.job_id
        assert recommendation.job_id == submitted.job_id
        assert card.candidate_set_id == candidate_set.id
        assert recommendation.fit_card_id == card.id
        assert recommendation.recommended_market_id == (
            "mkt_gemini_lmsys_1"
        )
        assert card.fit_confidence is None
        assert card.provenance["model_adapter"] == "deterministic-only"
        assert card.provenance["model_run_id"] == "deterministic"
        assert card.provenance["per_market"]["mkt_gemini_lmsys_1"][
            "published"
        ] == "direct"
        retrieval = card.provenance["retrieval"]
        assert retrieval["backend"] == "sqlite_lexical"
        assert retrieval["index_identity"] == environment.index_sha256
        assert len(retrieval["query_digest"]) == 64
        assert retrieval["backend_request_id"] is None
        assert [
            result["source_order"] for result in retrieval["source_results"]
        ] == list(range(1, len(retrieval["source_results"]) + 1))
        assert all(member.current_probability is None for member in members)
        assert all(
            member.eligibility_flags["liquidity"] == "unknown"
            for member in members
        )
    counts = _domain_counts(environment)
    assert counts["candidate_sets"] == 1
    assert counts["cards"] == 1
    assert counts["recommendations"] == 1
    assert counts["captures"] == counts["members"]


def test_worker_card_serializes_through_public_contract(tmp_path):
    environment = _environment(tmp_path)
    environment.submit()

    result = environment.worker().run_once(now=NOW)

    assert result.status == "succeeded"
    with environment.sessions() as session:
        card = session.get(FitCard, result.fit_card_id)
        recommendation = session.scalars(
            select(MarketRecommendation).where(
                MarketRecommendation.fit_card_id == card.id
            )
        ).one()
        rejections = session.scalars(
            select(RejectedMarketRow).where(
                RejectedMarketRow.market_recommendation_id
                == recommendation.id
            )
        ).all()
        dto = FitCardOut.model_validate(
            {
                "id": card.id,
                "thesis_analysis_id": card.thesis_analysis_id,
                "candidate_set_id": card.candidate_set_id,
                "semantic_fit_class": card.semantic_fit_class,
                "recommended_market_id": card.recommended_market_id,
                "what_it_captures": card.what_it_captures,
                "what_it_misses": card.what_it_misses,
                "horizon_match": card.horizon_match,
                "resolution_risk": card.resolution_risk,
                "rejected_markets": [
                    RejectedMarket(
                        market_id=rejection.market_id,
                        reason=rejection.reason,
                    )
                    for rejection in rejections
                ],
                "fit_confidence": card.fit_confidence,
                "draft_contract_id": card.draft_contract_id,
                "provenance": card.provenance,
            }
        )

    assert dto.semantic_fit_class.value == "direct"
    assert dto.provenance.model_adapter == "deterministic-only"
    assert dto.provenance.retrieval is not None
    assert dto.provenance.retrieval.backend == "sqlite_lexical"
    assert dto.provenance.retrieval.index_identity == environment.index_sha256
    assert dto.provenance.structure_extraction is not None
    assert dto.provenance.structure_extraction.structured_count == len(
        dto.provenance.per_market
    )
    proposer = dto.provenance.per_market["mkt_gemini_lmsys_1"][
        "structure_proposer"
    ]
    assert proposer["model_adapter"] == environment.proposer.model_adapter
    assert proposer["structure_sha256"]


def test_managed_backend_routes_through_injected_transport_and_persists_source_order(
    tmp_path,
):
    rows = _phase0_rows()
    environment = _environment(tmp_path, rows=rows)
    descriptor = _managed_descriptor(
        environment,
        expected_open_object_count=len(rows),
    )
    candidate_pin = _managed_pin(environment, descriptor)
    response_order = [rows[1], rows[0]]
    transport = _RecordingAgentRetrievalTransport(
        [
            _managed_hit(response_order[0], score=0.25),
            _managed_hit(response_order[1], score=0.91),
        ],
        request_id="agent-retrieval-request-1",
    )
    resolver = BackendRoutingIndexResolver(
        agent_retrieval_resolver=AgentRetrievalIndexResolver(transport)
    )
    submitted = environment.submit(candidate_index=candidate_pin)

    result = environment.worker(index_resolver=resolver).run_once(now=NOW)

    assert result.status == "succeeded"
    job = environment.jobs.get(submitted.job_id)
    assert job.pinned_manifest["candidate_index"]["backend"] == (
        "google_agent_retrieval"
    )
    assert job.pinned_manifest["candidate_index"]["index_sha256"] == (
        descriptor.canonical_sha256
    )
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.collection_resource == descriptor.collection_resource
    assert request.search_mode == "text"
    assert request.embedding_config == "disabled"
    assert request.fusion_policy_version == "disabled"
    with environment.sessions() as session:
        card = session.get(FitCard, result.fit_card_id)
        retrieval = card.provenance["retrieval"]
    assert retrieval == {
        "backend": "google_agent_retrieval",
        "index_identity": descriptor.canonical_sha256,
        "query_digest": retrieval["query_digest"],
        "backend_request_id": "agent-retrieval-request-1",
        "source_results": [
            {
                "market_id": response_order[0]["market_id"],
                "source_order": 1,
                "source_score": 0.25,
            },
            {
                "market_id": response_order[1]["market_id"],
                "source_order": 2,
                "source_score": 0.91,
            },
        ],
    }
    assert len(retrieval["query_digest"]) == 64


def test_managed_submission_rejects_incomplete_open_market_population(tmp_path):
    environment = _environment(tmp_path)
    descriptor = _managed_descriptor(
        environment,
        expected_open_object_count=2,
    )

    with pytest.raises(ValueError, match="object count"):
        environment.submit(candidate_index=_managed_pin(environment, descriptor))


def test_worker_detects_managed_population_drift_before_retrieval(tmp_path):
    environment = _environment(tmp_path)
    submitted = environment.submit(candidate_index=_managed_pin(environment))
    with environment.sessions.begin() as session:
        snapshot = session.get(MarketUniverseSnapshot, environment.snapshot_id)
        snapshot.open_market_count -= 1

    result = environment.worker().run_once(now=NOW)

    assert result.status == "needs_operator"
    job = environment.jobs.get(submitted.job_id)
    assert job.status == JobStatus.NEEDS_OPERATOR.value
    assert job.error_code == "candidate_index_integrity"
    assert not any(_domain_counts(environment).values())


@pytest.mark.parametrize(
    ("error", "result_status", "job_status", "error_code"),
    [
        (
            CandidateIndexTransientError("temporary"),
            "retry_wait",
            JobStatus.RETRY_WAIT.value,
            "candidate_index_transient",
        ),
        (
            CandidateIndexIntegrityError("drift"),
            "needs_operator",
            JobStatus.NEEDS_OPERATOR.value,
            "candidate_index_integrity",
        ),
        (
            CandidateIndexPermanentError("invalid"),
            "failed",
            JobStatus.FAILED.value,
            "candidate_index_permanent",
        ),
    ],
)
def test_candidate_index_errors_map_to_worker_dispositions(
    tmp_path,
    error,
    result_status,
    job_status,
    error_code,
):
    environment = _environment(tmp_path)
    submitted = environment.submit(max_attempts=2)

    def resolver(pins):
        return _FailingCandidateIndex(pins, error)

    result = environment.worker(index_resolver=resolver).run_once(now=NOW)

    assert result.status == result_status
    job = environment.jobs.get(submitted.job_id)
    assert job.status == job_status
    assert job.error_code == error_code
    assert not any(_domain_counts(environment).values())


@pytest.mark.parametrize(
    ("handle_backend", "query_backend"),
    [
        ("google_agent_retrieval", None),
        (None, "google_agent_retrieval"),
    ],
)
def test_worker_rejects_handle_and_query_backend_pin_mismatches(
    tmp_path,
    handle_backend,
    query_backend,
):
    environment = _environment(tmp_path)
    submitted = environment.submit()
    local_resolver = LocalSqliteIndexResolver()

    def resolver(pins):
        return _BackendOverrideIndex(
            local_resolver(pins),
            handle_backend=handle_backend,
            query_backend=query_backend,
        )

    result = environment.worker(index_resolver=resolver).run_once(now=NOW)

    assert result.status == "needs_operator"
    job = environment.jobs.get(submitted.job_id)
    assert job.status == JobStatus.NEEDS_OPERATOR.value
    assert job.error_code == "candidate_index_integrity"
    assert not any(_domain_counts(environment).values())


def test_mutated_thesis_after_submission_fails_closed_without_outputs(tmp_path):
    environment = _environment(tmp_path)
    submitted = environment.submit()
    changed = _claim(stance="no")
    with environment.sessions.begin() as session:
        analysis = session.get(ThesisAnalysis, environment.analysis_id)
        analysis.extracted_structure = changed.model_dump(mode="json")

    result = environment.worker().run_once(now=NOW)

    assert result.status == "failed"
    job = environment.jobs.get(submitted.job_id)
    assert job.error_code == "classification_input_invalid"
    assert not any(_domain_counts(environment).values())


def test_thesis_mutation_during_planning_is_rechecked_under_final_fence(tmp_path):
    environment = _environment(tmp_path)
    environment.submit()
    delegate = environment.proposer

    class MutatingProposer:
        model_adapter = delegate.model_adapter
        model_version = delegate.model_version
        prompt_version = delegate.prompt_version
        changed = False

        def propose_market_structure(self, **kwargs):
            result = delegate.propose_market_structure(**kwargs)
            if not self.changed:
                with environment.sessions.begin() as session:
                    analysis = session.get(
                        ThesisAnalysis, environment.analysis_id
                    )
                    analysis.extracted_structure = _claim(
                        stance="no"
                    ).model_dump(mode="json")
                self.changed = True
            return result

    result = environment.worker(MutatingProposer()).run_once(now=NOW)

    assert result.status == "failed"
    assert not any(_domain_counts(environment).values())


def test_worker_rejects_proposer_identity_different_from_pin(tmp_path):
    environment = _environment(tmp_path)
    environment.submit()
    delegate = FixtureMarketStructureProposer(_phase0_structures())
    wrong = BoundMarketStructureProposer(
        delegate=delegate,
        model_adapter="fixture",
        model_version="different-fixture-version",
        prompt_version=environment.config.structure_prompt_version,
    )

    result = environment.worker(wrong).run_once(now=NOW)

    assert result.status == "failed"
    assert delegate.calls == 0
    assert not any(_domain_counts(environment).values())


def test_no_clean_expression_is_a_successful_worker_result(tmp_path):
    unmatched = _claim(
        claim_summary="Zzcorp wins the underwater basket weaving cup in 2026.",
        entities=[Entity(name="Zzcorp", role="subject")],
        metric=Metric(
            what="underwater basket weaving championship outcome",
            measured_by="nobody",
            objective=True,
        ),
        resolution_source_class="press",
        contractible_version="Will Zzcorp win the cup by Dec 31, 2026?",
    )
    environment = _environment(tmp_path, claim=unmatched)
    environment.submit()

    result = environment.worker().run_once(now=NOW)

    assert result.status == "succeeded"
    assert result.fit_class == "no_clean_expression"
    assert result.recommended_market_id is None
    with environment.sessions() as session:
        card = session.scalars(select(FitCard)).one()
        recommendation = session.scalars(select(MarketRecommendation)).one()
        assert card.semantic_fit_class == "no_clean_expression"
        assert recommendation.recommended_market_id is None
        assert recommendation.contract_terms_hash is None


def test_index_pin_mismatch_fails_without_domain_rows(tmp_path):
    environment = _environment(tmp_path)
    submitted = environment.submit()
    connection = sqlite3.connect(environment.index_path)
    try:
        connection.execute("UPDATE markets SET is_open = 0")
        connection.commit()
    finally:
        connection.close()

    result = environment.worker().run_once(now=NOW)

    assert result.status == "needs_operator"
    job = environment.jobs.get(submitted.job_id)
    assert job.status == JobStatus.NEEDS_OPERATOR.value
    assert job.error_code == "candidate_index_integrity"
    assert not any(_domain_counts(environment).values())


def test_owner_cancellation_between_model_checkpoints_persists_nothing(tmp_path):
    environment = _environment(tmp_path)
    submitted = environment.submit()
    delegate = environment.proposer

    class CancellingProposer:
        model_adapter = delegate.model_adapter
        model_version = delegate.model_version
        prompt_version = delegate.prompt_version

        def propose_market_structure(self, **kwargs):
            result = delegate.propose_market_structure(**kwargs)
            assert environment.jobs.cancel_owned(
                submitted.job_id,
                owner_client_type=environment.owner_client_type,
                owner_actor_id=environment.owner_actor_id,
                now=NOW,
            )
            return result

    result = environment.worker(CancellingProposer()).run_once(now=NOW)

    assert result.status == "lost_authority"
    assert environment.jobs.get(submitted.job_id).status == (
        JobStatus.CANCELLED.value
    )
    assert not any(_domain_counts(environment).values())


def test_reclaimed_attempt_fences_stale_worker_before_persistence(tmp_path):
    environment = _environment(tmp_path)
    submitted = environment.submit()
    delegate = environment.proposer

    class ReclaimingProposer:
        model_adapter = delegate.model_adapter
        model_version = delegate.model_version
        prompt_version = delegate.prompt_version
        fresh_claim = None

        def propose_market_structure(self, **kwargs):
            with environment.sessions.begin() as session:
                job = session.get(Job, submitted.job_id)
                assert job is not None and job.active_attempt_id is not None
                session.execute(
                    update(Job)
                    .where(Job.id == submitted.job_id)
                    .values(lease_expires_at=NOW - timedelta(seconds=1))
                )
                session.execute(
                    update(JobAttempt)
                    .where(JobAttempt.id == job.active_attempt_id)
                    .values(lease_expires_at=NOW - timedelta(seconds=1))
                )
            self.fresh_claim = environment.jobs.claim_due(
                worker_id="fresh-classification-worker",
                lease_seconds=30,
                job_types=["fit_classification"],
                now=NOW,
            )
            assert self.fresh_claim is not None
            return delegate.propose_market_structure(**kwargs)

    proposer = ReclaimingProposer()
    result = environment.worker(proposer).run_once(now=NOW)

    assert result.status == "lost_authority"
    assert proposer.fresh_claim is not None
    assert environment.jobs.get(submitted.job_id).active_attempt_id == (
        proposer.fresh_claim.attempt_id
    )
    assert not any(_domain_counts(environment).values())


def test_final_write_failure_rolls_back_then_retry_commits_one_bundle(tmp_path):
    environment = _environment(tmp_path)
    submitted = environment.submit(max_attempts=2)

    def fail_once(_mapper, _connection, _target):
        raise RuntimeError("injected recommendation write failure")

    event.listen(MarketRecommendation, "before_insert", fail_once, once=True)
    first = environment.worker().run_once(now=NOW)

    assert first.status == "retry_wait"
    assert environment.jobs.get(submitted.job_id).status == (
        JobStatus.RETRY_WAIT.value
    )
    assert not any(_domain_counts(environment).values())

    second = environment.worker().run_once(now=NOW + timedelta(seconds=1))

    assert second.status == "succeeded"
    counts = _domain_counts(environment)
    assert counts["candidate_sets"] == 1
    assert counts["cards"] == 1
    assert counts["recommendations"] == 1
    with environment.sessions() as session:
        assert session.scalar(
            select(func.count()).select_from(CandidateSet).where(
                CandidateSet.job_id == submitted.job_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(FitCard).where(
                FitCard.job_id == submitted.job_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(MarketRecommendation).where(
                MarketRecommendation.job_id == submitted.job_id
            )
        ) == 1


def test_oversized_selected_market_id_fails_before_model_or_domain_writes(tmp_path):
    market_id = "x" * 129
    rows = [
        {
            "market_id": market_id,
            "title": "Google Gemini ranks first on LMSYS Chatbot Arena",
            "slug": "oversized-market",
            "description": "Gemini leaderboard contract.",
            "resolution_rules": "Resolves from the LMSYS leaderboard.",
            "outcomes": ["Yes", "No"],
            "token_ids": ["oversized-yes", "oversized-no"],
            "close_date": "2026-12-31",
            "closed_time": None,
            "snapshot_ts": "2026-05-22T00:00:00+00:00",
            "is_open": True,
            "volume_usd": 1000,
            "taxonomy_l1": "technology",
            "taxonomy_confidence": 0.9,
            "tags": [],
            "source_url": "https://example.test/oversized",
        }
    ]
    golden = _phase0_structures()["mkt_gemini_lmsys_1"].model_copy(
        update={"market_id": market_id}
    )
    proposer = FixtureMarketStructureProposer({market_id: golden})
    environment = _environment(
        tmp_path,
        rows=rows,
        structures={market_id: golden},
    )
    environment.proposer = BoundMarketStructureProposer(
        delegate=proposer,
        model_adapter=environment.config.structure_model_adapter,
        model_version=environment.config.structure_model_version,
        prompt_version=environment.config.structure_prompt_version,
    )
    environment.submit()

    result = environment.worker().run_once(now=NOW)

    assert result.status == "failed"
    assert proposer.calls == 0
    assert not any(_domain_counts(environment).values())


def test_more_than_128_selected_market_ids_fails_without_truncation(tmp_path):
    rows = [_market_row(f"candidate-{number:03d}") for number in range(129)]
    environment = _environment(
        tmp_path,
        rows=rows,
        query_limit=129,
        structure_limit=129,
    )
    environment.submit()
    delegate = environment.proposer.delegate

    result = environment.worker().run_once(now=NOW)

    assert result.status == "failed"
    assert delegate.calls == 0
    assert not any(_domain_counts(environment).values())


def test_market_observation_timestamp_is_preserved_in_output_provenance(tmp_path):
    market_id = "mkt_gemini_lmsys_1"
    source_time = datetime(2026, 5, 21, 15, 30, tzinfo=timezone.utc)
    row = _market_row(market_id, snapshot_ts=source_time.isoformat())
    environment = _environment(
        tmp_path,
        rows=[row],
        structures={market_id: _phase0_structures()[market_id]},
    )
    environment.submit()

    result = environment.worker().run_once(now=NOW)

    assert result.status == "succeeded"
    with environment.sessions() as session:
        capture = session.scalars(select(MarketRulesCapture)).one()
        recommendation = session.scalars(select(MarketRecommendation)).one()
        captured_at = capture.rules_captured_at.replace(tzinfo=timezone.utc)
        as_of = recommendation.as_of_ts.replace(tzinfo=timezone.utc)
        assert captured_at == source_time
        assert as_of == source_time


def test_fit_horizon_tolerances_are_pinned_and_executed(tmp_path):
    market_id = "mkt_gemini_lmsys_1"
    row = _market_row(market_id)
    base = _phase0_structures()[market_id]
    shifted = base.model_copy(
        update={
            "horizon": MarketHorizon(
                resolution_date=date(2026, 12, 20),
                timezone="America/New_York",
            )
        }
    )
    default_environment = _environment(
        tmp_path / "default",
        rows=[row],
        structures={market_id: shifted},
    )
    widened_tolerances = dict(FitPolicy().horizon_tolerances)
    widened_tolerances["day"] = (14, 28)
    custom_environment = _environment(
        tmp_path / "custom",
        rows=[row],
        structures={market_id: shifted},
        fit_policy=FitPolicy(horizon_tolerances=widened_tolerances),
    )
    default_job = default_environment.submit()
    custom_job = custom_environment.submit()

    default_result = default_environment.worker().run_once(now=NOW)
    custom_result = custom_environment.worker().run_once(now=NOW)

    assert default_environment.jobs.get(default_job.job_id).pinned_manifest[
        "fit"
    ]["horizon_tolerances"]["day"] == [7, 14]
    assert custom_environment.jobs.get(custom_job.job_id).pinned_manifest[
        "fit"
    ]["horizon_tolerances"]["day"] == [14, 28]
    assert default_result.fit_class == "indirect"
    assert custom_result.fit_class == "direct"


def test_conflicting_structure_cache_is_reverified_under_final_fence(tmp_path):
    environment = _environment(tmp_path)
    environment.submit()
    row = _phase0_rows()[0]
    golden = _phase0_structures()[row["market_id"]]
    conflicting = golden.model_copy(update={"event_stage": EventStage.LAUNCHED})
    with environment.sessions.begin() as session:
        universe = session.get(MarketUniverseSnapshot, environment.snapshot_id)
        session.add(
            MarketSnapshot(
                id=environment.snapshot_id,
                venue_id="test",
                as_of_ts=universe.cutoff_utc,
                retrieval_id=f"universe_{universe.content_sha256[:16]}",
            )
        )
        session.add(
            MarketStructureRow(
                market_id=row["market_id"],
                contract_terms_hash=hashlib.sha256(
                    row["title"].encode()
                ).hexdigest(),
                resolution_rules_hash=hashlib.sha256(
                    row["resolution_rules"].encode()
                ).hexdigest(),
                snapshot_id=environment.snapshot_id,
                schema_version=1,
                structure=conflicting.model_copy(
                    update={"snapshot_id": environment.snapshot_id}
                ).model_dump(mode="json"),
                extraction_policy_version=2,
            )
        )

    result = environment.worker().run_once(now=NOW)

    assert result.status == "failed"
    counts = _domain_counts(environment)
    assert counts["legacy_snapshots"] == 1
    assert counts["structures"] == 1
    assert counts["candidate_sets"] == 0
    assert counts["cards"] == 0
    assert counts["recommendations"] == 0
    assert counts["captures"] == 0


def test_classification_package_is_private_and_provider_independent():
    package = Path(__file__).parents[1] / "el" / "classification"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in package.glob("*.py")
    )
    assert "poly_data_client" not in source
    assert "google.genai" not in source
    assert "el.product" not in source
    assert "el.mcp" not in source
