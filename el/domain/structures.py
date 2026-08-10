"""Extracted-structure schema v1 — both sides of the deterministic gate.

RATIFIED AND FROZEN 2026-06-12 (blueprint §4). The fit gate compares
ExtractedStructure (claim side) against MarketStructure (contract side) —
never against raw text or model rationale. Schema changes go through
Loop 4 promotion discipline; bump schema_version, never mutate v1.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from el.domain.enums import (
    EventStage,
    HorizonPrecision,
    ResolutionSourceClass,
    Stance,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def enforce_schema_version_v1(v: int) -> int:
    """Frozen v1 invariant, preserved across the Vertex-compat relaxation.

    These fields were ``schema_version: Literal[1]``; a single-value int Literal
    emits a non-string const in the JSON schema, which Vertex AI's
    structured-output converter rejects ("Literal values must be strings"). The
    annotation is now a plain ``int`` (Vertex-safe) and this validator keeps the
    invariant — any value other than 1 still fails at model validation, so v1
    stays frozen (bump to a new schema, never mutate v1)."""
    if v != 1:
        raise ValueError(
            "schema_version must be 1 (frozen v1; bump to a new schema, never "
            "mutate v1)"
        )
    return v


class Entity(_Frozen):
    name: str = Field(min_length=1)
    role: Literal["subject", "object", "venue", "source"]


class Metric(_Frozen):
    what: str = Field(min_length=1)
    measured_by: str
    objective: bool


class ClaimHorizon(_Frozen):
    window_start: date | None = None
    window_end: date
    timezone: str = "UTC"
    precision: HorizonPrecision

    @model_validator(mode="after")
    def _window_ordered(self) -> "ClaimHorizon":
        if self.window_start and self.window_start > self.window_end:
            raise ValueError("window_start must not be after window_end")
        return self


class Mechanism(_Frozen):
    asserted_causal_chain: str | None = None
    is_composite: bool = False


class ExtractedStructure(_Frozen):
    """Claim-side normalized structure (Loop 1 output, Loop 3 input)."""

    schema_version: int = 1
    claim_summary: str = Field(min_length=1)
    entities: list[Entity] = Field(min_length=1)
    event_stage: EventStage
    metric: Metric
    horizon: ClaimHorizon
    mechanism: Mechanism
    stance: Stance
    resolution_source_class: ResolutionSourceClass
    ambiguities: list[str] = Field(default_factory=list)
    contractible_version: str = Field(min_length=1)

    @field_validator("schema_version")
    @classmethod
    def _schema_version_v1(cls, v: int) -> int:
        return enforce_schema_version_v1(v)


class MarketHorizon(_Frozen):
    resolution_date: date
    timezone: str = "UTC"


class MarketStructure(_Frozen):
    """Contract-side normalized structure (Loop 2.5 output, Loop 3 input).

    Extracted by a proposer, validated by a schema gate; golden rows live
    in the governed eval set alongside golden claim structures.
    """

    schema_version: int = 1
    market_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    event_stage: EventStage
    metric: Metric
    horizon: MarketHorizon
    entities: list[Entity] = Field(min_length=1)
    threshold: str | None = None
    direction: str | None = None
    resolution_source_class: ResolutionSourceClass
    extraction_policy_version: int = 1

    @field_validator("schema_version")
    @classmethod
    def _schema_version_v1(cls, v: int) -> int:
        return enforce_schema_version_v1(v)


class Provenance(_Frozen):
    """Run/policy provenance carried by every fit output.

    Reproducibility means re-running the same policy over the same
    structures yields the same judgment — partial provenance is no
    provenance (blueprint §5).
    """

    gate_policy_version: str
    extraction_schema_version: int
    market_structure_schema_version: int
    model_adapter: str
    model_run_id: str
    trace_id: str
    eval_pack_version: str
    judged_at: datetime
