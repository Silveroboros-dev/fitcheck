"""Offline public reference checks for the v3.1 human-gated journey."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from el.domain.enums import ClientType
from el.mcp.contracts import NotFound
from el.product.api import ProductApi
from el.product.app import build_app
from el.product.wiring import (
    HARBOR_REVISED_INPUT,
    HARBOR_UI_REVISED_INPUT,
    MULTI_THESIS_FIXTURE,
    HumanActor,
    build_services,
    local_actor,
    make_session_factory,
)
from el.sourceinterpretation.service import SourceCandidateChoiceConflict


def _api(tmp_path) -> ProductApi:
    sessions = make_session_factory(f"sqlite+pysqlite:///{tmp_path / 'reference.db'}")
    services = build_services(mode="fixture", session_factory=sessions)
    return ProductApi(services, local_actor(sessions))


@pytest.mark.parametrize(
    "revised_input",
    [HARBOR_REVISED_INPUT, HARBOR_UI_REVISED_INPUT],
    ids=["legacy-double-newline", "current-ui-single-space"],
)
def test_public_fixture_api_completes_source_to_market_none(tmp_path, revised_input):
    api = _api(tmp_path)
    client = TestClient(build_app(api))

    interpreted = client.post(
        "/api/v3/source-interpretations", json={"input_text": MULTI_THESIS_FIXTURE}
    )
    assert interpreted.status_code == 200
    source = interpreted.json()
    assert source["outcome"] == "candidates"
    assert len(source["candidates"]) == 2
    first = source["candidates"][0]

    selected = client.post(
        f"/api/v3/source-interpretations/{source['source_interpretation_id']}/choice",
        json={
            "selection_kind": "candidate",
            "source_thesis_candidate_id": first["source_thesis_candidate_id"],
        },
    )
    assert selected.status_code == 200
    assert selected.json()["selection_kind"] == "candidate"

    initial = client.post(
        f"/api/v3/source-thesis-candidates/{first['source_thesis_candidate_id']}/normalization-attempt"
    )
    assert initial.status_code == 200
    assert initial.json()["outcome"] == "clarification"

    revised = client.post(
        f"/api/v3/normalization-attempts/{initial.json()['normalization_attempt_id']}/revise",
        json={
            "expected_input_digest": initial.json()["input_digest"],
            "input_text": revised_input,
        },
    )
    assert revised.status_code == 200
    assert revised.json()["outcome"] == "candidate"

    accepted = client.post(
        f"/api/v3/normalization-attempts/{revised.json()['normalization_attempt_id']}/accept",
        json={"expected_input_digest": revised.json()["input_digest"]},
    )
    assert accepted.status_code == 200
    thesis_id = accepted.json()["thesis_analysis_id"]

    restored = client.get(f"/api/v3/theses/{thesis_id}/accepted-state")
    assert restored.status_code == 200
    saved = restored.json()
    assert saved["acceptance_origin"] == "human_ui"
    assert saved["normalization_decision_id"] == accepted.json()[
        "normalization_decision_id"
    ]
    assert saved["restored_source"]["original_source_text"] == MULTI_THESIS_FIXTURE
    assert saved["restored_source"]["selected_source_quote"] == first[
        "selected_source_quote"
    ]
    assert saved["restored_source"]["accepted_normalization_input"] == revised_input
    assert client.get(f"/api/v3/theses/{thesis_id}/accepted-state").json() == saved

    pool = client.post(f"/api/v3/theses/{thesis_id}/market-pool")
    assert pool.status_code == 200
    assert pool.json()["displayed_count"] == 3
    assert pool.json()["assessment_complete"] is True
    assert pool.json()["accepted_thesis_summary"] == saved["accepted_thesis_summary"]
    assert pool.json()["retrieval_scope"]["scope_kind"] == "frozen_snapshot"
    for card in pool.json()["candidate_markets"]:
        assert card["what_it_captures"].startswith("This contract tests ")
        assert "Your accepted thesis:" in card["what_it_misses"]
        assert card["resolution_conditions"]
    announcement = next(
        card
        for card in pool.json()["candidate_markets"]
        if card["market_id"] == "mkt_anthropic_ipo"
    )
    assert "An announcement does not establish" in announcement["what_it_misses"]

    choice = client.post(
        f"/api/v3/market-pools/{pool.json()['market_display_set_id']}/choice",
        json={"selection_kind": "none"},
    )
    assert choice.status_code == 200
    assert choice.json()["selection_kind"] == "none"


def test_source_choice_is_owner_bound_and_none_never_starts_normalization(tmp_path):
    api = _api(tmp_path)
    source = api.interpret_source(MULTI_THESIS_FIXTURE)
    candidate = source.candidates[0]
    foreign_actor = HumanActor(
        user_id=uuid.uuid4(),
        client_type=ClientType.HUMAN_UI,
        actor_id="user:foreign",
        agent_client_id="human_ui:foreign",
    )
    foreign = ProductApi(api._s, foreign_actor)
    try:
        foreign.choose_source_candidate(
            source.source_interpretation_id,
            selection_kind="candidate",
            source_thesis_candidate_id=candidate.source_thesis_candidate_id,
        )
    except NotFound:
        pass
    else:  # pragma: no cover - the assertion names the authority boundary
        raise AssertionError("foreign human actor reached a source interpretation")

    none = api.choose_source_candidate(
        source.source_interpretation_id,
        selection_kind="none",
        source_thesis_candidate_id=None,
    )
    assert none.selection_kind == "none"
    try:
        api.propose_selected_normalization(candidate.source_thesis_candidate_id)
    except SourceCandidateChoiceConflict:
        pass
    else:  # pragma: no cover - a candidate choice must gate normalization
        raise AssertionError("unselected source candidate started normalization")


def test_accepted_state_does_not_disclose_another_actors_source(tmp_path):
    api = _api(tmp_path)
    source = api.interpret_source(MULTI_THESIS_FIXTURE)
    candidate = source.candidates[0]
    api.choose_source_candidate(
        source.source_interpretation_id,
        selection_kind="candidate",
        source_thesis_candidate_id=candidate.source_thesis_candidate_id,
    )
    first = api.propose_selected_normalization(candidate.source_thesis_candidate_id)
    revised = api.revise_normalization(
        first.normalization_attempt_id,
        expected_input_digest=first.input_digest,
        input_text=HARBOR_REVISED_INPUT,
    )
    accepted = api.accept_normalization(
        revised.normalization_attempt_id,
        expected_input_digest=revised.input_digest,
    )
    foreign_actor = HumanActor(
        user_id=uuid.uuid4(),
        client_type=ClientType.HUMAN_UI,
        actor_id="user:foreign",
        agent_client_id="human_ui:foreign",
    )
    foreign = ProductApi(api._s, foreign_actor)
    with pytest.raises(NotFound):
        foreign.get_accepted_thesis_state(accepted.thesis_analysis_id)
