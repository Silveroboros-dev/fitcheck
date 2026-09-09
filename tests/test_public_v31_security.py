"""Transport security regressions for the additive public v3 MCP surface.

These tests deliberately use adapters that only record calls.  They prove the
server's boundary ordering for every v3 tool without a model, fixture corpus,
or database-backed worker: authenticate, atomically admit quota, then dispatch.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from el.mcp.auth import AuthError
from el.mcp.rate_limit import TOOL_COST_UNITS, UsageLimiterUnavailable
from el.mcp.server import ALL_TOOL_NAMES, V3_TOOL_NAMES, build_server


class _Response(BaseModel):
    accepted: bool = True


class _LegacyTools:
    """A legacy tool should never be reached while calling a v3 tool."""

    def __getattr__(self, name: str):  # pragma: no cover - guards a failure path
        raise AssertionError(f"unexpected legacy dispatch: {name}")


class _V3Tools:
    def __init__(self, events: list[object]):
        self._events = events

    def _dispatch(self, tool_name: str) -> _Response:
        self._events.append(("dispatch", tool_name))
        return _Response()

    def submit_source_interpretation(self, _principal, **_kwargs):
        return self._dispatch("v3_submit_source_interpretation")

    def get_source_interpretation_job(self, _principal, **_kwargs):
        return self._dispatch("v3_get_source_interpretation_job")

    def get_source_interpretation_job_by_idempotency(self, _principal, **_kwargs):
        return self._dispatch("v3_get_source_interpretation_job_by_idempotency")

    def choose_source_candidate(self, _principal, **_kwargs):
        return self._dispatch("v3_choose_source_candidate")

    def propose_selected_normalization(self, _principal, **_kwargs):
        return self._dispatch("v3_propose_selected_normalization")

    def revise_normalization(self, _principal, **_kwargs):
        return self._dispatch("v3_revise_normalization")

    def accept_normalization(self, _principal, **_kwargs):
        return self._dispatch("v3_accept_normalization")

    def reject_normalization(self, _principal, **_kwargs):
        return self._dispatch("v3_reject_normalization")

    def assess_market_pool(self, _principal, **_kwargs):
        return self._dispatch("v3_assess_market_pool")

    def choose_market(self, _principal, **_kwargs):
        return self._dispatch("v3_choose_market")


def _arguments_by_tool() -> dict[str, dict[str, object]]:
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    digest = "a" * 64
    return {
        "v3_submit_source_interpretation": {
            "input_text": "Synthetic source claim.",
            "idempotency_key": "security-test-submit",
        },
        "v3_get_source_interpretation_job": {"job_id": first},
        "v3_get_source_interpretation_job_by_idempotency": {
            "idempotency_key": "security-test-read",
        },
        "v3_choose_source_candidate": {
            "source_interpretation_id": first,
            "selection_kind": "candidate",
            "source_thesis_candidate_id": second,
        },
        "v3_propose_selected_normalization": {
            "source_thesis_candidate_id": first,
        },
        "v3_revise_normalization": {
            "normalization_attempt_id": first,
            "expected_input_digest": digest,
            "input_text": "Synthetic clarified claim.",
        },
        "v3_accept_normalization": {
            "normalization_attempt_id": first,
            "expected_input_digest": digest,
        },
        "v3_reject_normalization": {
            "normalization_attempt_id": first,
            "expected_input_digest": digest,
        },
        "v3_assess_market_pool": {"thesis_analysis_id": first},
        "v3_choose_market": {
            "market_display_set_id": first,
            "selection_kind": "market",
            "market_assessment_id": second,
        },
    }


def _server(events: list[object], *, resolve=None, consume_usage=None):
    principal = SimpleNamespace()
    resolve = resolve or (lambda _ctx: events.append("resolve") or principal)
    consume_usage = consume_usage or (
        lambda _principal, tool_name: events.append(("consume", tool_name))
    )
    return build_server(
        _LegacyTools(), resolve, consume_usage, v3_tools=_V3Tools(events)
    )


def test_v3_tool_costs_cover_the_exact_registered_surface():
    assert set(ALL_TOOL_NAMES) == set(TOOL_COST_UNITS)
    assert set(V3_TOOL_NAMES) <= set(TOOL_COST_UNITS)
    assert all(isinstance(cost, int) and cost > 0 for cost in TOOL_COST_UNITS.values())


def test_every_v3_tool_authenticates_admits_then_dispatches():
    events: list[object] = []
    server = _server(events)
    arguments_by_tool = _arguments_by_tool()

    registered = {tool.name for tool in asyncio.run(server.list_tools())}
    assert registered == set(ALL_TOOL_NAMES)
    assert set(arguments_by_tool) == set(V3_TOOL_NAMES)

    for tool_name in V3_TOOL_NAMES:
        start = len(events)
        result = asyncio.run(server.call_tool(tool_name, arguments_by_tool[tool_name]))
        assert result
        assert events[start:] == [
            "resolve",
            ("consume", tool_name),
            ("dispatch", tool_name),
        ]


def test_unauthenticated_v3_request_never_consumes_or_dispatches():
    events: list[object] = []

    def reject(_ctx):
        events.append("resolve")
        raise AuthError("missing api key")

    server = _server(events, resolve=reject)
    with pytest.raises(Exception, match="missing api key"):
        asyncio.run(
            server.call_tool(
                "v3_submit_source_interpretation",
                _arguments_by_tool()["v3_submit_source_interpretation"],
            )
        )
    assert events == ["resolve"]


def test_v3_limiter_failure_fails_closed_before_adapter_dispatch():
    events: list[object] = []

    def unavailable(_principal, tool_name):
        events.append(("consume", tool_name))
        raise UsageLimiterUnavailable()

    server = _server(events, consume_usage=unavailable)
    with pytest.raises(Exception, match="temporarily_unavailable"):
        asyncio.run(
            server.call_tool(
                "v3_submit_source_interpretation",
                _arguments_by_tool()["v3_submit_source_interpretation"],
            )
        )
    assert events == ["resolve", ("consume", "v3_submit_source_interpretation")]
