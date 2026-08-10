"""Immutable contracts for the private classification-worker slice."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from el.retrieval.agent_retrieval_index import (
    agent_retrieval_object_schema_descriptor,
)
from el.retrieval.candidate_contracts import CandidateIndexPort


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ClassificationPayload(_Contract):
    """The mutable job payload is deliberately reference-only."""

    thesis_analysis_id: uuid.UUID


class ThesisPin(_Contract):
    thesis_analysis_id: uuid.UUID
    schema_version: Literal[1] = 1
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SnapshotPin(_Contract):
    snapshot_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    venue: str = Field(min_length=1, max_length=64)
    cutoff_utc: datetime
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_uri: str = Field(min_length=1, max_length=2048)
    normalization_policy_version: str = Field(min_length=1, max_length=64)
    validation_policy_version: str = Field(min_length=1, max_length=64)

    @field_validator("cutoff_utc")
    @classmethod
    def _cutoff_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot cutoff must be timezone-aware")
        return value.astimezone(timezone.utc)


CandidateIndexBackend = Literal["sqlite_lexical", "google_agent_retrieval"]
AgentRetrievalSearchMode = Literal["text", "semantic", "hybrid"]


class ManagedAgentRetrievalDescriptor(_Contract):
    """Sealed control-plane identity for one managed retrieval collection."""

    project_id: str = Field(min_length=1, max_length=128)
    location: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]*$",
    )
    api_version: Literal["v1"] = "v1"
    collection_resource: str = Field(min_length=1, max_length=2048)
    snapshot_id: str = Field(min_length=1, max_length=128)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    create_time: datetime
    update_time: datetime
    expected_open_object_count: int = Field(ge=0)
    object_schema: dict[str, JsonValue] = Field(min_length=1)
    query_policy_version: str = Field(min_length=1, max_length=64)
    search_mode: AgentRetrievalSearchMode
    embedding_config: str = Field(min_length=1, max_length=256)
    fusion_policy_version: str = Field(min_length=1, max_length=128)

    @field_validator("project_id")
    @classmethod
    def _project_is_resource_segment(cls, value: str) -> str:
        if value.strip() != value or "/" in value:
            raise ValueError("Agent Retrieval project must be one resource segment")
        return value

    @field_validator("create_time", "update_time")
    @classmethod
    def _timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Agent Retrieval descriptor times must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _descriptor_is_coherent(self) -> "ManagedAgentRetrievalDescriptor":
        parts = self.collection_resource.split("/")
        if (
            len(parts) != 6
            or parts[0] != "projects"
            or parts[1] != self.project_id
            or parts[2] != "locations"
            or parts[3] != self.location
            or parts[4] != "collections"
            or not parts[5]
        ):
            raise ValueError(
                "Agent Retrieval collection resource differs from project/location"
            )
        if self.update_time < self.create_time:
            raise ValueError("Agent Retrieval update time precedes create time")
        if self.object_schema != agent_retrieval_object_schema_descriptor():
            raise ValueError("Agent Retrieval object schema is unsupported")
        if self.search_mode == "text":
            if (
                self.embedding_config != "disabled"
                or self.fusion_policy_version != "disabled"
            ):
                raise ValueError(
                    "text retrieval cannot claim embedding or fusion configuration"
                )
        elif self.search_mode == "semantic":
            if (
                self.embedding_config == "disabled"
                or self.fusion_policy_version != "disabled"
            ):
                raise ValueError(
                    "semantic retrieval requires embeddings and no fusion policy"
                )
        elif (
            self.embedding_config == "disabled"
            or self.fusion_policy_version == "disabled"
        ):
            raise ValueError(
                "hybrid retrieval requires embedding and fusion configuration"
            )
        return self

    @property
    def canonical_sha256(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CandidateIndexPin(_Contract):
    backend: CandidateIndexBackend = "sqlite_lexical"
    snapshot_id: str = Field(min_length=1, max_length=128)
    index_uri: str | None = Field(default=None, min_length=1, max_length=2048)
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=64)
    managed_descriptor: ManagedAgentRetrievalDescriptor | None = None

    @model_validator(mode="after")
    def _backend_identity_is_coherent(self) -> "CandidateIndexPin":
        if self.backend == "sqlite_lexical":
            if self.index_uri is None or self.managed_descriptor is not None:
                raise ValueError(
                    "SQLite candidate indexes require only a pinned index URI"
                )
        else:
            if self.index_uri is not None or self.managed_descriptor is None:
                raise ValueError(
                    "managed candidate indexes require only a sealed descriptor"
                )
            if self.snapshot_id != self.managed_descriptor.snapshot_id:
                raise ValueError(
                    "managed candidate index and descriptor snapshots differ"
                )
            if self.policy_version != self.managed_descriptor.query_policy_version:
                raise ValueError(
                    "managed candidate index and descriptor policies differ"
                )
            if self.index_sha256 != self.managed_descriptor.canonical_sha256:
                raise ValueError(
                    "managed candidate index identity differs from descriptor SHA"
                )
        return self


class RetrievalPins(_Contract):
    ranking_policy_version: str = Field(min_length=1, max_length=64)
    eligibility_policy_version: str = Field(min_length=1, max_length=64)
    min_liquidity_usd: float = Field(ge=0, allow_inf_nan=False)
    min_taxonomy_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    horizon_slack_days: int = Field(ge=0, le=3650)
    query_limit: int = Field(ge=1, le=200)


class StructurePins(_Contract):
    schema_version: Literal[1] = 1
    extraction_policy_version: int = Field(ge=1)
    gate_policy_version: str = Field(min_length=1, max_length=64)
    model_adapter: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=128)
    structure_limit: int = Field(ge=1, le=200)


class FitPins(_Contract):
    gate_policy_version: str = Field(min_length=1, max_length=64)
    token_rules_version: str = Field(min_length=1, max_length=64)
    alias_rules_version: str = Field(min_length=1, max_length=64)
    stacking_threshold: int = Field(ge=1, le=20)
    escalation_confidence_floor: float = Field(
        ge=0, le=1, allow_inf_nan=False
    )
    horizon_tolerances: dict[str, tuple[int, int]]
    m1_direction_guard: bool
    advisory_enabled: Literal[False] = False
    advisory_model_version: Literal["disabled"] = "disabled"
    advisory_prompt_version: Literal["disabled"] = "disabled"

    @field_validator("horizon_tolerances")
    @classmethod
    def _horizon_tolerances_are_complete(
        cls, value: dict[str, tuple[int, int]]
    ) -> dict[str, tuple[int, int]]:
        if set(value) != {"day", "month", "quarter", "year"}:
            raise ValueError("horizon tolerances must cover every precision")
        normalized: dict[str, tuple[int, int]] = {}
        for precision, (good_days, fair_days) in sorted(value.items()):
            if good_days < 0 or fair_days < good_days or fair_days > 3650:
                raise ValueError("horizon tolerances are invalid")
            normalized[precision] = (good_days, fair_days)
        return normalized


class ClassificationPinManifest(_Contract):
    schema_version: Literal["fitcheck_classification_pins_v1"] = (
        "fitcheck_classification_pins_v1"
    )
    thesis: ThesisPin
    snapshot: SnapshotPin
    candidate_index: CandidateIndexPin
    retrieval: RetrievalPins
    structure: StructurePins
    fit: FitPins
    code_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _identities_are_coherent(self) -> "ClassificationPinManifest":
        if self.candidate_index.snapshot_id != self.snapshot.snapshot_id:
            raise ValueError("candidate index and snapshot identities differ")
        descriptor = self.candidate_index.managed_descriptor
        if descriptor is not None and (
            descriptor.snapshot_id != self.snapshot.snapshot_id
            or descriptor.content_sha256 != self.snapshot.content_sha256
            or descriptor.artifact_sha256 != self.snapshot.artifact_sha256
        ):
            raise ValueError(
                "managed candidate descriptor differs from the pinned snapshot"
            )
        if self.structure.structure_limit > self.retrieval.query_limit:
            raise ValueError("structure limit cannot exceed query limit")
        return self


class ClassificationWorkerResult(_Contract):
    claimed: bool
    status: Literal[
        "idle",
        "succeeded",
        "retry_wait",
        "failed",
        "needs_operator",
        "lost_authority",
    ]
    job_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    fit_card_id: uuid.UUID | None = None
    market_recommendation_id: uuid.UUID | None = None
    fit_class: str | None = None
    recommended_market_id: str | None = None
