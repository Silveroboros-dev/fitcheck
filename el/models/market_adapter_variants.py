"""Unpinned adapter variants for sealed replay and the local canary.

The historical ``market_adapter`` module is hash-bound by the blind holdout.
Keep that file byte-for-byte stable and reuse its schemas, prompt, and policy
constants here for newly introduced behavior.
"""

from __future__ import annotations

import uuid
from hashlib import sha256

from el.domain.structures import MarketStructure
from el.models.market_adapter import (
    MARKET_EXTRACTION_POLICY_VERSION,
    MarketProposerResult,
    _MARKET_EXTRACTION_PROMPT,
    _ProposedMarketFields,
    FixtureMarketStructureProposer as _BaseFixtureProposer,
)


class FixtureMarketStructureUnavailable(RuntimeError):
    """A replay market cannot reuse a fixture structure under changed terms."""


class FixtureMarketStructureProposer(_BaseFixtureProposer):
    """Fixture proposer with optional exact-term binding for sealed replay."""

    def __init__(self, fixtures, *, expected_terms=None):
        super().__init__(fixtures)
        self._expected_terms = expected_terms

    def propose_market_structure(
        self,
        *,
        market_id: str,
        snapshot_id: str,
        contract_terms_text: str,
        resolution_rules_text: str,
    ) -> MarketProposerResult:
        if self._expected_terms is not None:
            expected = self._expected_terms.get(market_id)
            actual = (
                sha256(contract_terms_text.encode()).hexdigest(),
                sha256(resolution_rules_text.encode()).hexdigest(),
            )
            if expected != actual:
                raise FixtureMarketStructureUnavailable(
                    "fixture structure terms do not match the replay capture"
                )
        return super().propose_market_structure(
            market_id=market_id,
            snapshot_id=snapshot_id,
            contract_terms_text=contract_terms_text,
            resolution_rules_text=resolution_rules_text,
        )


class VertexCanaryMarketStructureProposer:
    """Market proposer using the explicit shared Vertex canary operation."""

    def __init__(self, model: str, *, operation):
        self.model = model
        self._operation = operation

    def propose_market_structure(
        self,
        *,
        market_id: str,
        snapshot_id: str,
        contract_terms_text: str,
        resolution_rules_text: str,
    ) -> MarketProposerResult:
        contents = _MARKET_EXTRACTION_PROMPT.format(
            contract_terms_text=contract_terms_text,
            resolution_rules_text=resolution_rules_text,
        )
        response = self._operation.generate(
            stage="market_structure",
            contents=contents,
            config={
                "response_mime_type": "application/json",
                "response_schema": _ProposedMarketFields,
                "temperature": 0.0,
            },
            request_identity={
                "market_id": market_id,
                "snapshot_id": snapshot_id,
                "contract_terms_sha256": sha256(
                    contract_terms_text.encode()
                ).hexdigest(),
                "resolution_rules_sha256": sha256(
                    resolution_rules_text.encode()
                ).hexdigest(),
                "policy_version": MARKET_EXTRACTION_POLICY_VERSION,
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
            model_adapter=f"vertex:{self.model}",
            model_run_id=str(uuid.uuid4()),
        )


__all__ = [
    "FixtureMarketStructureProposer",
    "FixtureMarketStructureUnavailable",
    "VertexCanaryMarketStructureProposer",
]
