"""Fixture-only market provider for the public FitCheck distribution.

The private canonical repository has additional provider integration code.
That implementation, its credentials, and its operational policy are not
part of this distribution. Public builds accept only frozen fixtures or an
explicit in-memory market list and fail closed for live-provider selection.
"""

import json
import os
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from el.domain.structures import ExtractedStructure


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CandidateMarketRecord(_Record):
    """One project-authored synthetic market fixture."""

    market_id: str
    title: str
    venue: str = "Synthetic"
    description: str = ""
    resolution_rules: str = ""
    close_date: date | None = None
    outcomes: list[str] = ["Yes", "No"]
    current_probability: float | None = None
    liquidity_usd: float | None = None
    taxonomy_l1: str | None = None
    taxonomy_confidence: float | None = None
    taxonomy_low_confidence: bool | None = None
    tags: list[str] = []
    source_url: str | None = None


class MarketRetrievalResult(_Record):
    mode: str
    snapshot_id: str
    as_of_ts: datetime
    retrieval_id: str
    markets: list[CandidateMarketRecord]
    query_summary: dict[str, Any] = {}
    excluded_summary: dict[str, Any] = {}


class MarketProvider(Protocol):
    name: str

    def retrieve(
        self, structure: ExtractedStructure | None = None
    ) -> MarketRetrievalResult:
        """Return a bounded fixture universe with audit metadata."""


def _retrieval_id(
    *, mode: str, snapshot_id: str, claim_summary: str, market_ids: list[str]
) -> str:
    payload = repr((mode, snapshot_id, claim_summary, sorted(market_ids))).encode()
    return f"retr_{sha256(payload).hexdigest()[:16]}"


class FixtureMarketProvider:
    """Frozen provider used by public tests and the local product walkthrough."""

    def __init__(
        self,
        *,
        snapshot_id: str,
        as_of_ts: datetime,
        markets: list[CandidateMarketRecord],
        name: str = "fixture",
    ):
        self.name = name
        self._snapshot_id = snapshot_id
        self._as_of_ts = as_of_ts
        self._markets = list(markets)

    @classmethod
    def from_path(cls, path: str | Path) -> "FixtureMarketProvider":
        raw = json.loads(Path(path).read_text())
        return cls(
            snapshot_id=raw["snapshot_id"],
            as_of_ts=datetime.fromisoformat(raw["as_of_ts"]),
            markets=[CandidateMarketRecord.model_validate(row) for row in raw["markets"]],
            name=raw.get("name", "fixture"),
        )

    def retrieve(
        self, structure: ExtractedStructure | None = None
    ) -> MarketRetrievalResult:
        claim_summary = structure.claim_summary if structure else ""
        market_ids = [market.market_id for market in self._markets]
        return MarketRetrievalResult(
            mode=self.name,
            snapshot_id=self._snapshot_id,
            as_of_ts=self._as_of_ts,
            retrieval_id=_retrieval_id(
                mode=self.name,
                snapshot_id=self._snapshot_id,
                claim_summary=claim_summary,
                market_ids=market_ids,
            ),
            markets=list(self._markets),
            query_summary={
                "source": "frozen_fixture",
                "claim_present": structure is not None,
                "returned_count": len(self._markets),
            },
        )


def build_market_provider(
    *,
    markets: list[CandidateMarketRecord] | None = None,
    snapshot_id: str = "adhoc",
    fixture_path: str | Path | None = None,
) -> MarketProvider:
    if markets is not None:
        return FixtureMarketProvider(
            snapshot_id=snapshot_id,
            as_of_ts=datetime.now(timezone.utc),
            markets=markets,
        )

    provider_name = os.environ.get("MARKET_PROVIDER", "fixture").strip().lower()
    if provider_name == "polydata":
        raise RuntimeError(
            "MARKET_PROVIDER=polydata is not included in the public distribution; "
            "use a frozen fixture or an explicit market list"
        )
    if provider_name != "fixture":
        raise RuntimeError(
            f"unsupported MARKET_PROVIDER={provider_name!r}; the public "
            "distribution supports only fixture retrieval"
        )

    path = fixture_path or os.environ.get("FIXTURE_SNAPSHOT_PATH")
    if not path:
        raise RuntimeError(
            "fixture provider requires markets, fixture_path, or "
            "FIXTURE_SNAPSHOT_PATH"
        )
    return FixtureMarketProvider.from_path(path)


__all__ = [
    "CandidateMarketRecord",
    "FixtureMarketProvider",
    "MarketProvider",
    "MarketRetrievalResult",
    "build_market_provider",
]
