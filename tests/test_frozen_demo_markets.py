"""Synthetic frozen demo v3: exact pools, diagnostics and owned choices."""

from copy import deepcopy
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
import json
import uuid

from alembic import command
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect, select, text

from el.domain.structures import ExtractedStructure
from el.domain.tables import (
    CandidateSetMember, FitCard, MarketAssessment, MarketChoice,
    MarketDisplaySet, MarketRulesCapture, ThesisAnalysis,
)
from el.marketpool.service import MarketPoolConflict
from el.mcp.auth import Principal
from el.mcp.contracts import Conflict
from el.mcp.v3_tools import McpV3Tools
from el.product.api import ProductApi
from el.product.app import build_app
import el.product.demo_frozen_markets as frozen
from el.product.demo_frozen_markets import (
    AUTHORITY, CURATION_POLICY, ELIGIBILITY_POLICY, EVIDENCE_SCHEMA,
    EVENT_FIXTURE_SCHEMA, FIXTURE_KIND, FIXTURE_VERSIONED_UTC,
    MARKET_FIXTURE_SCHEMA, PAIRS, POOLS, PROVIDER_VERSION,
    FrozenDemoMarketProvider, assessment_evidence, event_association,
    market_structures, public_market, snapshot_id, structure_digest,
)
from el.product.wiring import (
    MULTI_THESIS_FIXTURE,
    ROBINHOOD_UI_REVISED_INPUT,
    STABLECOIN_UI_REVISED_INPUT,
    _add_multi_normalization_fixtures,
    _alembic_config,
    build_services,
    local_actor,
    make_session_factory,
)
from el.sourceinterpretation.jobs import SourceInterpretationWorker

ORIGIN = {"Origin": "http://testserver"}
INPUTS = [STABLECOIN_UI_REVISED_INPUT, ROBINHOOD_UI_REVISED_INPUT]
MARKETS = [POOLS["stablecoin"], POOLS["robinhood"]]


@pytest.fixture()
def demo(tmp_path):
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'frozen.sqlite'}")
    services = build_services(mode="fixture", session_factory=sessions, anonymous_demo=True)
    return services, sessions, TestClient(build_app(services))


def accept_thesis(demo, candidate_index):
    services, sessions, client = demo
    client.get("/api/health")
    queued = client.post("/api/v3/source-interpretation-jobs", headers=ORIGIN,
                         json={"input_text": MULTI_THESIS_FIXTURE,
                               "idempotency_key": str(uuid.uuid4())})
    assert queued.status_code == 202, queued.text
    worker = SourceInterpretationWorker(
        jobs=services.jobs, source_interpretation=services.source_interpretation,
        session_factory=sessions, worker_id="frozen-demo-test",
    )
    assert worker.run_once().status == "succeeded"
    source = client.get(f"/api/v3/source-interpretation-jobs/{queued.json()['job_id']}").json()["interpretation"]
    candidate = source["candidates"][candidate_index]
    choice = client.post(f"/api/v3/source-interpretations/{source['source_interpretation_id']}/choice",
                         headers=ORIGIN, json={"selection_kind": "candidate",
                         "source_thesis_candidate_id": candidate["source_thesis_candidate_id"]})
    assert choice.status_code == 200, choice.text
    initial = client.post(f"/api/v3/source-thesis-candidates/{candidate['source_thesis_candidate_id']}/normalization-attempt",
                          headers=ORIGIN).json()
    assert initial["outcome"] == "clarification"
    revised = client.post(f"/api/v3/normalization-attempts/{initial['normalization_attempt_id']}/revise",
                          headers=ORIGIN, json={"input_text": INPUTS[candidate_index],
                          "expected_input_digest": initial["input_digest"]})
    assert revised.status_code == 200, revised.text
    proposal = revised.json()
    assert proposal["extracted_structure"]["horizon"]["window_end"] == "2027-12-31"
    semantic_payload = {
        key: proposal[key]
        for key in (
            "normalized_claim_summary",
            "extracted_structure",
            "clarifying_question",
            "reasons",
        )
    }
    assert "2028" not in str(semantic_payload)
    assert "by December 31, 2027" in proposal["extracted_structure"]["contractible_version"]
    accepted = client.post(f"/api/v3/normalization-attempts/{proposal['normalization_attempt_id']}/accept",
                           headers=ORIGIN, json={"expected_input_digest": proposal["input_digest"]})
    assert accepted.status_code == 200, accepted.text
    return accepted.json()["thesis_analysis_id"]


def test_compatible_populated_journey_downgrades_to_predecessor(demo):
    _, sessions, client = demo
    thesis_id = accept_thesis(demo, 0)
    response = client.post(
        f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN
    )
    assert response.status_code == 200, response.text
    pool = response.json()
    assert pool["assessed_count"] == pool["displayed_count"] == 3
    client.close()

    engine = sessions.kw["bind"]
    config = _alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "4f7c2e9a6b13")
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "18e9c4a7d2b5")

    with engine.connect() as connection:
        table_names = set(inspect(connection).get_table_names())
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "18e9c4a7d2b5"
        assert "demo_sessions" not in table_names
        assert connection.scalar(text("SELECT count(*) FROM fit_cards")) == 1
        assert connection.scalar(
            text("SELECT count(*) FROM fit_cards WHERE semantic_fit_class IS NULL")
        ) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM market_assessments")
        ) == 3
        assert connection.scalar(
            text(
                "SELECT count(*) FROM market_assessments WHERE pair_class IS NULL"
            )
        ) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM market_display_sets")
        ) == 1
        assert connection.scalar(
            text(
                "SELECT count(*) FROM market_display_sets "
                "WHERE displayed_count > assessed_count"
            )
        ) == 0
        assert not {
            name for name in table_names if name.startswith("_alembic_tmp_")
        }


@pytest.mark.parametrize("candidate_index", [0, 1])
@pytest.mark.parametrize("selection_kind", ["market", "none"])
def test_complete_frozen_workflow_preserves_automatic_card_and_owned_choices(demo, candidate_index, selection_kind):
    services, sessions, client = demo
    thesis_id = accept_thesis(demo, candidate_index)
    response = client.post(f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN)
    assert response.status_code == 200, response.text
    pool = response.json()
    assert pool["assessed_count"] == pool["target_count"] == pool["displayed_count"] == 3
    assert pool["assessment_complete"] is True
    assert pool["system_pool_outcome"] == "no_clean_expression"
    assert pool["retrieval_scope"]["returned_count"] == 3
    assert {card["market_id"] for card in pool["candidate_markets"]} == set(MARKETS[candidate_index])
    assert [card["display_rank"] for card in pool["candidate_markets"]] == [1, 2, 3]
    card = pool["candidate_markets"][0]
    mid = card["market_id"]
    for item in pool["candidate_markets"]:
        item_mid = item["market_id"]
        raw = public_market(item_mid)
        assert item["pair_class"] == PAIRS[item_mid]["pair_class"]
        assert item["authority"] == AUTHORITY
        assert item["fit_confidence"] is None
        assert item["resolution_conditions"] == raw["resolution_rules"]
        evidence = item["frozen_demo_evidence"]
        assert evidence["schema_version"] == EVIDENCE_SCHEMA
        assert evidence["policy_version"] == CURATION_POLICY
        assert evidence["event_association"] == event_association(item_mid, raw)
        assert evidence["condition_id"] == raw["condition_id"]
        assert evidence["fixture_resource"] == PAIRS[item_mid]["resource"]
        assert evidence["fixture_metadata_authority"] == FIXTURE_KIND
        assert evidence["fixture_versioned_utc"] == FIXTURE_VERSIONED_UTC
        assert evidence["eligibility_policy_version"] == ELIGIBILITY_POLICY
        assert evidence["fixture_liquidity_usd"] == raw["liquidity_usd"]
        assert evidence["current_prices_or_final_outcomes_asserted"] is False
        assert evidence["yes_no_summary_basis"] == "project_authored_market_rules"
        assert evidence["child_specific_yes_no_prose"] is True
    with sessions() as session:
        fit = session.get(FitCard, uuid.UUID(pool["fit_card_id"]))
        original = deepcopy(fit.provenance)
        automatic = original["per_market"][mid]
        assert automatic["authority"] != AUTHORITY
        from el.fitgate.m1_subject_only import evaluate_market
        claim = ExtractedStructure.model_validate(session.get(ThesisAnalysis, uuid.UUID(thesis_id)).extracted_structure)
        recomputed = evaluate_market(claim, market_structures()[mid], policy=services.fit._policy)
        assert automatic["checks"] == [check.model_dump(mode="json", include={"check_id", "name", "status", "cap", "hard", "detail"}) for check in recomputed.checks]
        assert card["frozen_demo_evidence"]["automatic_diagnostic"]["authority"] == automatic["authority"]
        assert session.scalar(select(MarketChoice)) is None
        member = session.get(CandidateSetMember, uuid.UUID(card["candidate_set_member_id"]))
        assert member.eligibility_flags["policy_version"] == ELIGIBILITY_POLICY
        assert member.eligibility_flags["liquidity"] == "pass"
    payload = {"selection_kind": selection_kind}
    if selection_kind == "market":
        payload["market_assessment_id"] = card["market_assessment_id"]
    selected = client.post(f"/api/v3/market-pools/{pool['market_display_set_id']}/choice",
                           headers=ORIGIN, json=payload)
    assert selected.status_code == 200, selected.text
    assert selected.json()["selection_kind"] == selection_kind
    assert selected.json()["market_assessment_id"] == (card["market_assessment_id"] if selection_kind == "market" else None)
    assert client.post(f"/api/v3/market-pools/{pool['market_display_set_id']}/choice",
                       headers=ORIGIN, json=payload).json() == selected.json()
    with sessions() as session:
        assert session.get(FitCard, uuid.UUID(pool["fit_card_id"])).provenance == original
    foreign = TestClient(client.app)
    foreign.get("/api/health")
    assert foreign.post(f"/api/v3/market-pools/{pool['market_display_set_id']}/choice",
                        headers=ORIGIN, json=payload).status_code == 404
    client.post("/api/demo/reset", headers=ORIGIN)
    assert client.post(f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN).status_code == 404


def test_resources_exact_rules_clock_and_semantic_binding():
    fixtures = {}
    _add_multi_normalization_fixtures(fixtures)
    provider = FrozenDemoMarketProvider()
    for text, mids in zip(INPUTS, MARKETS):
        structure = fixtures[text].candidate.structure
        for mid in mids:
            market = public_market(mid)
            blob = files("el.product.demo_fixtures").joinpath(
                "markets", PAIRS[mid]["resource"]
            ).read_bytes()
            assert sha256(blob).hexdigest() == PAIRS[mid]["fixture_sha256"]
            assert sha256(market["resolution_rules"].encode()).hexdigest() == PAIRS[mid]["rules_sha256"]
        result = provider.retrieve(structure)
        assert len(result.markets) == 3 and [market.market_id for market in result.markets] == list(mids)
        assert all(market.current_probability is None for market in result.markets)
        assert result.mode == PROVIDER_VERSION
        assert result.query_summary["source"] == "project_authored_synthetic_fixture"
        assert result.query_summary["query_binding"] == structure_digest(structure)
        altered = structure.model_dump(mode="json")
        altered["metric"]["what"] += " under an unsupported condition"
        with pytest.raises(MarketPoolConflict, match="unavailable"):
            provider.retrieve(ExtractedStructure.model_validate(altered))
    with pytest.raises(MarketPoolConflict, match="unavailable"):
        provider.retrieve(None)
    assert "05:00 UTC" in PAIRS["synthetic-stablecoin-total-cap"]["deadline"]
    assert "January 1, 2027, 04:59 UTC" in PAIRS["synthetic-broker-tokenized-asset"]["deadline"]


def test_synthetic_fixture_bytes_event_child_and_snapshot_binding(monkeypatch):
    root = Path(__file__).resolve().parents[1] / "el/product/demo_fixtures/markets"
    expected_resources = {pair["resource"] for pair in PAIRS.values()}
    expected_resources.update(
        pair["event"]["resource"] for pair in PAIRS.values() if "event" in pair
    )
    assert len(expected_resources) == 8
    assert {path.name for path in root.glob("synthetic-*.json")} == expected_resources

    for mid, pair in PAIRS.items():
        blob = (root / pair["resource"]).read_bytes()
        market = public_market(mid)
        assert len(blob) == pair["fixture_body_bytes"]
        assert sha256(blob).hexdigest() == pair["fixture_sha256"]
        assert market["schema_version"] == MARKET_FIXTURE_SCHEMA
        assert market["fixture_kind"] == FIXTURE_KIND
        assert market["market_id"] == mid
        assert market["fixture_versioned_utc"] == FIXTURE_VERSIONED_UTC
        assert market["source_url"].startswith("https://example.invalid/")
        assert set(market) == {
            "schema_version", "fixture_kind", "market_id", "title",
            "resolution_rules", "condition_id", "outcomes", "close_at_utc",
            "liquidity_usd", "source_url", "fixture_versioned_utc",
        }

    bound = {
        "synthetic-stablecoin-unit-cap": True,
        "synthetic-broker-first-tokenized": False,
    }
    for mid, parent_differs in bound.items():
        pair = PAIRS[mid]
        event_binding = pair["event"]
        event_blob = (root / event_binding["resource"]).read_bytes()
        event = json.loads(event_blob)
        market = public_market(mid)
        assert len(event_blob) == event_binding["fixture_body_bytes"]
        assert sha256(event_blob).hexdigest() == event_binding["fixture_sha256"]
        assert event["schema_version"] == EVENT_FIXTURE_SCHEMA
        assert event["fixture_kind"] == FIXTURE_KIND
        assert event["markets"] == [market]
        assert event_association(mid, market)["parent_description_differs"] is parent_differs

    a3_rules = public_market("synthetic-treasury-ledger-payment")["resolution_rules"]
    b2_rules = public_market("synthetic-broker-first-tokenized")["resolution_rules"]
    b3_rules = public_market("synthetic-broker-chain-token")["resolution_rules"]
    assert "primary clause" in a3_rules and "supplemental clause" in a3_rules
    assert "authorized Treasury sub-body or entity" in a3_rules
    assert "Pilots, tests, and proofs of concept do not qualify" in a3_rules
    assert "same ET calendar day" in b2_rules
    assert "earlier purchasability" in b2_rules
    assert "earlier public confirmation" in b2_rules
    for semantic_clause in (
        "both of these checks",
        "operating or voting token",
        "members of the public can exchange",
        "A statement of intent",
        "Currency-pegged assets",
        "joke or community coins",
        "receipts for staked assets",
        "derivative or synthetic claims",
        "representations of shares or ETFs",
        "the result is No",
    ):
        assert semantic_clause in b3_rules

    baseline = snapshot_id("stablecoin")
    monkeypatch.setitem(
        PAIRS["synthetic-stablecoin-unit-cap"]["event"],
        "fixture_sha256",
        "0" * 64,
    )
    assert snapshot_id("stablecoin") != baseline


def test_synthetic_event_binding_rejects_cross_bound_child():
    a2 = public_market("synthetic-stablecoin-unit-cap")
    b2 = public_market("synthetic-broker-first-tokenized")
    with pytest.raises(MarketPoolConflict, match="event child binding"):
        event_association(
            "synthetic-stablecoin-unit-cap",
            {**a2, "condition_id": "cross-bound"},
        )
    for field in ("title", "resolution_rules", "outcomes", "close_at_utc"):
        with pytest.raises(MarketPoolConflict, match="event child binding"):
            event_association(
                "synthetic-broker-first-tokenized",
                {**b2, field: "cross-bound"},
            )


@pytest.mark.parametrize("candidate_index,market_id", [
    (0, "synthetic-stablecoin-total-cap"),
    (0, "synthetic-stablecoin-unit-cap"),
    (0, "synthetic-treasury-ledger-payment"),
    (1, "synthetic-broker-tokenized-asset"),
    (1, "synthetic-broker-first-tokenized"),
    (1, "synthetic-broker-chain-token"),
])
def test_each_displayed_market_is_selectable_without_default(demo, candidate_index, market_id):
    _, sessions, client = demo
    thesis_id = accept_thesis(demo, candidate_index)
    pool = client.post(f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN).json()
    with sessions() as session:
        assert session.scalar(select(MarketChoice)) is None
    card = next(item for item in pool["candidate_markets"] if item["market_id"] == market_id)
    payload = {"selection_kind": "market", "market_assessment_id": card["market_assessment_id"]}
    response = client.post(f"/api/v3/market-pools/{pool['market_display_set_id']}/choice",
                           headers=ORIGIN, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["market_assessment_id"] == card["market_assessment_id"]


def test_one_of_three_assessments_is_incomplete(demo, monkeypatch):
    services, _, client = demo
    thesis_id = accept_thesis(demo, 0)
    proposer = services.fit._structures._proposer
    ordinary = proposer.propose_market_structure
    def partial(**kwargs):
        result = ordinary(**kwargs)
        if kwargs["market_id"] != "synthetic-stablecoin-total-cap":
            return result.model_copy(update={"structure": result.structure.model_copy(update={"market_id": "cross-bound"})})
        return result
    monkeypatch.setattr(proposer, "propose_market_structure", partial)
    pool = client.post(f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN).json()
    assert pool["target_count"] == 3
    assert pool["assessed_count"] == pool["displayed_count"] == 1
    assert pool["assessment_complete"] is False
    assert pool["system_pool_outcome"] == "incomplete"
    selected = client.post(
        f"/api/v3/market-pools/{pool['market_display_set_id']}/choice",
        headers=ORIGIN,
        json={"selection_kind": "market",
              "market_assessment_id": pool["candidate_markets"][0]["market_assessment_id"]},
    )
    assert selected.status_code == 200, selected.text


@pytest.mark.parametrize("surface", ["product", "mcp"])
def test_direct_choice_refuses_tampered_frozen_pool_before_write(demo, surface):
    services, sessions, client = demo
    thesis_id = accept_thesis(demo, 0)
    pool = client.post(f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN).json()
    from el.product.demo_sessions import COOKIE_NAME, DemoSessions
    actor, _ = DemoSessions(sessions).resolve(client.cookies.get(COOKIE_NAME))
    with sessions.begin() as session:
        display = session.get(MarketDisplaySet, uuid.UUID(pool["market_display_set_id"]))
        display.target_count = 2
    display_id = uuid.UUID(pool["market_display_set_id"])
    if surface == "product":
        with pytest.raises(MarketPoolConflict, match="completeness binding"):
            ProductApi(services, actor).choose_market(
                display_id, selection_kind="none", market_assessment_id=None)
    else:
        fields = {
            "user_id": actor.user_id, "client_type": actor.client_type,
            "actor_id": actor.actor_id, "agent_client_id": actor.agent_client_id,
            "rate_limit_tier": "default",
        }
        if "api_client_id" in Principal.__dataclass_fields__:
            fields["api_client_id"] = actor.user_id
        tools = McpV3Tools(
            source_interpretation=services.source_interpretation,
            source_interpretation_jobs=services.source_interpretation_jobs,
            extraction=services.extraction, market_pool=services.market_pool,
            session_factory=sessions,
        )
        with pytest.raises(Conflict):
            tools.choose_market(
                Principal(**fields), market_display_set_id=display_id,
                selection_kind="none", market_assessment_id=None)
    with sessions() as session:
        assert session.scalar(select(MarketChoice)) is None


@pytest.mark.parametrize(
    "legacy_policy",
    ["frozen-demo-curation-v1", "frozen-demo-curation-v2"],
)
def test_persisted_v1_v2_evidence_fails_closed_without_legacy_resource_reads(
    demo, legacy_policy, monkeypatch
):
    services, sessions, client = demo
    thesis_id = accept_thesis(demo, 0)
    pool = client.post(f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN).json()
    card = pool["candidate_markets"][0]
    from el.product.demo_sessions import COOKIE_NAME, DemoSessions
    actor, _ = DemoSessions(sessions).resolve(client.cookies.get(COOKIE_NAME))
    with sessions.begin() as session:
        assessment = session.get(MarketAssessment, uuid.UUID(card["market_assessment_id"]))
        changed = deepcopy(assessment.provenance)
        changed["frozen_demo_evidence"]["policy_version"] = legacy_policy
        changed["frozen_demo_evidence"].pop("schema_version", None)
        assessment.provenance = changed

    original = frozen._resource_bytes
    seen = []
    def synthetic_only(resource):
        seen.append(resource)
        assert resource.startswith("synthetic-")
        return original(resource)
    monkeypatch.setattr(frozen, "_resource_bytes", synthetic_only)

    with pytest.raises(MarketPoolConflict, match="legacy.*unsupported"):
        ProductApi(services, actor)._market_pool_out(
            uuid.UUID(pool["market_display_set_id"])
        )
    assert seen == []


@pytest.mark.parametrize("corruption", ["thesis", "rules", "captures", "misses", "yes", "deadline", "authority", "pair_class", "diagnostic_copy"])
def test_cross_bound_or_corrupt_assessment_cannot_render_or_be_chosen(demo, corruption):
    _, sessions, client = demo
    thesis_id = accept_thesis(demo, 0)
    pool = client.post(f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN).json()
    card = pool["candidate_markets"][0]
    with sessions() as session:
        assessment = session.get(MarketAssessment, uuid.UUID(card["market_assessment_id"]))
        thesis = session.get(ThesisAnalysis, uuid.UUID(thesis_id))
        capture = session.get(MarketRulesCapture, assessment.rules_capture_id)
        fit_card = session.get(FitCard, assessment.fit_card_id)
        if corruption == "thesis":
            altered = deepcopy(thesis.extracted_structure)
            altered["horizon"]["window_end"] = "2028-12-31"
            thesis.extracted_structure = altered
        elif corruption == "rules":
            capture.resolution_rules_text += " Modified rule."
        elif corruption in {"captures", "misses"}:
            setattr(assessment, "what_it_" + corruption, "Unsupported summary")
        elif corruption == "diagnostic_copy":
            altered = deepcopy(assessment.provenance)
            altered["per_market"]["published"] = "direct"
            assessment.provenance = altered
        elif corruption == "authority":
            assessment.authority = "human_reviewed"
        elif corruption == "pair_class":
            assessment.pair_class = "direct"
        else:
            altered = deepcopy(assessment.provenance)
            altered["frozen_demo_evidence"]["yes_condition" if corruption == "yes" else "rule_deadline"] = "Unsupported summary"
            assessment.provenance = altered
        with pytest.raises(MarketPoolConflict, match="mismatch"):
            assessment_evidence(assessment, thesis, capture, fit_card)
        session.commit()
    selected = client.post(f"/api/v3/market-pools/{pool['market_display_set_id']}/choice",
                           headers=ORIGIN, json={"selection_kind": "none"})
    assert selected.status_code in {404, 409}
    with sessions() as session:
        assert session.scalar(select(MarketChoice)) is None


def test_missing_assessment_is_incomplete_and_unsupported_thesis_is_unavailable(demo, monkeypatch):
    services, sessions, client = demo
    thesis_id = accept_thesis(demo, 1)
    # A schema-valid rejection leaves zero assessed candidates, never no-clean.
    proposer = services.fit._structures._proposer
    ordinary = proposer.propose_market_structure
    def rejected(**kwargs):
        result = ordinary(**kwargs)
        return result.model_copy(update={"structure": result.structure.model_copy(update={"market_id": "cross-bound"})})
    monkeypatch.setattr(proposer, "propose_market_structure", rejected)
    pool = client.post(f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN).json()
    assert pool["system_pool_outcome"] == "incomplete"
    assert pool["assessment_complete"] is False
    assert pool["displayed_count"] == pool["assessed_count"] == 0
    assert pool["target_count"] == 3
    with sessions.begin() as session:
        thesis = session.get(ThesisAnalysis, uuid.UUID(thesis_id))
        changed = deepcopy(thesis.extracted_structure)
        changed["metric"]["what"] += " custom changed condition"
        thesis.extracted_structure = changed
    response = client.post(f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN)
    assert response.status_code == 503
    assert response.json()["error"] == "market_pool_unavailable"


def test_product_and_mcp_projection_agree_without_changing_ordinary_wiring(demo):
    services, sessions, client = demo
    thesis_id = accept_thesis(demo, 1)
    pool = client.post(f"/api/v3/theses/{thesis_id}/market-pool", headers=ORIGIN).json()
    from el.product.demo_sessions import COOKIE_NAME, DemoSessions
    actor, _ = DemoSessions(sessions).resolve(client.cookies.get(COOKIE_NAME))
    tools = McpV3Tools(source_interpretation=services.source_interpretation,
                       source_interpretation_jobs=services.source_interpretation_jobs,
                       extraction=services.extraction, market_pool=services.market_pool,
                       session_factory=sessions)
    # Test transport projection using the same already-authorized principal.
    # This does not grant human_ui access to the authenticated MCP server.
    principal_fields = {
        "user_id": actor.user_id, "client_type": actor.client_type,
        "actor_id": actor.actor_id, "agent_client_id": actor.agent_client_id,
        "rate_limit_tier": "default",
    }
    # The public transport adds api_client_id; this projection test does not
    # resolve an API key or enter the authenticated server/rate-limit path.
    if "api_client_id" in Principal.__dataclass_fields__:
        principal_fields["api_client_id"] = actor.user_id
    principal = Principal(**principal_fields)
    mcp = tools._market_pool_out(principal, uuid.UUID(pool["market_display_set_id"]))
    api = ProductApi(services, actor)._market_pool_out(uuid.UUID(pool["market_display_set_id"]))
    assert mcp.candidate_markets[0].model_dump(mode="json") == api.candidate_markets[0].model_dump(mode="json")
    ordinary = build_services(mode="fixture", session_factory=sessions, anonymous_demo=False)
    assert not isinstance(ordinary.retrieval._provider, FrozenDemoMarketProvider)
    assert ordinary.retrieval._policy.min_liquidity_usd == 1000
    assert ordinary.market_pool._assessment_projection is None


def test_contract_and_english_ui_bind_frozen_assessment_separately():
    root = Path(__file__).resolve().parents[1]
    contract = (root / "docs/fitcheck-frozen-market-demo-contract-v3.md").read_text()
    for number in range(1, 13):
        assert f"AC-S{number}:" in contract
    html = (root / "el/product/static/product_console.html").read_text()
    for copy in ["Curated assessment:", "Automatic diagnostic:",
                 "Curated confidence is unavailable.", "Rule deadline", "Resolution source",
                 "View packaged synthetic fixture", "Fixed owner-selected candidate pool",
                 "fully assessed displayed candidate", "enableFrozenDemoMarketStep",
                 "three synthetic contracts", "project-authored fixture version"]:
        assert copy in html
    assert "showDemoMarketPending" not in html
    assert "matches are pending" not in html


def test_custom_clarification_does_not_create_an_accepted_thesis_or_pool(demo):
    services, sessions, client = demo
    client.get("/api/health")
    # Source-independent normalization is retained as a compatibility API;
    # it still cannot invent custom fixture semantics or market evidence.
    initial = client.post("/api/v3/normalization-attempts", headers=ORIGIN,
                          json={"input_text": MULTI_THESIS_FIXTURE.split(" Robinhood")[0]}).json()
    revised = client.post(f"/api/v3/normalization-attempts/{initial['normalization_attempt_id']}/revise",
                          headers=ORIGIN, json={"input_text": STABLECOIN_UI_REVISED_INPUT.replace("$200 billion", "$300 billion"),
                          "expected_input_digest": initial["input_digest"]})
    assert revised.status_code == 503
    assert revised.json()["error"] == "service_unavailable"
    with sessions() as session:
        assert session.scalar(select(ThesisAnalysis)) is None
        assert session.scalar(select(MarketDisplaySet)) is None
