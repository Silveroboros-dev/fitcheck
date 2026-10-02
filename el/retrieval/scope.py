"""Persisted, display-safe scope for one retrieved candidate set.

This is intentionally a projection of the ``MarketRetrievalResult`` that
actually entered ``RetrievalService``.  It is not a provider configuration
record and it does not make a bounded observation into a catalog claim.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field

from el.retrieval.provider import MarketRetrievalResult


class CandidateSetRetrievalScope(BaseModel):
    """Small immutable provenance projection stored with a candidate set.

    ``query_text`` is intentionally the exact query from the observed request,
    when one exists. It is transport provenance, not a verified literal quote
    from the user's source, so product/MCP output-language guards check it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1, max_length=128)
    scope_kind: str = Field(min_length=1, max_length=64)
    query_text: str | None = Field(default=None, max_length=512)
    query_binding: str | None = Field(default=None, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=128)
    retrieval_id: str = Field(min_length=1, max_length=128)
    returned_count: int = Field(ge=0)
    lifecycle_eligible_count: int = Field(ge=0)
    lifecycle_excluded_count: int = Field(ge=0)
    gate_eligible_count: int = Field(ge=0)


def candidate_set_retrieval_scope(
    result: MarketRetrievalResult, *, gate_eligible_count: int
) -> CandidateSetRetrievalScope:
    """Return the safe scope attached to this exact retrieval result.

    Providers expose broader diagnostic dictionaries, but the durable product
    record needs only counts, the bounded-observation basis, and the exact
    query that was actually observed.  Unknown provider-specific fields are
    intentionally excluded.
    """

    query_summary = result.query_summary or {}
    excluded_summary = result.excluded_summary or {}
    source = query_summary.get("source")
    scope_kind = {
        "gamma_public_search_capture": "bounded_query_observation",
        "frozen_fixture": "frozen_snapshot",
        "market_universe": "market_universe",
    }.get(source, "provider_result")
    query_text = _observed_query(query_summary.get("request_url"))
    query_binding = query_summary.get("query_binding")
    if not isinstance(query_binding, str):
        query_binding = None

    returned_count = _count(
        query_summary.get("market_count"), fallback=len(result.markets)
    )
    lifecycle_eligible_count = _count(
        query_summary.get("replayable_market_count"),
        fallback=len(result.markets),
    )
    lifecycle_excluded_count = _count(
        excluded_summary.get("lifecycle_excluded_count"),
        fallback=max(returned_count - lifecycle_eligible_count, 0),
    )
    # A malformed provider summary must not claim fewer returned markets than
    # entered the eligible replay.  The records are the source of truth here.
    returned_count = max(returned_count, lifecycle_eligible_count)

    return CandidateSetRetrievalScope(
        provider=result.mode,
        scope_kind=scope_kind,
        query_text=query_text,
        query_binding=query_binding,
        snapshot_id=result.snapshot_id,
        retrieval_id=result.retrieval_id,
        returned_count=returned_count,
        lifecycle_eligible_count=lifecycle_eligible_count,
        lifecycle_excluded_count=lifecycle_excluded_count,
        gate_eligible_count=gate_eligible_count,
    )


def persisted_candidate_set_scope(
    value: dict[str, Any] | None,
) -> CandidateSetRetrievalScope | None:
    """Read legacy nulls as unknown without reconstructing old provider state."""

    if value is None:
        return None
    return CandidateSetRetrievalScope.model_validate(value)


def _observed_query(request_url: object) -> str | None:
    if not isinstance(request_url, str):
        return None
    values = parse_qs(urlparse(request_url).query, keep_blank_values=True).get("q")
    if values is None or len(values) != 1 or not values[0]:
        return None
    return values[0]


def _count(value: object, *, fallback: int) -> int:
    # bool is an int subclass but never a meaningful count.
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback
