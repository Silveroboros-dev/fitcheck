"""schema_version v1 invariant survives the Vertex-compat relaxation.

`Literal[1]` emitted a non-string const in the JSON schema, which Vertex AI's
structured-output converter rejects. The annotation is now plain `int`, but a
validator preserves the invariant: schema_version != 1 still fails.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from el.domain.structures import (
    ExtractedStructure,
    MarketStructure,
    enforce_schema_version_v1,
)
from el.models.market_adapter import _ProposedMarketFields

_CLAIM_STRUCTURE = json.loads(
    sorted((Path(__file__).parent / "fixtures" / "claims").glob("*.json"))[0].read_text()
)["proposal"]["structure"]


@pytest.mark.parametrize(
    "model", [ExtractedStructure, MarketStructure, _ProposedMarketFields]
)
def test_vertex_bound_schema_version_is_plain_integer(model):
    # The Vertex-bound JSON schema must NOT emit a non-string Literal/const/enum
    # for schema_version (the converter rejects those) — just a plain integer.
    prop = model.model_json_schema()["properties"]["schema_version"]
    assert prop.get("type") == "integer"
    assert "const" not in prop
    assert "enum" not in prop


def test_schema_version_1_passes():
    s = ExtractedStructure.model_validate({**_CLAIM_STRUCTURE, "schema_version": 1})
    assert s.schema_version == 1


def test_schema_version_2_fails_validation():
    with pytest.raises(ValidationError):
        ExtractedStructure.model_validate({**_CLAIM_STRUCTURE, "schema_version": 2})


def test_enforce_helper_directly():
    # The shared invariant used by all three models.
    assert enforce_schema_version_v1(1) == 1
    with pytest.raises(ValueError):
        enforce_schema_version_v1(2)
