#!/usr/bin/env python3
"""Disposable structured-market validation for Google Agent Retrieval.

The spike deliberately avoids RAG Engine, auto-embeddings, GCS imports, and
ANN indexes. It creates one short-lived Collection, writes three synthetic
structured market contracts, validates filtered KNN and full-text search plus
ETag fencing, and then deletes the Collection.

Dry-run is the default. Pass ``--execute`` only after the cloud mutation scope
has been approved.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from el.classification.contracts import (
    CandidateIndexPin,
    ManagedAgentRetrievalDescriptor,
    SnapshotPin,
)
from el.classification.worker import AgentRetrievalIndexResolver
from el.domain.structures import (
    ClaimHorizon,
    Entity,
    ExtractedStructure,
    Mechanism,
    Metric,
)
from el.retrieval.agent_retrieval_index import (
    AGENT_RETRIEVAL_INDEX_POLICY_VERSION,
    AGENT_RETRIEVAL_OBJECT_SCHEMA_VERSION,
    AgentRetrievalSearchRequest,
    AgentRetrievalSearchResponse,
    agent_retrieval_data_schema,
    agent_retrieval_object_schema_descriptor,
    agent_retrieval_object_manifest_sha256,
    agent_retrieval_object_sha256,
    normalize_agent_retrieval_data,
    serialize_agent_retrieval_market,
)
from el.retrieval.candidate_contracts import CandidateIndexIntegrityError
from el.retrieval.snapshot_contracts import NormalizedUniverseMarket


DEFAULT_LOCATION = "us-central1"
REPORT_SCHEMA = "fitcheck_agent_retrieval_spike_v3"
COLLECTION_PREFIX = "fitcheck-ar-spike-"
SNAPSHOT_ID = "spike-snapshot-v1"
CONTENT_SHA256 = "a" * 64
ARTIFACT_SHA256 = "b" * 64
SUPPORTED_LOCATIONS = {
    "asia-east1",
    "asia-northeast1",
    "asia-southeast1",
    "europe-north1",
    "europe-west2",
    "europe-west4",
    "us-central1",
    "us-east4",
    "us-west1",
}
_COLLECTION_ID = re.compile(r"^[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?$")


class SpikeError(RuntimeError):
    """The cloud contract did not satisfy the bounded spike."""


def data_schema() -> dict[str, Any]:
    return agent_retrieval_data_schema()


def vector_schema() -> dict[str, Any]:
    return {"market_embedding": {"denseVector": {"dimensions": 3}}}


def fixture_objects() -> tuple[dict[str, Any], ...]:
    observed_at = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    common = {
        "close_date": date(2027, 1, 1),
        "snapshot_ts": observed_at,
        "volume_usd": 1000.0,
        "taxonomy_confidence": 0.9,
        "tags": [],
    }
    target = NormalizedUniverseMarket(
        **common,
        market_id="polydata:fed-cut",
        title="Will the Federal Reserve cut rates in 2026?",
        slug="federal-reserve-cut-rates-2026",
        description="Federal Reserve interest-rate decision contract.",
        resolution_rules="Resolves Yes after an official target-rate cut.",
        outcomes=["Yes", "No"],
        token_ids=["fed-cut-yes", "fed-cut-no"],
        closed_time=None,
        is_open=True,
        taxonomy_l1="monetary-policy",
        source_url="https://example.test/fed-cut",
    )
    cpi = NormalizedUniverseMarket(
        **common,
        market_id="polydata:cpi",
        title="Will US CPI exceed four percent in 2026?",
        slug="us-cpi-four-percent-2026",
        description="US inflation release contract.",
        resolution_rules="Resolves from the official CPI release.",
        outcomes=["Yes", "No"],
        token_ids=["cpi-yes", "cpi-no"],
        closed_time=None,
        is_open=True,
        taxonomy_l1="inflation",
        source_url="https://example.test/cpi",
    )
    closed = NormalizedUniverseMarket(
        **common,
        market_id="polydata:fed-cut-closed",
        title="Did the Federal Reserve cut rates in 2025?",
        slug="federal-reserve-cut-rates-2025",
        description="Closed Federal Reserve decision contract.",
        resolution_rules="Resolved from the official target rate.",
        outcomes=["Yes", "No"],
        token_ids=["fed-cut-closed-yes", "fed-cut-closed-no"],
        closed_time=observed_at,
        is_open=False,
        taxonomy_l1="monetary-policy",
        source_url="https://example.test/fed-cut-closed",
    )
    return (
        {
            "id": "market-fed-cut",
            "data": _stored_market(target),
            "vectors": {
                "market_embedding": {"dense": {"values": [1.0, 0.0, 0.0]}}
            },
        },
        {
            "id": "market-cpi",
            "data": _stored_market(cpi),
            "vectors": {
                "market_embedding": {"dense": {"values": [0.0, 1.0, 0.0]}}
            },
        },
        {
            "id": "market-fed-cut-closed",
            "data": _stored_market(closed),
            "vectors": {
                "market_embedding": {"dense": {"values": [1.0, 0.0, 0.0]}}
            },
        },
    )


def _stored_market(market: NormalizedUniverseMarket) -> dict[str, Any]:
    return serialize_agent_retrieval_market(
        market,
        snapshot_id=SNAPSHOT_ID,
        content_sha256=CONTENT_SHA256,
    )


def _market_from_stored(data: dict[str, Any]) -> NormalizedUniverseMarket:
    return NormalizedUniverseMarket.model_validate(json.loads(data["market_json"]))


def _claim() -> ExtractedStructure:
    return ExtractedStructure(
        claim_summary="Federal Reserve cut rates",
        entities=[Entity(name="Federal Reserve", role="subject")],
        event_stage="measured",
        metric=Metric(
            what="Federal Reserve rate cut",
            measured_by="Federal Reserve",
            objective=True,
        ),
        horizon=ClaimHorizon(window_end=date(2026, 12, 31), precision="day"),
        mechanism=Mechanism(),
        stance="yes",
        resolution_source_class="official",
        contractible_version="Will the Federal Reserve cut rates?",
    )


def collection_id() -> str:
    value = f"{COLLECTION_PREFIX}{uuid.uuid4().hex[:12]}"
    if not _COLLECTION_ID.fullmatch(value):
        raise AssertionError("generated collection ID is invalid")
    return value


def market_ids(search_payload: Any) -> list[str]:
    rows = _search_rows(search_payload)
    values: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise SpikeError("search result row is malformed")
        data_object = row.get("dataObject") or row.get("data_object") or row
        data = data_object.get("data") if isinstance(data_object, dict) else None
        market_id = data.get("market_id") if isinstance(data, dict) else None
        if not isinstance(market_id, str) or not market_id:
            raise SpikeError("search result omitted structured market_id")
        values.append(market_id)
    return values


def _search_rows(search_payload: Any) -> list[dict[str, Any]]:
    if isinstance(search_payload, dict):
        rows = search_payload.get("results", [])
    elif isinstance(search_payload, list):
        rows = search_payload
    else:
        raise SpikeError("search response is not a JSON object or list")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SpikeError("search response results are malformed")
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _json_output(completed: subprocess.CompletedProcess[str]) -> Any:
    output = completed.stdout.strip()
    return json.loads(output) if output else None


def _run(
    args: list[str],
    *,
    check: bool = True,
    timeout_seconds: int = 300,
) -> subprocess.CompletedProcess[str]:
    command = ["gcloud", *args]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        if check:
            raise SpikeError("gcloud command timed out") from error
        return subprocess.CompletedProcess(
            command,
            124,
            stdout="",
            stderr="TIMEOUT",
        )
    except OSError as error:
        if check:
            raise SpikeError("gcloud command could not start") from error
        return subprocess.CompletedProcess(
            command,
            127,
            stdout="",
            stderr="PROCESS_ERROR",
        )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-12:]
        raise SpikeError(
            f"gcloud command failed ({completed.returncode}): "
            f"{' | '.join(detail) if detail else 'no diagnostic'}"
        )
    return completed


def _is_stale_etag_rejection(
    completed: subprocess.CompletedProcess[str],
) -> bool:
    return completed.returncode != 0 and "ABORTED" in completed.stderr.upper()


def _error_code(completed: subprocess.CompletedProcess[str]) -> str:
    diagnostic = completed.stderr.upper()
    for code in (
        "ABORTED",
        "NOT_FOUND",
        "FAILED_PRECONDITION",
        "UNAVAILABLE",
        "DEADLINE_EXCEEDED",
        "PERMISSION_DENIED",
        "INVALID_ARGUMENT",
        "TIMEOUT",
        "PROCESS_ERROR",
    ):
        if code in diagnostic:
            return code
    return "NONE" if completed.returncode == 0 else "UNKNOWN"


def _gcloud_version() -> str:
    completed = _run(["version", "--format=json"], check=False)
    if completed.returncode != 0:
        return "unknown"
    try:
        payload = _json_output(completed)
    except json.JSONDecodeError:
        return "unknown"
    value = payload.get("Google Cloud SDK") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value else "unknown"


def _base(project: str, location: str, collection: str) -> list[str]:
    return [
        f"--project={project}",
        f"--location={location}",
        f"--collection={collection}",
    ]


def _strict_filter(request: AgentRetrievalSearchRequest) -> dict[str, Any]:
    return {
        "$and": [
            {"snapshot_id": {"$eq": request.filters.snapshot_id}},
            {"content_sha256": {"$eq": request.filters.content_sha256}},
            {"is_open": {"$eq": request.filters.is_open}},
        ]
    }


class GcloudAgentRetrievalTransport:
    """Disposable CLI transport that also attests the known spike population.

    This is test harness code, not the production SDK transport.  Its bounded
    list of deterministic object IDs lets the spike verify the full open-object
    manifest before returning search results to the application adapter.
    """

    def __init__(
        self,
        *,
        project: str,
        location: str,
        collection: str,
        population_object_ids: tuple[str, ...],
    ) -> None:
        self.project = project
        self.location = location
        self.collection = collection
        self.population_object_ids = population_object_ids
        self.collection_resource = (
            f"projects/{project}/locations/{location}/collections/{collection}"
        )

    def _attest_open_population(
        self,
        request: AgentRetrievalSearchRequest,
    ) -> tuple[str, int, dict[str, str]]:
        open_objects: list[dict[str, Any]] = []
        for object_id in self.population_object_ids:
            described = _json_output(
                _run(
                    [
                        "vector-search",
                        "collections",
                        "data-objects",
                        "describe",
                        object_id,
                        *_base(
                            self.project,
                            self.location,
                            self.collection,
                        ),
                        "--format=json",
                    ]
                )
            )
            data = described.get("data") if isinstance(described, dict) else None
            if not isinstance(data, dict):
                raise SpikeError("described Data Object omitted structured data")
            if (
                data.get("snapshot_id") == request.filters.snapshot_id
                and data.get("content_sha256")
                == request.filters.content_sha256
                and data.get("is_open") is True
            ):
                open_objects.append(data)
        return (
            agent_retrieval_object_manifest_sha256(open_objects),
            len(open_objects),
            {
                str(data["market_id"]): agent_retrieval_object_sha256(data)
                for data in open_objects
            },
        )

    def _observe_collection_times(self) -> tuple[str, str]:
        state = _json_output(
            _run(
                [
                    "vector-search",
                    "collections",
                    "describe",
                    self.collection,
                    f"--project={self.project}",
                    f"--location={self.location}",
                    "--format=json",
                ]
            )
        )
        if not isinstance(state, dict) or state.get("name") != self.collection_resource:
            raise CandidateIndexIntegrityError(
                "collection resource identity changed before search"
            )
        create_time = state.get("createTime", state.get("create_time"))
        update_time = state.get("updateTime", state.get("update_time"))
        if not isinstance(create_time, str) or not isinstance(update_time, str):
            raise CandidateIndexIntegrityError(
                "collection identity timestamps are missing"
            )
        return create_time, update_time

    def search(
        self,
        request: AgentRetrievalSearchRequest,
    ) -> AgentRetrievalSearchResponse:
        if request.collection_resource != self.collection_resource:
            raise SpikeError("adapter requested a different collection")
        if request.search_mode != "text":
            raise SpikeError("spike transport only ratifies text search")
        observed_create_time, observed_update_time = (
            self._observe_collection_times()
        )
        (
            observed_manifest,
            observed_count,
            attested_object_hashes,
        ) = self._attest_open_population(request)
        strict_filter = _strict_filter(request)
        payload = _json_output(
            _run(
                [
                    "vector-search",
                    "collections",
                    "data-objects",
                    "search",
                    *_base(self.project, self.location, self.collection),
                    f"--text-search-text={request.query_text}",
                    (
                        "--text-search-data-fields="
                        "title,description,resolution_rules"
                    ),
                    "--json-filter="
                    + json.dumps(strict_filter, separators=(",", ":")),
                    f"--top-k={request.limit}",
                    "--output-data-fields=*",
                    "--output-metadata-fields=*",
                    "--format=json",
                ],
                timeout_seconds=max(1, round(request.timeout_seconds)),
            )
        )
        hits: list[dict[str, Any]] = []
        for row in _search_rows(payload):
            data_object = row.get("dataObject") or row.get("data_object") or row
            data = (
                data_object.get("data")
                if isinstance(data_object, dict)
                else None
            )
            if not isinstance(data, dict):
                raise SpikeError("search result omitted structured market data")
            raw_score = row.get("score")
            score = (
                float(raw_score)
                if isinstance(raw_score, (int, float))
                else None
            )
            normalized = normalize_agent_retrieval_data(data, score=score)
            market_id = normalized["market_id"]
            if (
                market_id not in attested_object_hashes
                or agent_retrieval_object_sha256(data)
                != attested_object_hashes[market_id]
            ):
                raise CandidateIndexIntegrityError(
                    "search returned bytes outside the attested fixture set"
                )
            hits.append(normalized)
        request_id = payload.get("requestId") if isinstance(payload, dict) else None
        if not isinstance(request_id, str) or not request_id:
            request_id = None
        return AgentRetrievalSearchResponse(
            collection_resource=self.collection_resource,
            collection_create_time=observed_create_time,
            collection_update_time=observed_update_time,
            snapshot_id=request.filters.snapshot_id,
            content_sha256=request.filters.content_sha256,
            object_schema_version=AGENT_RETRIEVAL_OBJECT_SCHEMA_VERSION,
            object_manifest_sha256=observed_manifest,
            open_object_count=observed_count,
            hits=tuple(hits),
            request_id=request_id,
        )


def execute_spike(
    *,
    project: str,
    location: str,
    report_path: Path,
) -> dict[str, Any]:
    if not project.strip():
        raise SpikeError("project is required")
    if location not in SUPPORTED_LOCATIONS:
        raise SpikeError("location is not supported by Agent Retrieval")

    collection = collection_id()
    started = time.monotonic()
    run_started_at = datetime.now(timezone.utc)
    fixtures = fixture_objects()
    open_fixture_data = tuple(
        fixture["data"] for fixture in fixtures if fixture["data"]["is_open"]
    )
    object_manifest_sha256 = agent_retrieval_object_manifest_sha256(
        open_fixture_data
    )
    expected_open_object_count = len(open_fixture_data)
    collection_resource = (
        f"projects/{project}/locations/{location}/collections/{collection}"
    )
    gcloud_version = _gcloud_version()
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "run_started_at_utc": run_started_at.isoformat(),
        "run_completed_at_utc": None,
        "api_version": "v1",
        "gcloud_version": gcloud_version,
        "project": project,
        "location": location,
        "collection_id": collection,
        "collection_resource": collection_resource,
        "collection_created": False,
        "collection_deleted": False,
        "collection_absent_verified": False,
        "objects_created": 0,
        "objects_deleted": 0,
        "expected_open_object_count": expected_open_object_count,
        "attestation_scope": "exclusive_fixture_ids",
        "object_manifest_sha256": object_manifest_sha256,
        "index_sha256": None,
        "collection_create_time": None,
        "collection_update_time": None,
        "knn_market_ids": [],
        "text_market_ids": [],
        "expanded_text_market_ids": [],
        "adapter_market_ids": [],
        "adapter_query_digest": None,
        "etag_changed": False,
        "stale_etag_rejected": False,
        "stale_etag_error_code": None,
        "orphan_object_ids": [],
        "orphan_collection": None,
        "cleanup_error_codes": [],
        "passed": False,
    }

    primary_error: Exception | None = None
    with tempfile.TemporaryDirectory(prefix="fitcheck-agent-retrieval-") as root:
        temp_root = Path(root)
        data_schema_path = temp_root / "data-schema.json"
        vector_schema_path = temp_root / "vector-schema.json"
        query_vector_path = temp_root / "query-vector.json"
        _write_json(data_schema_path, data_schema())
        _write_json(vector_schema_path, vector_schema())
        _write_json(
            query_vector_path,
            {"dense": {"values": [1.0, 0.0, 0.0]}},
        )

        # Register every deterministic target before any network create.  An
        # ambiguous client timeout must not remove a possible server-side
        # success from cleanup scope.
        created_object_ids = [fixture["id"] for fixture in fixtures]
        try:
            _run(
                [
                    "vector-search",
                    "collections",
                    "create",
                    collection,
                    f"--project={project}",
                    f"--location={location}",
                    "--display-name=FitCheck structured-market spike",
                    "--description=Disposable Agent Retrieval validation",
                    f"--data-schema={data_schema_path}",
                    f"--vector-schema={vector_schema_path}",
                    "--labels=purpose=fitcheck-spike",
                    f"--request-id={uuid.uuid4()}",
                    "--quiet",
                    "--format=json",
                ]
            )
            report["collection_created"] = True

            for fixture in fixtures:
                object_id = fixture["id"]
                data_path = temp_root / f"{object_id}-data.json"
                vectors_path = temp_root / f"{object_id}-vectors.json"
                _write_json(data_path, fixture["data"])
                _write_json(vectors_path, fixture["vectors"])
                _run(
                    [
                        "vector-search",
                        "collections",
                        "data-objects",
                        "create",
                        object_id,
                        *_base(project, location, collection),
                        f"--data={data_path}",
                        f"--vectors={vectors_path}",
                        "--format=json",
                    ]
                )
                report["objects_created"] += 1

            collection_state = _json_output(
                _run(
                    [
                        "vector-search",
                        "collections",
                        "describe",
                        collection,
                        f"--project={project}",
                        f"--location={location}",
                        "--format=json",
                    ]
                )
            )
            if not isinstance(collection_state, dict):
                raise SpikeError("Collection describe response is malformed")
            observed_resource = collection_state.get("name")
            create_time = collection_state.get(
                "createTime", collection_state.get("create_time")
            )
            update_time = collection_state.get(
                "updateTime", collection_state.get("update_time")
            )
            if observed_resource != collection_resource:
                raise SpikeError("Collection resource identity drifted")
            if not isinstance(create_time, str) or not isinstance(
                update_time, str
            ):
                raise SpikeError("Collection timestamps are missing")
            descriptor = ManagedAgentRetrievalDescriptor.model_validate(
                {
                    "project_id": project,
                    "location": location,
                    "api_version": "v1",
                    "collection_resource": collection_resource,
                    "snapshot_id": SNAPSHOT_ID,
                    "content_sha256": CONTENT_SHA256,
                    "artifact_sha256": ARTIFACT_SHA256,
                    "object_manifest_sha256": object_manifest_sha256,
                    "create_time": create_time,
                    "update_time": update_time,
                    "expected_open_object_count": (
                        expected_open_object_count
                    ),
                    "object_schema": (
                        agent_retrieval_object_schema_descriptor()
                    ),
                    "query_policy_version": (
                        AGENT_RETRIEVAL_INDEX_POLICY_VERSION
                    ),
                    "search_mode": "text",
                    "embedding_config": "disabled",
                    "fusion_policy_version": "disabled",
                }
            )
            candidate_pin = CandidateIndexPin(
                backend="google_agent_retrieval",
                snapshot_id=SNAPSHOT_ID,
                index_sha256=descriptor.canonical_sha256,
                policy_version=AGENT_RETRIEVAL_INDEX_POLICY_VERSION,
                managed_descriptor=descriptor,
            )
            snapshot_pin = SnapshotPin(
                snapshot_id=SNAPSHOT_ID,
                provider="synthetic",
                venue="synthetic",
                cutoff_utc=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
                content_sha256=CONTENT_SHA256,
                artifact_sha256=ARTIFACT_SHA256,
                manifest_sha256="d" * 64,
                artifact_uri="memory://fitcheck-agent-retrieval-spike",
                normalization_policy_version="spike-v1",
                validation_policy_version="spike-v1",
            )
            report["index_sha256"] = descriptor.canonical_sha256
            report["collection_create_time"] = descriptor.create_time.isoformat()
            report["collection_update_time"] = descriptor.update_time.isoformat()

            exact_request = AgentRetrievalSearchRequest(
                collection_resource=collection_resource,
                expected_collection_create_time=descriptor.create_time,
                expected_collection_update_time=descriptor.update_time,
                query_text="Federal Reserve cut rates",
                search_mode="text",
                embedding_config="disabled",
                fusion_policy_version="disabled",
                filters={
                    "snapshot_id": SNAPSHOT_ID,
                    "content_sha256": CONTENT_SHA256,
                    "is_open": True,
                },
                expected_object_manifest_sha256=object_manifest_sha256,
                expected_open_object_count=expected_open_object_count,
                limit=3,
                timeout_seconds=15,
            )
            knn_filter = _strict_filter(exact_request)
            knn = _json_output(
                _run(
                    [
                        "vector-search",
                        "collections",
                        "data-objects",
                        "search",
                        *_base(project, location, collection),
                        "--vector-search-field=market_embedding",
                        f"--vector-from-file={query_vector_path}",
                        "--use-knn",
                        "--distance-metric=dot-product",
                        f"--json-filter={json.dumps(knn_filter, separators=(',', ':'))}",
                        "--top-k=3",
                        "--output-data-fields=*",
                        "--output-metadata-fields=*",
                        "--format=json",
                    ]
                )
            )
            report["knn_market_ids"] = market_ids(knn)

            text_filter = _strict_filter(exact_request)
            text = _json_output(
                _run(
                    [
                        "vector-search",
                        "collections",
                        "data-objects",
                        "search",
                        *_base(project, location, collection),
                        "--text-search-text=Federal Reserve cut rates",
                        (
                            "--text-search-data-fields="
                            "title,description,resolution_rules"
                        ),
                        f"--json-filter={json.dumps(text_filter, separators=(',', ':'))}",
                        "--top-k=3",
                        "--output-data-fields=*",
                        "--output-metadata-fields=*",
                        "--format=json",
                    ]
                )
            )
            report["text_market_ids"] = market_ids(text)

            expanded_text = _json_output(
                _run(
                    [
                        "vector-search",
                        "collections",
                        "data-objects",
                        "search",
                        *_base(project, location, collection),
                        (
                            "--text-search-text=Federal Reserve Federal "
                            "Reserve cut rates Will the Federal Reserve cut "
                            "rates? Federal Reserve rate cut Federal Reserve"
                        ),
                        (
                            "--text-search-data-fields="
                            "title,description,resolution_rules"
                        ),
                        f"--json-filter={json.dumps(text_filter, separators=(',', ':'))}",
                        "--top-k=3",
                        "--output-data-fields=*",
                        "--output-metadata-fields=*",
                        "--format=json",
                    ]
                )
            )
            report["expanded_text_market_ids"] = market_ids(expanded_text)

            transport = GcloudAgentRetrievalTransport(
                project=project,
                location=location,
                collection=collection,
                population_object_ids=tuple(created_object_ids),
            )
            adapter_result = AgentRetrievalIndexResolver(
                transport
            ).resolve_pinned(
                candidate_pin=candidate_pin,
                snapshot=snapshot_pin,
            ).query(_claim(), limit=3)
            report["adapter_market_ids"] = [
                hit.market.market_id for hit in adapter_result.hits
            ]
            report["adapter_query_digest"] = adapter_result.query_digest

            described = _json_output(
                _run(
                    [
                        "vector-search",
                        "collections",
                        "data-objects",
                        "describe",
                        "market-fed-cut",
                        *_base(project, location, collection),
                        "--format=json",
                    ]
                )
            )
            old_etag = described.get("etag") if isinstance(described, dict) else None
            if not isinstance(old_etag, str) or not old_etag:
                raise SpikeError("data object response omitted ETag")

            update_path = temp_root / "market-fed-cut-update.json"
            current_market = _market_from_stored(fixtures[0]["data"])
            updated_data = _stored_market(
                current_market.model_copy(
                    update={"description": "Updated fixture contract."}
                )
            )
            _write_json(update_path, updated_data)
            _run(
                [
                    "vector-search",
                    "collections",
                    "data-objects",
                    "update",
                    "market-fed-cut",
                    *_base(project, location, collection),
                    f"--data={update_path}",
                    f"--etag={old_etag}",
                    "--format=json",
                ]
            )
            updated = _json_output(
                _run(
                    [
                        "vector-search",
                        "collections",
                        "data-objects",
                        "describe",
                        "market-fed-cut",
                        *_base(project, location, collection),
                        "--format=json",
                    ]
                )
            )
            new_etag = updated.get("etag") if isinstance(updated, dict) else None
            report["etag_changed"] = (
                isinstance(new_etag, str)
                and bool(new_etag)
                and new_etag != old_etag
            )

            stale_delete = _run(
                [
                    "vector-search",
                    "collections",
                    "data-objects",
                    "delete",
                    "market-fed-cut",
                    *_base(project, location, collection),
                    f"--etag={old_etag}",
                    "--quiet",
                ],
                check=False,
            )
            report["stale_etag_rejected"] = _is_stale_etag_rejection(
                stale_delete
            )
            report["stale_etag_error_code"] = _error_code(stale_delete)

            report["passed"] = all(
                (
                    report["objects_created"] == 3,
                    bool(report["knn_market_ids"]),
                    report["knn_market_ids"][0] == "polydata:fed-cut",
                    bool(report["text_market_ids"]),
                    report["text_market_ids"][0] == "polydata:fed-cut",
                    "polydata:fed-cut-closed" not in report["knn_market_ids"],
                    "polydata:fed-cut-closed" not in report["text_market_ids"],
                    report["adapter_market_ids"] == ["polydata:fed-cut"],
                    isinstance(report["adapter_query_digest"], str),
                    len(report["adapter_query_digest"]) == 64,
                    report["etag_changed"],
                    report["stale_etag_rejected"],
                    report["stale_etag_error_code"] == "ABORTED",
                )
            )
            if not report["passed"]:
                raise SpikeError("Agent Retrieval acceptance signals did not all pass")
        except Exception as error:
            primary_error = error
        finally:
            for object_id in reversed(created_object_ids):
                cleanup_object = None
                for attempt in range(3):
                    cleanup_object = _run(
                        [
                            "vector-search",
                            "collections",
                            "data-objects",
                            "delete",
                            object_id,
                            *_base(project, location, collection),
                            "--quiet",
                        ],
                        check=False,
                    )
                    code = _error_code(cleanup_object)
                    if cleanup_object.returncode == 0 or code == "NOT_FOUND":
                        break
                    if code not in {
                        "TIMEOUT",
                        "UNAVAILABLE",
                        "DEADLINE_EXCEEDED",
                    }:
                        break
                    time.sleep(attempt + 1)
                if cleanup_object is not None and cleanup_object.returncode != 0:
                    code = _error_code(cleanup_object)
                    if code != "NOT_FOUND":
                        report["cleanup_error_codes"].append(
                            f"object:{object_id}:{code}"
                        )

            orphan_object_ids: list[str] = []
            for object_id in created_object_ids:
                verify_object = _run(
                    [
                        "vector-search",
                        "collections",
                        "data-objects",
                        "describe",
                        object_id,
                        *_base(project, location, collection),
                        "--format=json",
                    ],
                    check=False,
                )
                if _error_code(verify_object) == "NOT_FOUND":
                    report["objects_deleted"] += 1
                else:
                    orphan_object_ids.append(object_id)
            report["orphan_object_ids"] = orphan_object_ids

            cleanup = None
            for attempt in range(5):
                cleanup = _run(
                    [
                        "vector-search",
                        "collections",
                        "delete",
                        collection,
                        f"--project={project}",
                        f"--location={location}",
                        f"--request-id={uuid.uuid4()}",
                        "--quiet",
                        "--format=json",
                    ],
                    check=False,
                )
                cleanup_code = _error_code(cleanup)
                if cleanup.returncode == 0 or cleanup_code == "NOT_FOUND":
                    break
                if cleanup_code not in {
                    "FAILED_PRECONDITION",
                    "TIMEOUT",
                    "UNAVAILABLE",
                    "DEADLINE_EXCEEDED",
                }:
                    break
                time.sleep(attempt + 1)
            assert cleanup is not None
            if cleanup.returncode != 0 and _error_code(cleanup) != "NOT_FOUND":
                report["cleanup_error_codes"].append(
                    f"collection:{_error_code(cleanup)}"
                )
            verify_collection = _run(
                [
                    "vector-search",
                    "collections",
                    "describe",
                    collection,
                    f"--project={project}",
                    f"--location={location}",
                    "--format=json",
                ],
                check=False,
            )
            report["collection_absent_verified"] = (
                _error_code(verify_collection) == "NOT_FOUND"
            )
            report["collection_deleted"] = report["collection_absent_verified"]
            report["orphan_collection"] = (
                None if report["collection_absent_verified"] else collection
            )
            report["passed"] = bool(
                report["passed"]
                and report["objects_deleted"] == report["objects_created"]
                and report["collection_deleted"]
                and not report["orphan_object_ids"]
                and report["orphan_collection"] is None
            )
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            report["run_completed_at_utc"] = datetime.now(
                timezone.utc
            ).isoformat()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(report_path, report)

    if report["orphan_object_ids"] or report["orphan_collection"] is not None:
        detail = (
            "disposable Agent Retrieval cleanup failed; "
            f"objects={report['orphan_object_ids']!r}; "
            f"collection={report['orphan_collection']!r}; inspect immediately"
        )
        raise SpikeError(detail) from primary_error
    if primary_error is not None:
        raise primary_error
    return report


def dry_run_plan(*, project: str, location: str) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "mode": "dry_run",
        "project": project,
        "location": location,
        "mutations": [
            "create one disposable Agent Retrieval Collection",
            "create three synthetic structured Data Objects",
            "attest the exclusive known-fixture manifest and run it through the FitCheck adapter",
            "update one object under its current ETag",
            "attempt and require rejection of one stale-ETag delete",
            "delete and verify absence of every object and the Collection",
        ],
        "excluded": [
            "IAM changes",
            "RAG Engine",
            "auto-embeddings or Gemini calls",
            "GCS import",
            "ANN index creation",
            "governed FitCheck data",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
    )
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("fitcheck-agent-retrieval-spike-report.json"),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.project.strip():
        raise SystemExit("--project or GOOGLE_CLOUD_PROJECT is required")
    if not args.execute:
        print(json.dumps(dry_run_plan(project=args.project, location=args.location), indent=2))
        return 0
    try:
        report = execute_spike(
            project=args.project,
            location=args.location,
            report_path=args.report,
        )
    except SpikeError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
