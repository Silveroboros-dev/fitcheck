"""Packaged, project-authored AGI comparison for the anonymous fixture demo."""

import json
from datetime import date
from hashlib import sha256
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Fixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CuratedMarket(_Fixture):
    key: Literal["rsi", "millennium", "agi"]
    title: str = Field(min_length=1)
    deadline: date
    conditions_summary: str = Field(min_length=1)
    captures: list[str] = Field(min_length=1)
    misses: list[str] = Field(min_length=1)
    earlier_class: Literal["not_an_expression"]
    current_class: Literal["weak_proxy", "not_an_expression"]
    relatedness_explanation: str = Field(min_length=1)
    outcome_example: str = Field(min_length=1)


class CuratedAgiCase(_Fixture):
    schema_version: Literal["fitcheck.agi2028-curated-demo.v1"]
    case_id: Literal["agi2028"]
    title: str = Field(min_length=1)
    fixture_kind: Literal["curated_real_contract_comparison"]
    content_date: date
    thesis_wording: str = Field(min_length=1)
    criterion_note: str = Field(min_length=1)
    provenance_note: str = Field(min_length=1)
    pool_outcome: Literal["no_clean_expression"]
    pool_note: str = Field(min_length=1)
    proposal_note: str = Field(min_length=1)
    next_steps: str = Field(min_length=1)
    markets: list[CuratedMarket] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def exact_case(self):
        if [market.key for market in self.markets] != ["rsi", "millennium", "agi"]:
            raise ValueError("curated AGI market order changed")
        if [market.current_class for market in self.markets] != [
            "weak_proxy", "not_an_expression", "not_an_expression"
        ]:
            raise ValueError("curated AGI proposal classes changed")
        strings = [
            self.title, self.thesis_wording, self.criterion_note,
            self.provenance_note, self.pool_note, self.proposal_note, self.next_steps,
        ]
        for market in self.markets:
            strings.extend([
                market.title, market.conditions_summary,
                market.relatedness_explanation, market.outcome_example,
                *market.captures, *market.misses,
            ])
        if any(not value.strip() for value in strings):
            raise ValueError("curated AGI copy must be nonempty")
        return self


def load_curated_agi_case() -> dict:
    """Validate the bundled case and seal the exact served JSON payload."""
    resource = files("el.product.demo_fixtures").joinpath("agi2028.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    CuratedAgiCase.model_validate(payload)
    digest = sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return {"case": payload, "sha256": digest}
