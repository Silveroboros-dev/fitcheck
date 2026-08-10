"""Offline contract tests for the Google Agent Retrieval index adapter."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pytest

from el.domain.structures import (
    ClaimHorizon,
    Entity,
    ExtractedStructure,
    Mechanism,
    Metric,
)
from el.retrieval.agent_retrieval_index import (
    AGENT_RETRIEVAL_BACKEND,
    AGENT_RETRIEVAL_INDEX_POLICY_VERSION,
    AGENT_RETRIEVAL_OBJECT_SCHEMA_VERSION,
    AgentRetrievalCandidateIndex,
    AgentRetrievalSearchRequest,
    AgentRetrievalSearchResponse,
)
from el.retrieval.candidate_contracts import (
    CandidateIndexIntegrityError,
    CandidateIndexPermanentError,
    CandidateIndexTransientError,
)
from el.retrieval.snapshot_contracts import NormalizedUniverseMarket
from el.retrieval.snapshot_validation import canonical_row_json

CUTOFF = datetime(2026, 5, 22, tzinfo=timezone.utc)
COLLECTION_CREATE_TIME = datetime(2026, 5, 22, 1, tzinfo=timezone.utc)
COLLECTION_UPDATE_TIME = datetime(2026, 5, 22, 2, tzinfo=timezone.utc)
SNAPSHOT_ID = f"mu_{'1' * 64}"
CONTENT_SHA256 = "2" * 64
ARTIFACT_SHA256 = "3" * 64
INDEX_SHA256 = "4" * 64
OBJECT_MANIFEST_SHA256 = "5" * 64
COLLECTION = (
    "projects/fitcheck-test/locations/us-central1/collections/"
    f"snapshot-{SNAPSHOT_ID}"
)


def _structure() -> ExtractedStructure:
    return ExtractedStructure(
        claim_summary="Gemini ranks first on LMSYS by the end of 2026.",
        entities=[
            Entity(name="Google Gemini", role="subject"),
            Entity(name="LMSYS Chatbot Arena", role="venue"),
        ],
        event_stage="measured",
        metric=Metric(
            what="LMSYS rank",
            measured_by="LMSYS leaderboard",
            objective=True,
        ),
        horizon=ClaimHorizon(window_end=date(2026, 12, 31), precision="day"),
        mechanism=Mechanism(),
        stance="yes",
        resolution_source_class="leaderboard",
        contractible_version="Will Gemini rank first on LMSYS?",
    )


def _market(market_id: str = "target", *, is_open: bool = True) -> dict:
    return {
        "market_id": market_id,
        "title": "Will Google Gemini rank first on LMSYS?",
        "slug": market_id,
        "description": "A model leaderboard market.",
        "resolution_rules": "Resolves from the LMSYS leaderboard.",
        "outcomes": ["Yes", "No"],
        "token_ids": [f"{market_id}-yes", f"{market_id}-no"],
        "close_date": "2026-12-31",
        "closed_time": None if is_open else CUTOFF.isoformat(),
        "snapshot_ts": CUTOFF.isoformat(),
        "is_open": is_open,
        "volume_usd": 1000.0,
        "taxonomy_l1": "technology",
        "taxonomy_confidence": 0.9,
        "tags": ["ai", "leaderboard"],
        "source_url": f"https://example.test/{market_id}",
    }


def _row_sha256(market: dict) -> str:
    normalized = NormalizedUniverseMarket.model_validate(market)
    return hashlib.sha256(
        canonical_row_json(normalized).encode("utf-8")
    ).hexdigest()


def _hit(market: dict | None = None, **overrides) -> dict:
    market = market or _market()
    values = {
        "market_id": market.get("market_id", "target"),
        "snapshot_id": SNAPSHOT_ID,
        "content_sha256": CONTENT_SHA256,
        "row_sha256": _row_sha256(market),
        "is_open": market.get("is_open", True),
        "market": market,
        "score": 0.83,
    }
    values.update(overrides)
    return values


@dataclass
class FakeTransport:
    response: AgentRetrievalSearchResponse | dict
    requests: list[AgentRetrievalSearchRequest] = field(default_factory=list)

    def search(self, request: AgentRetrievalSearchRequest):
        self.requests.append(request)
        return self.response


@dataclass
class RaisingTransport:
    error: Exception

    def search(self, _request: AgentRetrievalSearchRequest):
        raise self.error


class InvalidConstructingTransport:
    def search(self, _request: AgentRetrievalSearchRequest):
        return AgentRetrievalSearchResponse(hits=())


def _response(hits=(), request_id=None, **overrides):
    values = {
        "collection_resource": COLLECTION,
        "collection_create_time": COLLECTION_CREATE_TIME,
        "collection_update_time": COLLECTION_UPDATE_TIME,
        "snapshot_id": SNAPSHOT_ID,
        "content_sha256": CONTENT_SHA256,
        "object_schema_version": AGENT_RETRIEVAL_OBJECT_SCHEMA_VERSION,
        "object_manifest_sha256": OBJECT_MANIFEST_SHA256,
        "open_object_count": 3,
        "hits": hits,
        "request_id": request_id,
    }
    values.update(overrides)
    return AgentRetrievalSearchResponse(**values)


def _index(transport, **overrides) -> AgentRetrievalCandidateIndex:
    values = {
        "transport": transport,
        "collection_resource": COLLECTION,
        "snapshot_id": SNAPSHOT_ID,
        "content_sha256": CONTENT_SHA256,
        "artifact_sha256": ARTIFACT_SHA256,
        "index_sha256": INDEX_SHA256,
        "object_manifest_sha256": OBJECT_MANIFEST_SHA256,
        "expected_open_object_count": 3,
        "collection_create_time": COLLECTION_CREATE_TIME,
        "collection_update_time": COLLECTION_UPDATE_TIME,
    }
    values.update(overrides)
    return AgentRetrievalCandidateIndex(
        **values,
    )


def test_query_sends_strict_structured_filters_and_hydrates_bounded_markets():
    first = _market("target")
    second = _market("runner-up")
    transport = FakeTransport(
        _response(
            hits=(
                _hit(first, score=0.91),
                _hit(second, score=0.72),
            ),
            request_id="provider-request-1",
        )
    )
    index = _index(transport)

    result = index.query(_structure(), limit=5)

    assert index.backend == AGENT_RETRIEVAL_BACKEND
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.collection_resource == COLLECTION
    assert request.expected_collection_create_time == COLLECTION_CREATE_TIME
    assert request.expected_collection_update_time == COLLECTION_UPDATE_TIME
    assert request.search_mode == "text"
    assert request.embedding_config == "disabled"
    assert request.fusion_policy_version == "disabled"
    assert request.object_schema_version == AGENT_RETRIEVAL_OBJECT_SCHEMA_VERSION
    assert request.expected_object_manifest_sha256 == OBJECT_MANIFEST_SHA256
    assert request.expected_open_object_count == 3
    assert request.filters.model_dump() == {
        "snapshot_id": SNAPSHOT_ID,
        "content_sha256": CONTENT_SHA256,
        "is_open": True,
    }
    assert request.limit == 5
    assert request.query_text == _structure().claim_summary
    assert "\n" not in request.query_text
    assert result.backend == AGENT_RETRIEVAL_BACKEND
    assert result.snapshot_id == SNAPSHOT_ID
    assert result.content_sha256 == CONTENT_SHA256
    assert result.artifact_sha256 == ARTIFACT_SHA256
    assert result.index_sha256 == INDEX_SHA256
    assert result.index_policy_version == AGENT_RETRIEVAL_INDEX_POLICY_VERSION
    assert result.backend_request_id == "provider-request-1"
    assert [hit.market.market_id for hit in result.hits] == [
        "target",
        "runner-up",
    ]
    assert [hit.source_score for hit in result.hits] == [0.91, 0.72]
    assert len(result.query_digest) == 64


def test_query_digest_is_stable_for_the_same_pinned_response():
    response = _response(hits=(_hit(),), request_id="first")
    first = _index(FakeTransport(response)).query(_structure(), limit=10)
    second_response = response.model_copy(update={"request_id": "second"})
    second = _index(FakeTransport(second_response)).query(_structure(), limit=10)

    assert first.query_digest == second.query_digest
    assert first.backend_request_id != second.backend_request_id


def test_query_digest_changes_with_explicit_search_configuration():
    response = _response(hits=(_hit(),))
    text = _index(FakeTransport(response)).query(_structure(), limit=10)
    semantic = _index(
        FakeTransport(response),
        search_mode="semantic",
        embedding_config="text-embedding-model@pinned",
    ).query(_structure(), limit=10)

    assert text.query_digest != semantic.query_digest


@pytest.mark.parametrize(
    "response",
    [
        _response(collection_resource=COLLECTION + "-drift"),
        _response(
            collection_create_time=COLLECTION_CREATE_TIME.replace(day=23)
        ),
        _response(
            collection_update_time=COLLECTION_UPDATE_TIME.replace(day=23)
        ),
        _response(snapshot_id="different-snapshot"),
        _response(content_sha256="6" * 64),
        _response(object_manifest_sha256="7" * 64),
        _response(open_object_count=2),
    ],
)
def test_collection_attestation_drift_fails_before_result_use(response):
    with pytest.raises(CandidateIndexIntegrityError, match="attestation"):
        _index(FakeTransport(response)).query(_structure(), limit=10)


@pytest.mark.parametrize(
    "response",
    [
        {"unexpected": []},
        {"hits": [{"market_id": "missing-everything"}]},
        {
            "hits": [
                {
                    **_hit(),
                    "market": {**_market(), "outcomes": ["Yes"]},
                }
            ]
        },
        {"hits": [_hit(score=float("nan"))]},
    ],
)
def test_malformed_response_or_market_fails_integrity(response):
    with pytest.raises(CandidateIndexIntegrityError):
        _index(FakeTransport(response)).query(_structure(), limit=10)


@pytest.mark.parametrize(
    "hit",
    [
        _hit(snapshot_id="different-snapshot"),
        _hit(content_sha256="5" * 64),
        _hit(is_open=False),
        _hit(_market(is_open=False)),
        _hit(market_id="different-market"),
        _hit(row_sha256="6" * 64),
    ],
)
def test_filter_or_object_identity_drift_fails_integrity(hit):
    response = _response(hits=(hit,))

    with pytest.raises(CandidateIndexIntegrityError):
        _index(FakeTransport(response)).query(_structure(), limit=10)


def test_duplicate_markets_fail_integrity():
    response = _response(hits=(_hit(), _hit()))

    with pytest.raises(CandidateIndexIntegrityError, match="duplicate"):
        _index(FakeTransport(response)).query(_structure(), limit=10)


def test_backend_cannot_return_more_than_the_requested_limit():
    response = _response(
        hits=(_hit(_market("first")), _hit(_market("second")))
    )

    with pytest.raises(CandidateIndexIntegrityError, match="more hits"):
        _index(FakeTransport(response)).query(_structure(), limit=1)


@pytest.mark.parametrize("limit", [0, 201])
def test_invalid_limit_fails_before_transport(limit):
    transport = FakeTransport(_response(hits=()))

    with pytest.raises(ValueError, match="limit"):
        _index(transport).query(_structure(), limit=limit)

    assert transport.requests == []


def test_typed_transport_errors_preserve_retry_semantics():
    transient = CandidateIndexTransientError("temporary")
    permanent = CandidateIndexPermanentError("configuration")

    with pytest.raises(CandidateIndexTransientError) as transient_error:
        _index(RaisingTransport(transient)).query(_structure(), limit=10)
    with pytest.raises(CandidateIndexPermanentError) as permanent_error:
        _index(RaisingTransport(permanent)).query(_structure(), limit=10)

    assert transient_error.value is transient
    assert permanent_error.value is permanent


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionError(), OSError()])
def test_transport_connectivity_failures_are_typed_transient(error):
    with pytest.raises(CandidateIndexTransientError):
        _index(RaisingTransport(error)).query(_structure(), limit=10)


def test_transport_side_validation_error_is_typed_integrity():
    with pytest.raises(CandidateIndexIntegrityError, match="transport"):
        _index(InvalidConstructingTransport()).query(_structure(), limit=10)


def test_invalid_or_drifted_index_descriptor_is_rejected_at_wiring_time():
    transport = FakeTransport(_response(hits=()))
    with pytest.raises(CandidateIndexPermanentError, match="SHA-256"):
        AgentRetrievalCandidateIndex(
            transport=transport,
            collection_resource=COLLECTION,
            snapshot_id=SNAPSHOT_ID,
            content_sha256="not-a-digest",
            artifact_sha256=ARTIFACT_SHA256,
            index_sha256=INDEX_SHA256,
            object_manifest_sha256=OBJECT_MANIFEST_SHA256,
            expected_open_object_count=3,
            collection_create_time=COLLECTION_CREATE_TIME,
            collection_update_time=COLLECTION_UPDATE_TIME,
        )
    with pytest.raises(CandidateIndexPermanentError, match="unsupported"):
        AgentRetrievalCandidateIndex(
            transport=transport,
            collection_resource=COLLECTION,
            snapshot_id=SNAPSHOT_ID,
            content_sha256=CONTENT_SHA256,
            artifact_sha256=ARTIFACT_SHA256,
            index_sha256=INDEX_SHA256,
            object_manifest_sha256=OBJECT_MANIFEST_SHA256,
            expected_open_object_count=3,
            collection_create_time=COLLECTION_CREATE_TIME,
            collection_update_time=COLLECTION_UPDATE_TIME,
            index_policy_version="future-policy",
        )


@pytest.mark.parametrize(
    ("search_mode", "embedding_config", "fusion_policy_version"),
    [
        ("text", "unexpected-model", "disabled"),
        ("semantic", "disabled", "disabled"),
        ("hybrid", "text-embedding-model@pinned", "disabled"),
    ],
)
def test_unratified_search_configuration_fails_at_wiring_time(
    search_mode, embedding_config, fusion_policy_version
):
    with pytest.raises(CandidateIndexPermanentError):
        _index(
            FakeTransport(_response(hits=())),
            search_mode=search_mode,
            embedding_config=embedding_config,
            fusion_policy_version=fusion_policy_version,
        )
