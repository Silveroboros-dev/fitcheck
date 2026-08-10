"""Backend-neutral contracts for bounded candidate retrieval indexes.

The immutable market-universe snapshot remains the source of truth.  An index
is a rebuildable retrieval derivative whose identity and query policy are
pinned by the caller.  Concrete adapters may expose additional diagnostics,
but classification only needs the common contracts in this module.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from el.domain.structures import ExtractedStructure
from el.retrieval.snapshot_contracts import NormalizedUniverseMarket

MAX_CANDIDATE_QUERY_LIMIT = 200


class CandidateIndexError(RuntimeError):
    """Base error safe for candidate-index orchestration boundaries."""


class CandidateIndexTransientError(CandidateIndexError):
    """The same pinned query may succeed on a bounded retry."""


class CandidateIndexPermanentError(CandidateIndexError):
    """The request or configured adapter cannot succeed unchanged."""


class CandidateIndexIntegrityError(CandidateIndexPermanentError):
    """Returned or stored index evidence conflicts with its pinned identity."""


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CandidateQueryHit(_Contract):
    """One hydrated market returned by a concrete retrieval backend.

    Tuple order is the backend rank.  ``source_score`` is deliberately optional:
    not every backend exposes a comparable numeric score, and FitCheck performs
    its version-pinned deterministic ranking after candidate retrieval.
    """

    market: NormalizedUniverseMarket
    source_score: float | None = Field(default=None, allow_inf_nan=False)


class CandidateQueryResult(_Contract):
    """Common result required by the private classification worker."""

    backend: str = Field(min_length=1, max_length=64)
    snapshot_id: str = Field(min_length=1, max_length=128)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Kept under the existing name until the classification pin is migrated.
    # File adapters use the byte digest; managed adapters use the SHA-256 of a
    # canonical, sealed index descriptor.
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_policy_version: str = Field(min_length=1, max_length=128)
    query_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    limit: int = Field(ge=1, le=MAX_CANDIDATE_QUERY_LIMIT)
    hits: tuple[CandidateQueryHit, ...]
    backend_request_id: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _hits_are_bounded(self) -> "CandidateQueryResult":
        if len(self.hits) > self.limit:
            raise ValueError("candidate query returned more hits than requested")
        return self

    @property
    def returned_count(self) -> int:
        return len(self.hits)


class CandidateIndexPort(Protocol):
    """Read-only handle to one pinned candidate-index identity."""

    backend: str
    snapshot_id: str
    content_sha256: str
    artifact_sha256: str
    index_policy_version: str
    index_sha256: str

    def query(
        self,
        structure: ExtractedStructure,
        *,
        limit: int,
    ) -> CandidateQueryResult: ...
