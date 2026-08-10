"""Market-structure proposer adapters — the contract-side model layer.

Same discipline as claim-side adapters (el.models.adapter): agents
propose; deterministic systems verify. Adapters produce a
MarketStructure; the market-structure gate (el.marketstructure.gate)
owns every decision about it.

The model proposes JUDGMENT fields only (stage, metric, horizon,
entities, threshold, direction, source class); the adapter assembles the
full MarketStructure with the ids it was asked about — a model never
fills identity fields.
"""

import os
import uuid
from datetime import date
from typing import Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from el.domain.enums import EventStage, ResolutionSourceClass
from el.domain.structures import (
    Entity,
    MarketStructure,
    Metric,
    enforce_schema_version_v1,
)

# v2 (2026-06-12, blueprint §4 semantics pin): resolution_date is the
# CONDITION DEADLINE, never the administrative settlement date.
MARKET_EXTRACTION_POLICY_VERSION = 2


class MarketProposerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structure: MarketStructure
    model_adapter: str
    model_run_id: str


class MarketStructureProposer(Protocol):
    def propose_market_structure(
        self,
        *,
        market_id: str,
        snapshot_id: str,
        contract_terms_text: str,
        resolution_rules_text: str,
    ) -> MarketProposerResult: ...


class FixtureMarketStructureProposer:
    """Deterministic proposer over golden structures, keyed by market_id.

    snapshot_id is stamped from the request: goldens teach the judgment
    fields; identity always comes from the call.
    """

    def __init__(self, fixtures: dict[str, MarketStructure]):
        self._fixtures = fixtures
        self.calls = 0

    def propose_market_structure(
        self,
        *,
        market_id: str,
        snapshot_id: str,
        contract_terms_text: str,
        resolution_rules_text: str,
    ) -> MarketProposerResult:
        self.calls += 1
        golden = self._fixtures[market_id]
        return MarketProposerResult(
            structure=golden.model_copy(
                update={"market_id": market_id, "snapshot_id": snapshot_id}
            ),
            model_adapter="fixture",
            model_run_id=f"fixture-{self.calls}",
        )


class _ProposedMarketFields(BaseModel):
    """Response schema for the model: judgment fields only, no identity."""

    model_config = ConfigDict(extra="forbid")

    event_stage: EventStage
    metric: Metric
    resolution_date: date
    timezone: str = "UTC"
    entities: list[Entity]
    threshold: str | None = None
    direction: str | None = None
    resolution_source_class: ResolutionSourceClass
    schema_version: int = 1

    @field_validator("schema_version")
    @classmethod
    def _schema_version_v1(cls, v: int) -> int:
        return enforce_schema_version_v1(v)


_MARKET_EXTRACTION_PROMPT = """\
You normalize a prediction-market contract into a precise structure for
an expression-risk audit. Work ONLY from the contract text below; never
invent conditions that are not written.

Rules:
- `event_stage` is the stage the market RESOLVES on (announced, launched,
  shipped, adopted, measured, resolved) — not the stage the title implies.
- `metric.what` states exactly what is measured; `metric.measured_by`
  names who or what attests it; `objective` is false if resolution needs
  judgment calls.
- `resolution_date` is the CONDITION DEADLINE: the date by which the
  resolving condition must occur, ISO format. It is NOT the
  administrative settlement date — when rules say "condition by Dec 31,
  settles by Jan 31", the answer is Dec 31.
- `threshold` and `direction` capture numeric/comparative conditions
  ("above $500", "decline") when present, else null.
- `resolution_source_class`: official, leaderboard, filing, press, none.
- This is analysis, not advice. Never use trading words anywhere.

Market title:
---
{contract_terms_text}
---
Resolution rules:
---
{resolution_rules_text}
---
"""


class GeminiMarketStructureProposer:
    """Gemini structured-output proposer (lazy import; key via env)."""

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    def propose_market_structure(
        self,
        *,
        market_id: str,
        snapshot_id: str,
        contract_terms_text: str,
        resolution_rules_text: str,
    ) -> MarketProposerResult:
        from google import genai  # lazy: tests never import the SDK

        client = genai.Client()  # GEMINI_API_KEY from env
        run_id = str(uuid.uuid4())
        response = client.models.generate_content(
            model=self.model,
            contents=_MARKET_EXTRACTION_PROMPT.format(
                contract_terms_text=contract_terms_text,
                resolution_rules_text=resolution_rules_text,
            ),
            config={
                "response_mime_type": "application/json",
                "response_schema": _ProposedMarketFields,
                "temperature": 0.0,
            },
        )
        fields = _ProposedMarketFields.model_validate_json(response.text)
        structure = MarketStructure(
            market_id=market_id,
            snapshot_id=snapshot_id,
            event_stage=fields.event_stage,
            metric=fields.metric,
            horizon={
                "resolution_date": fields.resolution_date,
                "timezone": fields.timezone,
            },
            entities=fields.entities,
            threshold=fields.threshold,
            direction=fields.direction,
            resolution_source_class=fields.resolution_source_class,
            extraction_policy_version=MARKET_EXTRACTION_POLICY_VERSION,
        )
        return MarketProposerResult(
            structure=structure,
            model_adapter=f"gemini:{self.model}",
            model_run_id=run_id,
        )
