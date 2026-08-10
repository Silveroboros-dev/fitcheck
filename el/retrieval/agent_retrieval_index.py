"""Offline-testable Google Agent Retrieval candidate-index adapter.

This module contains no Google SDK or credential handling.  A production
transport is responsible for translating the typed request into the ratified
Agent Retrieval API and for mapping HTTP/service failures into the typed index
errors below.  The adapter remains responsible for FitCheck's immutable
snapshot checks and bounded hydration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from el.domain.structures import ExtractedStructure
from el.retrieval.candidate_contracts import (
    MAX_CANDIDATE_QUERY_LIMIT,
    CandidateIndexError,
    CandidateIndexIntegrityError,
    CandidateIndexPermanentError,
    CandidateIndexTransientError,
    CandidateQueryHit,
    CandidateQueryResult,
)
from el.retrieval.snapshot_contracts import NormalizedUniverseMarket
from el.retrieval.snapshot_validation import canonical_row_json

AGENT_RETRIEVAL_BACKEND = "google_agent_retrieval"
AGENT_RETRIEVAL_INDEX_POLICY_VERSION = "agent-retrieval-shadow-v0"
AGENT_RETRIEVAL_OBJECT_SCHEMA_VERSION = "fitcheck-market-object-v1"
DEFAULT_AGENT_RETRIEVAL_TIMEOUT_SECONDS = 15.0
AgentRetrievalSearchMode = Literal["text", "semantic", "hybrid"]


def agent_retrieval_data_schema() -> dict[str, Any]:
    """Return the canonical managed-object schema for FitCheck markets.

    Searchable text and strict filter fields stay first-class.  The complete
    provider-neutral market is retained as canonical JSON so optional/null
    domain fields do not acquire a second service-specific representation.
    """

    properties = {
        "object_schema_version": {"type": "string"},
        "market_id": {"type": "string"},
        "snapshot_id": {"type": "string"},
        "content_sha256": {"type": "string"},
        "row_sha256": {"type": "string"},
        "is_open": {"type": "boolean"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "resolution_rules": {"type": "string"},
        "market_json": {"type": "string"},
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
    }


def agent_retrieval_object_schema_descriptor() -> dict[str, Any]:
    """Return the exact object-schema descriptor allowed by shadow policy."""

    return {
        "version": AGENT_RETRIEVAL_OBJECT_SCHEMA_VERSION,
        "data_schema": agent_retrieval_data_schema(),
    }


def serialize_agent_retrieval_market(
    market: NormalizedUniverseMarket,
    *,
    snapshot_id: str,
    content_sha256: str,
) -> dict[str, Any]:
    """Serialize one normalized market into the canonical managed envelope."""

    if not snapshot_id.strip():
        raise CandidateIndexPermanentError("snapshot ID must be nonblank")
    if len(content_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in content_sha256
    ):
        raise CandidateIndexPermanentError(
            "content_sha256 must be a lowercase SHA-256 digest"
        )
    market_json = canonical_row_json(market)
    row_sha256 = hashlib.sha256(market_json.encode("utf-8")).hexdigest()
    return {
        "object_schema_version": AGENT_RETRIEVAL_OBJECT_SCHEMA_VERSION,
        "market_id": market.market_id,
        "snapshot_id": snapshot_id,
        "content_sha256": content_sha256,
        "row_sha256": row_sha256,
        "is_open": market.is_open,
        "title": market.title,
        "description": market.description,
        "resolution_rules": market.resolution_rules,
        "market_json": market_json,
    }


def normalize_agent_retrieval_data(
    data: dict[str, Any],
    *,
    score: float | None = None,
) -> dict[str, Any]:
    """Normalize an untrusted managed Data Object for adapter validation."""

    market = _validate_serialized_agent_retrieval_data(data)
    return {
        "market_id": data["market_id"],
        "snapshot_id": data["snapshot_id"],
        "content_sha256": data["content_sha256"],
        "row_sha256": data["row_sha256"],
        "is_open": data["is_open"],
        "market": market.model_dump(mode="json"),
        "score": score,
    }


def agent_retrieval_object_manifest_sha256(
    data_objects: Iterable[dict[str, Any]],
) -> str:
    """Hash the ordered identity/content manifest for one sealed population."""

    rows: list[dict[str, str]] = []
    for data in data_objects:
        object_sha256 = agent_retrieval_object_sha256(data)
        rows.append(
            {
                "market_id": str(data["market_id"]),
                "object_sha256": object_sha256,
            }
        )
    rows.sort(key=lambda row: row["market_id"])
    if len({row["market_id"] for row in rows}) != len(rows):
        raise CandidateIndexPermanentError(
            "Agent Retrieval object manifest contains duplicate markets"
        )
    encoded = json.dumps(
        rows,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def agent_retrieval_object_sha256(data: dict[str, Any]) -> str:
    """Hash every canonical and search-influencing field of one object."""

    _validate_serialized_agent_retrieval_data(data)
    encoded = json.dumps(
        data,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_serialized_agent_retrieval_data(
    data: dict[str, Any],
) -> NormalizedUniverseMarket:
    expected_fields = set(agent_retrieval_data_schema()["properties"])
    if set(data) != expected_fields:
        raise CandidateIndexIntegrityError(
            "Agent Retrieval object fields conflict with the canonical schema"
        )
    market_json = data.get("market_json")
    if not isinstance(market_json, str):
        raise CandidateIndexIntegrityError(
            "Agent Retrieval object omitted canonical market JSON"
        )
    try:
        market = NormalizedUniverseMarket.model_validate_json(market_json)
    except (ValidationError, ValueError) as error:
        raise CandidateIndexIntegrityError(
            "Agent Retrieval object contains invalid market JSON"
        ) from error
    canonical_market_json = canonical_row_json(market)
    row_sha256 = hashlib.sha256(
        canonical_market_json.encode("utf-8")
    ).hexdigest()
    if market_json != canonical_market_json or data.get("row_sha256") != row_sha256:
        raise CandidateIndexIntegrityError(
            "Agent Retrieval object failed canonical row validation"
        )
    if (
        data.get("object_schema_version")
        != AGENT_RETRIEVAL_OBJECT_SCHEMA_VERSION
        or data.get("market_id") != market.market_id
        or data.get("is_open") is not market.is_open
        or data.get("title") != market.title
        or data.get("description") != market.description
        or data.get("resolution_rules") != market.resolution_rules
    ):
        raise CandidateIndexIntegrityError(
            "Agent Retrieval searchable fields differ from canonical market data"
        )
    snapshot_id = data.get("snapshot_id")
    content_sha256 = data.get("content_sha256")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise CandidateIndexIntegrityError(
            "Agent Retrieval object has an invalid snapshot ID"
        )
    if not isinstance(content_sha256, str) or len(content_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in content_sha256
    ):
        raise CandidateIndexIntegrityError(
            "Agent Retrieval object has an invalid content digest"
        )
    return market


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AgentRetrievalFilters(_Contract):
    """Structured filters that a concrete transport must apply conjunctively."""

    snapshot_id: str = Field(min_length=1, max_length=128)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_open: Literal[True] = True


class AgentRetrievalSearchRequest(_Contract):
    collection_resource: str = Field(min_length=1, max_length=2048)
    api_version: Literal["v1"] = "v1"
    expected_collection_create_time: datetime
    expected_collection_update_time: datetime
    query_text: str = Field(min_length=1, max_length=32_768)
    search_mode: AgentRetrievalSearchMode
    embedding_config: str = Field(min_length=1, max_length=256)
    fusion_policy_version: str = Field(min_length=1, max_length=128)
    object_schema_version: Literal["fitcheck-market-object-v1"] = (
        AGENT_RETRIEVAL_OBJECT_SCHEMA_VERSION
    )
    filters: AgentRetrievalFilters
    expected_object_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_open_object_count: int = Field(ge=0)
    limit: int = Field(ge=1, le=MAX_CANDIDATE_QUERY_LIMIT)
    timeout_seconds: float = Field(gt=0, le=60, allow_inf_nan=False)

    @field_validator(
        "expected_collection_create_time",
        "expected_collection_update_time",
    )
    @classmethod
    def _expected_times_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected collection times must be timezone-aware")
        return value.astimezone(timezone.utc)


class AgentRetrievalSearchResponse(_Contract):
    """Transport-normalized envelope; individual hits remain untrusted."""

    collection_resource: str = Field(min_length=1, max_length=2048)
    collection_create_time: datetime
    collection_update_time: datetime
    snapshot_id: str = Field(min_length=1, max_length=128)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_schema_version: Literal["fitcheck-market-object-v1"]
    object_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    open_object_count: int = Field(ge=0)
    hits: tuple[dict[str, Any], ...]
    request_id: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("collection_create_time", "collection_update_time")
    @classmethod
    def _observed_times_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed collection times must be timezone-aware")
        return value.astimezone(timezone.utc)


class AgentRetrievalTransportHit(_Contract):
    """Required normalized shape for one managed-search result."""

    market_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=128)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_open: bool
    market: dict[str, Any]
    score: float | None = Field(default=None, allow_inf_nan=False)


class AgentRetrievalTransport(Protocol):
    """Narrow seam for an authenticated Agent Retrieval API client."""

    def search(
        self, request: AgentRetrievalSearchRequest
    ) -> AgentRetrievalSearchResponse | dict[str, Any]: ...


@dataclass(frozen=True)
class AgentRetrievalCandidateIndex:
    """Read-only handle to one sealed Agent Retrieval collection descriptor."""

    transport: AgentRetrievalTransport
    collection_resource: str
    snapshot_id: str
    content_sha256: str
    artifact_sha256: str
    index_sha256: str
    object_manifest_sha256: str
    expected_open_object_count: int
    collection_create_time: datetime
    collection_update_time: datetime
    index_policy_version: str = AGENT_RETRIEVAL_INDEX_POLICY_VERSION
    search_mode: AgentRetrievalSearchMode = "text"
    embedding_config: str = "disabled"
    fusion_policy_version: str = "disabled"
    timeout_seconds: float = DEFAULT_AGENT_RETRIEVAL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        values = {
            "collection_resource": self.collection_resource,
            "snapshot_id": self.snapshot_id,
            "index_policy_version": self.index_policy_version,
            "embedding_config": self.embedding_config,
            "fusion_policy_version": self.fusion_policy_version,
        }
        if any(not value.strip() for value in values.values()):
            raise CandidateIndexPermanentError(
                "Agent Retrieval index identity must be nonblank"
            )
        for name, digest in (
            ("content_sha256", self.content_sha256),
            ("artifact_sha256", self.artifact_sha256),
            ("index_sha256", self.index_sha256),
            ("object_manifest_sha256", self.object_manifest_sha256),
        ):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise CandidateIndexPermanentError(
                    f"{name} must be a lowercase SHA-256 digest"
                )
        if self.index_policy_version != AGENT_RETRIEVAL_INDEX_POLICY_VERSION:
            raise CandidateIndexPermanentError(
                "unsupported Agent Retrieval index policy"
            )
        if self.expected_open_object_count < 0:
            raise CandidateIndexPermanentError(
                "expected open-object count cannot be negative"
            )
        for name, value in (
            ("collection_create_time", self.collection_create_time),
            ("collection_update_time", self.collection_update_time),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise CandidateIndexPermanentError(
                    f"{name} must be timezone-aware"
                )
        if self.collection_update_time < self.collection_create_time:
            raise CandidateIndexPermanentError(
                "collection update time precedes create time"
            )
        if self.search_mode == "text":
            if (
                self.embedding_config != "disabled"
                or self.fusion_policy_version != "disabled"
            ):
                raise CandidateIndexPermanentError(
                    "text search cannot claim embedding or fusion configuration"
                )
        elif self.search_mode == "semantic":
            if (
                self.embedding_config == "disabled"
                or self.fusion_policy_version != "disabled"
            ):
                raise CandidateIndexPermanentError(
                    "semantic search requires embeddings and no fusion policy"
                )
        elif (
            self.embedding_config == "disabled"
            or self.fusion_policy_version == "disabled"
        ):
            raise CandidateIndexPermanentError(
                "hybrid search requires embedding and fusion configuration"
            )
        if not 0 < self.timeout_seconds <= 60:
            raise CandidateIndexPermanentError(
                "Agent Retrieval timeout must be within 60 seconds"
            )

    @property
    def backend(self) -> str:
        return AGENT_RETRIEVAL_BACKEND

    def query(
        self,
        structure: ExtractedStructure,
        *,
        limit: int,
    ) -> CandidateQueryResult:
        if not 1 <= limit <= MAX_CANDIDATE_QUERY_LIMIT:
            raise ValueError(
                "candidate index limit must be between 1 and "
                f"{MAX_CANDIDATE_QUERY_LIMIT}"
            )
        query_text = _query_text(structure)
        try:
            request = AgentRetrievalSearchRequest(
                collection_resource=self.collection_resource,
                expected_collection_create_time=self.collection_create_time,
                expected_collection_update_time=self.collection_update_time,
                query_text=query_text,
                search_mode=self.search_mode,
                embedding_config=self.embedding_config,
                fusion_policy_version=self.fusion_policy_version,
                filters=AgentRetrievalFilters(
                    snapshot_id=self.snapshot_id,
                    content_sha256=self.content_sha256,
                    is_open=True,
                ),
                expected_object_manifest_sha256=(
                    self.object_manifest_sha256
                ),
                expected_open_object_count=self.expected_open_object_count,
                limit=limit,
                timeout_seconds=self.timeout_seconds,
            )
        except ValidationError as error:
            raise CandidateIndexPermanentError(
                "Agent Retrieval query contract is invalid"
            ) from error
        try:
            raw_response = self.transport.search(request)
        except CandidateIndexError:
            raise
        except ValidationError as error:
            raise CandidateIndexIntegrityError(
                "Agent Retrieval transport produced an invalid response"
            ) from error
        except (ConnectionError, OSError, TimeoutError) as error:
            raise CandidateIndexTransientError(
                "Agent Retrieval query is temporarily unavailable"
            ) from error

        try:
            response = AgentRetrievalSearchResponse.model_validate(raw_response)
        except ValidationError as error:
            raise CandidateIndexIntegrityError(
                "Agent Retrieval response envelope is invalid"
            ) from error
        if (
            response.collection_resource != self.collection_resource
            or response.collection_create_time
            != self.collection_create_time.astimezone(timezone.utc)
            or response.collection_update_time
            != self.collection_update_time.astimezone(timezone.utc)
            or response.snapshot_id != self.snapshot_id
            or response.content_sha256 != self.content_sha256
            or response.object_manifest_sha256
            != self.object_manifest_sha256
            or response.open_object_count != self.expected_open_object_count
        ):
            raise CandidateIndexIntegrityError(
                "Agent Retrieval collection attestation conflicts with its pin"
            )
        if len(response.hits) > limit:
            raise CandidateIndexIntegrityError(
                "Agent Retrieval returned more hits than requested"
            )

        hydrated: list[CandidateQueryHit] = []
        digest_hits: list[dict[str, Any]] = []
        market_ids: set[str] = set()
        for raw_hit in response.hits:
            try:
                hit = AgentRetrievalTransportHit.model_validate(raw_hit)
                market = NormalizedUniverseMarket.model_validate(hit.market)
            except ValidationError as error:
                raise CandidateIndexIntegrityError(
                    "Agent Retrieval returned malformed market evidence"
                ) from error
            if (
                hit.snapshot_id != self.snapshot_id
                or hit.content_sha256 != self.content_sha256
                or not hit.is_open
                or not market.is_open
            ):
                raise CandidateIndexIntegrityError(
                    "Agent Retrieval result conflicts with strict filters"
                )
            if hit.market_id != market.market_id:
                raise CandidateIndexIntegrityError(
                    "Agent Retrieval market identity is inconsistent"
                )
            observed_row_sha256 = _row_sha256(market)
            if hit.row_sha256 != observed_row_sha256:
                raise CandidateIndexIntegrityError(
                    "Agent Retrieval market content failed integrity validation"
                )
            if market.market_id in market_ids:
                raise CandidateIndexIntegrityError(
                    "Agent Retrieval returned a duplicate market"
                )
            market_ids.add(market.market_id)
            hydrated.append(
                CandidateQueryHit(market=market, source_score=hit.score)
            )
            digest_hits.append(
                {
                    "market_id": market.market_id,
                    "row_sha256": observed_row_sha256,
                    "source_score": hit.score,
                }
            )

        query_digest = _query_digest(
            collection_resource=self.collection_resource,
            snapshot_id=self.snapshot_id,
            content_sha256=self.content_sha256,
            artifact_sha256=self.artifact_sha256,
            index_sha256=self.index_sha256,
            object_manifest_sha256=self.object_manifest_sha256,
            expected_open_object_count=self.expected_open_object_count,
            collection_create_time=self.collection_create_time,
            collection_update_time=self.collection_update_time,
            index_policy_version=self.index_policy_version,
            search_mode=self.search_mode,
            embedding_config=self.embedding_config,
            fusion_policy_version=self.fusion_policy_version,
            query_text=query_text,
            filters=request.filters,
            limit=limit,
            hits=digest_hits,
        )
        return CandidateQueryResult(
            backend=self.backend,
            snapshot_id=self.snapshot_id,
            content_sha256=self.content_sha256,
            artifact_sha256=self.artifact_sha256,
            index_sha256=self.index_sha256,
            index_policy_version=self.index_policy_version,
            query_digest=query_digest,
            limit=limit,
            hits=tuple(hydrated),
            backend_request_id=response.request_id,
        )


def _query_text(structure: ExtractedStructure) -> str:
    # The normalized summary is the claim's concise retrieval intent.  Repeating
    # entities, metric text, and the contractible question caused the managed
    # full-text API to return no result for a live exact-match fixture.
    return structure.claim_summary.strip()


def _row_sha256(market: NormalizedUniverseMarket) -> str:
    return hashlib.sha256(canonical_row_json(market).encode("utf-8")).hexdigest()


def _query_digest(
    *,
    collection_resource: str,
    snapshot_id: str,
    content_sha256: str,
    artifact_sha256: str,
    index_sha256: str,
    object_manifest_sha256: str,
    expected_open_object_count: int,
    collection_create_time: datetime,
    collection_update_time: datetime,
    index_policy_version: str,
    search_mode: AgentRetrievalSearchMode,
    embedding_config: str,
    fusion_policy_version: str,
    query_text: str,
    filters: AgentRetrievalFilters,
    limit: int,
    hits: list[dict[str, Any]],
) -> str:
    payload = json.dumps(
        {
            "backend": AGENT_RETRIEVAL_BACKEND,
            "collection_resource": collection_resource,
            "snapshot_id": snapshot_id,
            "content_sha256": content_sha256,
            "artifact_sha256": artifact_sha256,
            "index_sha256": index_sha256,
            "object_manifest_sha256": object_manifest_sha256,
            "expected_open_object_count": expected_open_object_count,
            "collection_create_time": collection_create_time.astimezone(
                timezone.utc
            ).isoformat(),
            "collection_update_time": collection_update_time.astimezone(
                timezone.utc
            ).isoformat(),
            "index_policy_version": index_policy_version,
            "object_schema_version": AGENT_RETRIEVAL_OBJECT_SCHEMA_VERSION,
            "search_mode": search_mode,
            "embedding_config": embedding_config,
            "fusion_policy_version": fusion_policy_version,
            "query_text_sha256": hashlib.sha256(
                query_text.encode("utf-8")
            ).hexdigest(),
            "filters": filters.model_dump(mode="json"),
            "limit": limit,
            "hits": hits,
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
