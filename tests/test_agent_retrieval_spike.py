"""Offline contract tests for the disposable Agent Retrieval spike."""

import hashlib
import json
import subprocess
from datetime import datetime, timezone

import pytest

from scripts.spike_agent_retrieval import (
    COLLECTION_PREFIX,
    CONTENT_SHA256,
    SNAPSHOT_ID,
    GcloudAgentRetrievalTransport,
    SpikeError,
    _claim,
    collection_id,
    data_schema,
    dry_run_plan,
    fixture_objects,
    _is_stale_etag_rejection,
    _run,
    market_ids,
    vector_schema,
)
from el.retrieval.agent_retrieval_index import (
    AgentRetrievalCandidateIndex,
    agent_retrieval_object_manifest_sha256,
    serialize_agent_retrieval_market,
)
from el.retrieval.candidate_contracts import CandidateIndexIntegrityError
from el.retrieval.snapshot_contracts import NormalizedUniverseMarket
from el.retrieval.snapshot_validation import canonical_row_json


COLLECTION_CREATE_TIME = datetime(2026, 8, 7, 10, tzinfo=timezone.utc)
COLLECTION_UPDATE_TIME = datetime(2026, 8, 7, 11, tzinfo=timezone.utc)


def test_fixture_objects_match_strict_structured_schema():
    schema = data_schema()
    expected = set(schema["properties"])
    fixtures = fixture_objects()

    # Agent Retrieval enforces undeclared fields as forbidden and its gcloud
    # schema loader currently crashes if JSON Schema's boolean
    # ``additionalProperties`` keyword is supplied explicitly.
    assert "additionalProperties" not in schema
    assert set(schema["required"]) == expected
    assert len(fixtures) == 3
    assert len({fixture["id"] for fixture in fixtures}) == 3
    for fixture in fixtures:
        assert set(fixture["data"]) == expected
        market = NormalizedUniverseMarket.model_validate_json(
            fixture["data"]["market_json"]
        )
        assert fixture["data"]["market_id"] == market.market_id
        assert fixture["data"]["is_open"] is market.is_open
        assert fixture["data"]["row_sha256"] == hashlib.sha256(
            canonical_row_json(market).encode("utf-8")
        ).hexdigest()
        assert len(
            fixture["vectors"]["market_embedding"]["dense"]["values"]
        ) == vector_schema()["market_embedding"]["denseVector"]["dimensions"]


def test_generated_collection_id_is_bounded_and_disposable():
    value = collection_id()

    assert value.startswith(COLLECTION_PREFIX)
    assert len(value) <= 63
    assert value[-1].isalnum()


def test_searchable_field_drift_is_covered_by_object_manifest():
    data = dict(fixture_objects()[0]["data"])
    data["title"] = "Tampered searchable title"

    with pytest.raises(CandidateIndexIntegrityError, match="searchable fields"):
        agent_retrieval_object_manifest_sha256([data])


def test_market_ids_accepts_gcloud_and_rest_response_shapes():
    row = {"dataObject": {"data": {"market_id": "polydata:fed-cut"}}}

    assert market_ids([row]) == ["polydata:fed-cut"]
    assert market_ids({"results": [row]}) == ["polydata:fed-cut"]


def test_dry_run_makes_cloud_boundary_explicit():
    plan = dry_run_plan(project="project-test", location="us-central1")

    assert plan["mode"] == "dry_run"
    assert "IAM changes" in plan["excluded"]
    assert "RAG Engine" in plan["excluded"]
    assert "ANN index creation" in plan["excluded"]


def test_stale_etag_signal_requires_service_aborted_code():
    aborted = subprocess.CompletedProcess([], 1, "", "ERROR: ABORTED: stale")
    flag_error = subprocess.CompletedProcess([], 1, "", "invalid --etag value")

    assert _is_stale_etag_rejection(aborted)
    assert not _is_stale_etag_rejection(flag_error)


def test_nonchecking_timeout_returns_cleanup_signal(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("gcloud", 1)

    monkeypatch.setattr(subprocess, "run", timeout)

    completed = _run(["test"], check=False, timeout_seconds=1)

    assert completed.returncode == 124
    assert completed.stderr == "TIMEOUT"


def test_checking_timeout_fails_safely(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("gcloud", 1)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        _run(["test"], timeout_seconds=1)


def test_gcloud_transport_attests_population_and_uses_exact_filters(monkeypatch):
    fixtures = fixture_objects()
    commands = []

    def fake_run(args, *, check=True, timeout_seconds=300):
        del check, timeout_seconds
        commands.append(args)
        if args[:3] == ["vector-search", "collections", "describe"]:
            payload = {
                "name": (
                    "projects/project-test/locations/us-central1/"
                    "collections/fixture-collection"
                ),
                "createTime": COLLECTION_CREATE_TIME.isoformat(),
                "updateTime": COLLECTION_UPDATE_TIME.isoformat(),
            }
        elif "describe" in args:
            object_id = args[args.index("describe") + 1]
            fixture = next(item for item in fixtures if item["id"] == object_id)
            payload = {"data": fixture["data"]}
        elif "search" in args:
            payload = [
                {"dataObject": {"data": fixtures[0]["data"]}}
            ]
        else:  # pragma: no cover - an unexpected command is a test failure
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr("scripts.spike_agent_retrieval._run", fake_run)
    open_data = [item["data"] for item in fixtures if item["data"]["is_open"]]
    manifest = agent_retrieval_object_manifest_sha256(open_data)
    transport = GcloudAgentRetrievalTransport(
        project="project-test",
        location="us-central1",
        collection="fixture-collection",
        population_object_ids=tuple(item["id"] for item in fixtures),
    )
    result = AgentRetrievalCandidateIndex(
        transport=transport,
        collection_resource=transport.collection_resource,
        snapshot_id=SNAPSHOT_ID,
        content_sha256=CONTENT_SHA256,
        artifact_sha256="b" * 64,
        index_sha256="c" * 64,
        object_manifest_sha256=manifest,
        expected_open_object_count=2,
        collection_create_time=COLLECTION_CREATE_TIME,
        collection_update_time=COLLECTION_UPDATE_TIME,
    ).query(_claim(), limit=3)

    assert [hit.market.market_id for hit in result.hits] == [
        "polydata:fed-cut"
    ]
    search = next(command for command in commands if "search" in command)
    filter_arg = next(arg for arg in search if arg.startswith("--json-filter="))
    assert json.loads(filter_arg.split("=", 1)[1]) == {
        "$and": [
            {"snapshot_id": {"$eq": SNAPSHOT_ID}},
            {"content_sha256": {"$eq": CONTENT_SHA256}},
            {"is_open": {"$eq": True}},
        ]
    }


def test_gcloud_transport_rejects_result_outside_attested_fixture_set(
    monkeypatch,
):
    fixtures = fixture_objects()
    target = NormalizedUniverseMarket.model_validate_json(
        fixtures[0]["data"]["market_json"]
    )
    unexpected = target.model_copy(
        update={
            "market_id": "polydata:unexpected",
            "slug": "unexpected",
            "token_ids": ["unexpected-yes", "unexpected-no"],
            "source_url": "https://example.test/unexpected",
        }
    )
    unexpected_data = serialize_agent_retrieval_market(
        unexpected,
        snapshot_id=SNAPSHOT_ID,
        content_sha256=CONTENT_SHA256,
    )

    def fake_run(args, *, check=True, timeout_seconds=300):
        del check, timeout_seconds
        if args[:3] == ["vector-search", "collections", "describe"]:
            payload = {
                "name": (
                    "projects/project-test/locations/us-central1/"
                    "collections/fixture-collection"
                ),
                "createTime": COLLECTION_CREATE_TIME.isoformat(),
                "updateTime": COLLECTION_UPDATE_TIME.isoformat(),
            }
        elif "describe" in args:
            object_id = args[args.index("describe") + 1]
            fixture = next(item for item in fixtures if item["id"] == object_id)
            payload = {"data": fixture["data"]}
        elif "search" in args:
            payload = [{"dataObject": {"data": unexpected_data}}]
        else:  # pragma: no cover - an unexpected command is a test failure
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr("scripts.spike_agent_retrieval._run", fake_run)
    open_data = [item["data"] for item in fixtures if item["data"]["is_open"]]
    transport = GcloudAgentRetrievalTransport(
        project="project-test",
        location="us-central1",
        collection="fixture-collection",
        population_object_ids=tuple(item["id"] for item in fixtures),
    )
    index = AgentRetrievalCandidateIndex(
        transport=transport,
        collection_resource=transport.collection_resource,
        snapshot_id=SNAPSHOT_ID,
        content_sha256=CONTENT_SHA256,
        artifact_sha256="b" * 64,
        index_sha256="c" * 64,
        object_manifest_sha256=agent_retrieval_object_manifest_sha256(
            open_data
        ),
        expected_open_object_count=2,
        collection_create_time=COLLECTION_CREATE_TIME,
        collection_update_time=COLLECTION_UPDATE_TIME,
    )

    with pytest.raises(CandidateIndexIntegrityError, match="outside"):
        index.query(_claim(), limit=3)


def test_gcloud_transport_rejects_same_id_mutated_after_attestation(
    monkeypatch,
):
    fixtures = fixture_objects()
    target = NormalizedUniverseMarket.model_validate_json(
        fixtures[0]["data"]["market_json"]
    )
    mutated_data = serialize_agent_retrieval_market(
        target.model_copy(update={"description": "Mutated after attestation."}),
        snapshot_id=SNAPSHOT_ID,
        content_sha256=CONTENT_SHA256,
    )

    def fake_run(args, *, check=True, timeout_seconds=300):
        del check, timeout_seconds
        if args[:3] == ["vector-search", "collections", "describe"]:
            payload = {
                "name": (
                    "projects/project-test/locations/us-central1/"
                    "collections/fixture-collection"
                ),
                "createTime": COLLECTION_CREATE_TIME.isoformat(),
                "updateTime": COLLECTION_UPDATE_TIME.isoformat(),
            }
        elif "describe" in args:
            object_id = args[args.index("describe") + 1]
            fixture = next(item for item in fixtures if item["id"] == object_id)
            payload = {"data": fixture["data"]}
        elif "search" in args:
            payload = [{"dataObject": {"data": mutated_data}}]
        else:  # pragma: no cover - an unexpected command is a test failure
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr("scripts.spike_agent_retrieval._run", fake_run)
    open_data = [item["data"] for item in fixtures if item["data"]["is_open"]]
    transport = GcloudAgentRetrievalTransport(
        project="project-test",
        location="us-central1",
        collection="fixture-collection",
        population_object_ids=tuple(item["id"] for item in fixtures),
    )
    index = AgentRetrievalCandidateIndex(
        transport=transport,
        collection_resource=transport.collection_resource,
        snapshot_id=SNAPSHOT_ID,
        content_sha256=CONTENT_SHA256,
        artifact_sha256="b" * 64,
        index_sha256="c" * 64,
        object_manifest_sha256=agent_retrieval_object_manifest_sha256(
            open_data
        ),
        expected_open_object_count=2,
        collection_create_time=COLLECTION_CREATE_TIME,
        collection_update_time=COLLECTION_UPDATE_TIME,
    )

    with pytest.raises(CandidateIndexIntegrityError, match="bytes outside"):
        index.query(_claim(), limit=3)


def test_ambiguous_create_failure_still_cleans_every_intended_object(
    monkeypatch, tmp_path
):
    fixtures = fixture_objects()
    deleted_ids = []
    delete_attempts = {}

    def fake_run(args, *, check=True, timeout_seconds=300):
        del timeout_seconds
        if args[0] == "version":
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"Google Cloud SDK": "test-sdk"}),
                "",
            )
        if args[:3] == ["vector-search", "collections", "create"]:
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if "data-objects" in args and "create" in args:
            object_id = args[args.index("create") + 1]
            if object_id == fixtures[1]["id"] and check:
                raise SpikeError("ambiguous create timeout")
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if "data-objects" in args and "delete" in args:
            object_id = args[args.index("delete") + 1]
            deleted_ids.append(object_id)
            delete_attempts[object_id] = delete_attempts.get(object_id, 0) + 1
            if object_id == fixtures[2]["id"] and delete_attempts[object_id] == 1:
                return subprocess.CompletedProcess(args, 124, "", "TIMEOUT")
            return subprocess.CompletedProcess(args, 0, "", "")
        if "data-objects" in args and "describe" in args:
            return subprocess.CompletedProcess(args, 1, "", "NOT_FOUND")
        if args[:3] == ["vector-search", "collections", "delete"]:
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if args[:3] == ["vector-search", "collections", "describe"]:
            return subprocess.CompletedProcess(args, 1, "", "NOT_FOUND")
        raise AssertionError(args)

    monkeypatch.setattr("scripts.spike_agent_retrieval._run", fake_run)
    monkeypatch.setattr("scripts.spike_agent_retrieval.time.sleep", lambda _: None)
    report_path = tmp_path / "report.json"

    with pytest.raises(SpikeError, match="ambiguous"):
        from scripts.spike_agent_retrieval import execute_spike

        execute_spike(
            project="project-test",
            location="us-central1",
            report_path=report_path,
        )

    assert set(deleted_ids) == {item["id"] for item in fixtures}
    assert delete_attempts[fixtures[2]["id"]] == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["orphan_object_ids"] == []
    assert report["orphan_collection"] is None


def test_cleanup_failure_overrides_primary_error_with_orphan_identity(
    monkeypatch, tmp_path
):
    fixtures = fixture_objects()
    orphan_id = fixtures[2]["id"]

    def fake_run(args, *, check=True, timeout_seconds=300):
        del timeout_seconds
        if args[0] == "version":
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"Google Cloud SDK": "test-sdk"}),
                "",
            )
        if args[:3] == ["vector-search", "collections", "create"]:
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if "data-objects" in args and "create" in args:
            object_id = args[args.index("create") + 1]
            if object_id == fixtures[1]["id"] and check:
                raise SpikeError("primary create failure")
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if "data-objects" in args and "delete" in args:
            object_id = args[args.index("delete") + 1]
            if object_id == orphan_id:
                return subprocess.CompletedProcess(args, 124, "", "TIMEOUT")
            return subprocess.CompletedProcess(args, 0, "", "")
        if "data-objects" in args and "describe" in args:
            object_id = args[args.index("describe") + 1]
            if object_id == orphan_id:
                return subprocess.CompletedProcess(args, 0, "{}", "")
            return subprocess.CompletedProcess(args, 1, "", "NOT_FOUND")
        if args[:3] == ["vector-search", "collections", "delete"]:
            return subprocess.CompletedProcess(args, 124, "", "TIMEOUT")
        if args[:3] == ["vector-search", "collections", "describe"]:
            return subprocess.CompletedProcess(args, 0, "{}", "")
        raise AssertionError(args)

    monkeypatch.setattr("scripts.spike_agent_retrieval._run", fake_run)
    monkeypatch.setattr("scripts.spike_agent_retrieval.time.sleep", lambda _: None)
    report_path = tmp_path / "report.json"

    with pytest.raises(SpikeError, match="cleanup failed") as error:
        from scripts.spike_agent_retrieval import execute_spike

        execute_spike(
            project="project-test",
            location="us-central1",
            report_path=report_path,
        )

    assert isinstance(error.value.__cause__, SpikeError)
    assert "primary create failure" in str(error.value.__cause__)
    assert orphan_id in str(error.value)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["orphan_object_ids"] == [orphan_id]
    assert report["orphan_collection"] is not None
