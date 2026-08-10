from datetime import datetime, timezone
from pathlib import Path

import pytest

import el.retrieval.provider as provider_module
from el.retrieval.provider import (
    CandidateMarketRecord,
    FixtureMarketProvider,
    build_market_provider,
)


SNAPSHOT = (
    Path(__file__).parent / "fixtures" / "retrieval" / "frozen_snapshot_phase0.json"
)


def test_fixture_path_replays_without_live_adapter(monkeypatch):
    monkeypatch.delenv("MARKET_PROVIDER", raising=False)
    provider = build_market_provider(fixture_path=SNAPSHOT)
    assert isinstance(provider, FixtureMarketProvider)
    result = provider.retrieve()
    assert result.query_summary["source"] == "frozen_fixture"
    assert len(result.markets) == 10
    assert not hasattr(provider_module, "PolyDataMarketProvider")


def test_explicit_markets_remain_available(monkeypatch):
    monkeypatch.setenv("MARKET_PROVIDER", "polydata")
    market = CandidateMarketRecord(market_id="synthetic-1", title="Synthetic")
    provider = build_market_provider(markets=[market], snapshot_id="synthetic-snapshot")
    assert provider.retrieve().markets == [market]


def test_live_provider_selection_fails_closed(monkeypatch):
    monkeypatch.setenv("MARKET_PROVIDER", "polydata")
    with pytest.raises(RuntimeError, match="not included in the public distribution"):
        build_market_provider(fixture_path=SNAPSHOT)


def test_unknown_provider_selection_fails_closed(monkeypatch):
    monkeypatch.setenv("MARKET_PROVIDER", "unknown-live-provider")
    with pytest.raises(RuntimeError, match="supports only fixture retrieval"):
        build_market_provider(fixture_path=SNAPSHOT)
