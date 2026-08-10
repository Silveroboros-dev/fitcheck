"""Immutable contracts for the shared market-universe snapshot boundary."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SnapshotKey(_Contract):
    provider: str = Field(min_length=1, max_length=64)
    venue: str = Field(min_length=1, max_length=64)

    @field_validator("provider", "venue")
    @classmethod
    def _key_parts_are_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("snapshot key parts must be nonblank")
        return cleaned


class NormalizedUniverseMarket(_Contract):
    """Provider-neutral metadata used by retrieval indexing.

    Prices are intentionally absent. Candidate price hydration is a separate,
    bounded classification stage with its own ``as_of`` provenance.
    """

    market_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1)
    slug: str | None = None
    description: str = ""
    resolution_rules: str = ""
    outcomes: list[str] = Field(min_length=2)
    token_ids: list[str] = Field(min_length=1)
    close_date: date | None = None
    # Raw provider closure evidence is retained so validation does not have to
    # trust a source-derived ``is_open`` flag.
    closed_time: datetime | None = None
    snapshot_ts: datetime
    is_open: bool
    volume_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    taxonomy_l1: str | None = None
    taxonomy_confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    tags: list[str] = Field(default_factory=list)
    source_url: str | None = None

    @field_validator("market_id", "title")
    @classmethod
    def _identity_text_is_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("market ID and title must be nonblank")
        return cleaned

    @field_validator("slug")
    @classmethod
    def _slug_is_nonblank_when_present(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("slug must be nonblank when present")
        return cleaned

    @field_validator("snapshot_ts", "closed_time")
    @classmethod
    def _timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("outcomes", "token_ids")
    @classmethod
    def _ordered_labels_are_canonical(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("outcomes and token IDs must be nonblank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("outcomes and token IDs must be unique")
        return cleaned

    @field_validator("tags")
    @classmethod
    def _tags_are_canonical(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("tags must be nonblank")
        return sorted(set(cleaned))

    @model_validator(mode="after")
    def _closure_evidence_is_consistent(self) -> "NormalizedUniverseMarket":
        if self.is_open != (self.closed_time is None):
            raise ValueError("is_open must agree with closed_time")
        if len(self.outcomes) != len(self.token_ids):
            raise ValueError("outcomes and token IDs must have equal length")
        return self


class SnapshotValidationPolicy(_Contract):
    version: str = Field(min_length=1, max_length=64)
    minimum_row_count: int = Field(default=1, ge=1)
    required_sentinel_ids: tuple[str, ...] = ()
    direct_sentinel_id: str | None = None
    direct_expected_title: str | None = None
    direct_expected_slug: str | None = None
    require_sentinel_cutoff_timestamp: bool = True
    diagnostics_limit: int = Field(default=50, ge=1, le=500)

    @field_validator("version")
    @classmethod
    def _version_is_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("validation policy version must be nonblank")
        return cleaned

    @field_validator("required_sentinel_ids")
    @classmethod
    def _sentinels_are_unique(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        if len(cleaned) != len(set(cleaned)) or any(not item for item in cleaned):
            raise ValueError("required sentinel IDs must be nonblank and unique")
        return cleaned

    @field_validator("direct_sentinel_id")
    @classmethod
    def _direct_id_is_nonblank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("direct sentinel ID must be nonblank")
        return cleaned

    @field_validator("direct_expected_title", "direct_expected_slug")
    @classmethod
    def _direct_expectation_is_nonblank(
        cls, value: str | None
    ) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("direct sentinel expectations must be nonblank")
        return cleaned

    @model_validator(mode="after")
    def _direct_sentinel_contract(self) -> "SnapshotValidationPolicy":
        if self.direct_sentinel_id is None:
            if (
                self.direct_expected_title is not None
                or self.direct_expected_slug is not None
            ):
                raise ValueError("direct sentinel expectations require an ID")
            return self
        if self.direct_sentinel_id not in self.required_sentinel_ids:
            raise ValueError("direct sentinel ID must be a required sentinel")
        if not any(
            value is not None and value.strip()
            for value in (self.direct_expected_title, self.direct_expected_slug)
        ):
            raise ValueError("direct sentinel requires an expected title or slug")
        return self


class StagedSnapshot(_Contract):
    path: Path
    key: SnapshotKey
    cutoff_utc: datetime
    artifact_format: Literal["jsonl-v1"] = "jsonl-v1"
    normalization_policy_version: str = Field(min_length=1, max_length=64)
    source_versions: dict[str, str] = Field(default_factory=dict)

    @field_validator("cutoff_utc")
    @classmethod
    def _cutoff_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cutoff_utc must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("normalization_policy_version")
    @classmethod
    def _normalization_version_is_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("normalization policy version must be nonblank")
        return cleaned

    @field_validator("source_versions")
    @classmethod
    def _source_versions_are_nonblank(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned = {key.strip(): version.strip() for key, version in value.items()}
        if len(cleaned) != len(value) or any(
            not key or not version for key, version in cleaned.items()
        ):
            raise ValueError("source version keys and values must be nonblank")
        return cleaned


class SnapshotValidationReport(_Contract):
    passed: bool
    row_count: int
    unique_market_count: int
    open_market_count: int
    content_sha256: str
    membership_sha256: str
    artifact_sha256: str
    artifact_bytes: int
    missing_sentinel_ids: tuple[str, ...] = ()
    closed_sentinel_ids: tuple[str, ...] = ()
    nonbinary_sentinel_ids: tuple[str, ...] = ()
    sentinel_timestamp_mismatch_ids: tuple[str, ...] = ()
    future_market_ids: tuple[str, ...] = ()
    duplicate_market_ids: tuple[str, ...] = ()
    invalid_rows: tuple[str, ...] = ()
    direct_sentinel_match: bool = True
    errors: tuple[str, ...] = ()


class SnapshotManifest(_Contract):
    schema_version: Literal["fitcheck_market_universe_manifest_v1"] = (
        "fitcheck_market_universe_manifest_v1"
    )
    snapshot_id: str = Field(pattern=r"^mu_[0-9a-f]{64}$", max_length=128)
    provider: str
    venue: str
    cutoff_utc: datetime
    generated_at_utc: datetime
    artifact_format: str
    artifact_uri: str
    artifact_sha256: str
    artifact_bytes: int
    content_sha256: str
    membership_sha256: str
    row_count: int
    unique_market_count: int
    open_market_count: int
    normalization_policy_version: str
    validation_policy_version: str
    source_versions: dict[str, str]
    validation_report: SnapshotValidationReport

    @field_validator("cutoff_utc", "generated_at_utc")
    @classmethod
    def _manifest_timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("manifest timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)


class SnapshotRefreshPayload(_Contract):
    provider: str = Field(min_length=1, max_length=64)
    venue: str = Field(min_length=1, max_length=64)
    cutoff_utc: datetime
    validation_policy_version: str = Field(min_length=1, max_length=64)
    normalization_policy_version: str = Field(min_length=1, max_length=64)

    @field_validator("cutoff_utc")
    @classmethod
    def _refresh_cutoff_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cutoff_utc must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator(
        "provider",
        "venue",
        "validation_policy_version",
        "normalization_policy_version",
    )
    @classmethod
    def _refresh_identity_is_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("snapshot refresh identity fields must be nonblank")
        return cleaned


class SnapshotPromotionResult(_Contract):
    snapshot_id: str
    active_snapshot_id: str
    generation: int
    disposition: Literal["promoted", "already_active", "superseded"]


class PublishedSnapshot(_Contract):
    snapshot_id: str
    artifact_uri: str
    manifest_uri: str
    # If an identical artifact already exists, this is the canonical persisted
    # manifest. Callers must use it rather than newly generated attempt metadata.
    manifest: SnapshotManifest


def json_compatible(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value
