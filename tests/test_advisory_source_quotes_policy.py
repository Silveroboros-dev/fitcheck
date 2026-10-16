"""FC-Q01: exact attributed quotes may carry restricted source vocabulary."""

import hashlib
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from el.domain.enums import ClientType
from el.domain.tables import (
    CandidateSetMember,
    FitCard,
    MarketRulesCapture,
    MarketSnapshot,
    RejectedMarketRow,
    ThesisAnalysis,
)
from el.fitgate.gate import (
    ADVISORY_SOURCE_QUOTES_POLICY_V2 as SUCCESSOR,
    ADVISORY_VOCABULARY_POLICY_V1 as PREDECESSOR,
    merge_advisory,
)
from el.fitgate.policy import evaluate_market
from el.fitgate.m1_subject_only import (
    evaluate_market as evaluate_policy_market,
    stopword_corrected_fit_policy,
)
from el.fitgate.service import FitService
from el.fitgate.token_rules import TOKEN_RULES_SUCCESSOR_VERSION
from el.marketstructure.service import MarketStructureService
from el.mcp.vocab_guard import A7Violation, assert_a7_clean
from el.mcp.tools import McpTools
from el.models.fit_adapter import FixtureFitProposer
from el.models.market_adapter import FixtureMarketStructureProposer
from el.product.api import FitCardOut, ProductApi
from el.retrieval.provider import FixtureMarketProvider
from el.retrieval.service import RetrievalService
from tests.test_fit_service import (
    GOLDENS, SNAPSHOT_PATH, _advisory, _lmsys_claim, _session_factory,
)

MARKET_ID = "mkt_gemini_lmsys_1"
CLAIM_QUOTE = "people buy widgets"
RULES_QUOTE = "people sell widgets"


def _quoted_advisory(**updates):
    return _advisory(
        MARKET_ID, claim_evidence=CLAIM_QUOTE, market_evidence=RULES_QUOTE
    ).model_copy(update=updates)


def _merge(advisory, *, deterministic=None, **updates):
    return merge_advisory(
        deterministic if deterministic is not None else evaluate_market(_lmsys_claim(), GOLDENS[MARKET_ID]),
        advisory,
        market_id=MARKET_ID,
        **{
            "vocabulary_policy_version": SUCCESSOR,
            "claim_text": CLAIM_QUOTE,
            "rules_text": RULES_QUOTE,
            **updates,
        },
    )


def test_exact_source_quotes_require_explicit_successor_and_source_binding():
    advisory = _quoted_advisory()
    assert not _merge(advisory, vocabulary_policy_version=PREDECESSOR).accepted
    assert _merge(advisory).accepted
    for missing in ("claim_text", "rules_text"):
        merge = _merge(advisory, **{missing: None})
        assert not merge.accepted
        assert "require bound" in merge.rejected_reasons[0]


@pytest.mark.parametrize("field", ["what_it_captures", "what_it_misses", "falsifier", "bridge_assumptions"])
def test_exact_quotes_do_not_exempt_any_generated_advice_field(field):
    advice = "you should buy this market"
    value = [advice] if field == "bridge_assumptions" else advice
    merge = _merge(_quoted_advisory(**{field: value}))
    assert not merge.accepted
    assert any("restricted vocabulary" in reason for reason in merge.rejected_reasons)


@pytest.mark.parametrize(
    ("quote", "source", "reason"),
    [
        ("people buy inventions", CLAIM_QUOTE, "not a quote"),
        ("buy", CLAIM_QUOTE, "too thin"),
        ("People Buy Widgets", CLAIM_QUOTE, "not a quote"),
        ("people  buy widgets", CLAIM_QUOTE, "not a quote"),
    ],
)
def test_successor_does_not_admit_invented_thin_or_normalized_quotes(quote, source, reason):
    advisory = _quoted_advisory()
    for verdict in advisory.condition_verdicts:
        verdict.claim_evidence = quote
    merge = _merge(advisory, claim_text=source)
    assert not merge.accepted
    assert any(reason in item for item in merge.rejected_reasons)


def test_successor_quote_content_floor_uses_selected_token_rules():
    advisory = _quoted_advisory()
    for verdict in advisory.condition_verdicts:
        verdict.claim_evidence = "this widget"
    assert _merge(advisory, claim_text="this widget").accepted
    policy = stopword_corrected_fit_policy()
    merge = _merge(
        advisory, claim_text="this widget", token_rules_version=TOKEN_RULES_SUCCESSOR_VERSION,
        policy=policy, deterministic=evaluate_policy_market(_lmsys_claim(), GOLDENS[MARKET_ID], policy),
    )
    assert not merge.accepted
    assert any("too thin" in reason for reason in merge.rejected_reasons)


def test_source_quote_merge_derives_omitted_token_version_from_verdict():
    advisory = _quoted_advisory()
    for verdict in advisory.condition_verdicts:
        verdict.claim_evidence = "this always"
    policy = stopword_corrected_fit_policy()
    deterministic = evaluate_policy_market(_lmsys_claim(), GOLDENS[MARKET_ID], policy)
    for arguments in ({}, {"policy": policy}):
        merge = _merge(advisory, deterministic=deterministic, claim_text="this always", **arguments)
        assert not merge.accepted
        assert any("too thin" in reason for reason in merge.rejected_reasons)


def test_source_quote_merge_rejects_explicit_cross_token_argument():
    policy = stopword_corrected_fit_policy()
    deterministic = evaluate_policy_market(_lmsys_claim(), GOLDENS[MARKET_ID], policy)
    merge = _merge(_quoted_advisory(), deterministic=deterministic, policy=policy, token_rules_version="tok-v1")
    assert not merge.accepted
    assert any("token rules differ" in reason for reason in merge.rejected_reasons)


def test_verified_source_quote_can_retain_restricted_phrases_as_source():
    quote = "you should buy widgets"
    advisory = _quoted_advisory()
    for verdict in advisory.condition_verdicts:
        verdict.claim_evidence = quote
    assert _merge(advisory, claim_text="The retained source says " + quote).accepted


@pytest.mark.parametrize(
    "market_sources",
    [
        (f"Contract title: {RULES_QUOTE}", "unrelated resolution rule"),
        ("unrelated contract title", f"Resolution rule: {RULES_QUOTE}"),
    ],
)
def test_exact_market_quote_may_come_from_either_retained_field(market_sources):
    assert _merge(
        _quoted_advisory(),
        rules_text=None,
        market_source_texts=market_sources,
    ).accepted


def test_exact_market_quote_cannot_splice_across_retained_fields():
    splice = "people should\nbuy widgets"
    advisory = _quoted_advisory()
    for verdict in advisory.condition_verdicts:
        verdict.market_evidence = splice
    merge = _merge(
        advisory,
        rules_text=None,
        market_source_texts=("people should", "buy widgets"),
    )
    assert not merge.accepted
    assert any(
        "market_evidence is not a quote" in reason
        for reason in merge.rejected_reasons
    )


def _classify(
    version=SUCCESSOR,
    *,
    raw_quote=CLAIM_QUOTE,
    structure_quote=None,
    veto=False,
    mutate_capture_field=None,
):
    sessions = _session_factory()
    claim = _lmsys_claim()
    if structure_quote:
        claim = claim.model_copy(update={"claim_summary": claim.claim_summary + " " + structure_quote})
    with sessions() as session:
        analysis = ThesisAnalysis(
            input_text=_lmsys_claim().claim_summary + " " + raw_quote,
            extracted_structure=claim.model_dump(mode="json"),
            normalized_claim_summary=claim.claim_summary,
            client_type="human_ui",
        )
        session.add(analysis)
        session.commit()
        analysis_id = analysis.id
    frozen = FixtureMarketProvider.from_path(SNAPSHOT_PATH).retrieve()
    market = next(market for market in frozen.markets if market.market_id == MARKET_ID)
    provider = FixtureMarketProvider(
        snapshot_id=frozen.snapshot_id,
        as_of_ts=frozen.as_of_ts,
        markets=[market.model_copy(update={"resolution_rules": market.resolution_rules + " " + RULES_QUOTE})],
    )
    retrieval = RetrievalService(provider, sessions).retrieve_candidates(analysis_id)
    if mutate_capture_field:
        with sessions() as session:
            capture = session.scalars(
                select(MarketRulesCapture).where(
                    MarketRulesCapture.market_id == MARKET_ID,
                    MarketRulesCapture.snapshot_id == frozen.snapshot_id,
                )
            ).one()
            setattr(
                capture,
                mutate_capture_field,
                getattr(capture, mutate_capture_field) + " altered retained bytes",
            )
            session.commit()
    advisory = _quoted_advisory()
    if veto:
        advisory.condition_verdicts[1].status = "fail"
    service = FitService(
        MarketStructureService(FixtureMarketStructureProposer(GOLDENS), sessions, top_n=1),
        sessions,
        advisory=FixtureFitProposer({MARKET_ID: advisory}),
        advisory_vocabulary_policy=version,
    )
    return service.classify_fit(analysis_id, retrieval.candidate_set_id), sessions


@pytest.mark.parametrize(
    "field", ["contract_terms_text", "resolution_rules_text"]
)
def test_service_rejects_stale_capture_hash_before_advisory_can_run(field):
    outcome, sessions = _classify(veto=True, mutate_capture_field=field)
    assert outcome.fit_class.value == "direct"
    assert outcome.authority == "deterministic_fallback"
    assert outcome.advisory_calls == 0
    assert outcome.advisory_rejected == 1
    with sessions() as session:
        metadata = session.get(FitCard, outcome.fit_card_id).provenance[
            "per_market"
        ][MARKET_ID]["advisory"]
        assert metadata["status"] == "fallback"
        assert metadata["calls"] == 0
        assert metadata["rejected_reasons"] == [
            f"rules capture {field} hash mismatch"
        ]


def test_service_successor_quote_and_predecessor_fallback_are_observable():
    accepted, accepted_sessions = _classify()
    assert accepted.fit_class.value == "direct"
    assert accepted.advisory_calls == 1
    assert accepted.advisory_rejected == accepted.advisory_fallbacks == 0
    assert accepted.fit_confidence == 0.85
    with accepted_sessions() as session:
        metadata = session.get(FitCard, accepted.fit_card_id).provenance["per_market"][MARKET_ID]["advisory"]
        assert metadata["vocabulary_policy_version"] == SUCCESSOR
        assert metadata["source_binding"]["token_rules_version"] == "tok-v1"

    fallback, sessions = _classify(PREDECESSOR)
    assert fallback.fit_class.value == "direct"
    assert fallback.advisory_calls == fallback.advisory_rejected == 2
    assert fallback.advisory_fallbacks == 1
    assert fallback.authority == "deterministic_fallback"
    assert fallback.fit_confidence is None
    with sessions() as session:
        metadata = session.get(FitCard, fallback.fit_card_id).provenance["per_market"][MARKET_ID]["advisory"]
        assert metadata["vocabulary_policy_version"] == PREDECESSOR
        assert len(metadata["rejected_reasons"]) == 2
        assert all("restricted vocabulary" in reason for reason in metadata["rejected_reasons"])


def test_service_successor_never_uses_invented_extracted_structure_as_quote_source():
    outcome, sessions = _classify(raw_quote="raw source stays neutral", structure_quote=CLAIM_QUOTE)
    assert outcome.advisory_fallbacks == 1
    assert outcome.advisory_calls == 2
    with sessions() as session:
        metadata = session.get(FitCard, outcome.fit_card_id).provenance["per_market"][MARKET_ID]["advisory"]
        assert metadata["status"] == "fallback"
        assert all("claim_evidence is not a quote" in reason for reason in metadata["rejected_reasons"])


def _full_card_model(outcome, sessions, *, failure_quote=CLAIM_QUOTE, policy=SUCCESSOR):
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        provenance = deepcopy(card.provenance)
        advisory = provenance["per_market"][MARKET_ID]["advisory"]
        advisory["vocabulary_policy_version"] = policy
        # R03 stores precisely these already-validated failure fields. The
        # boundary revalidates them instead of trusting declarations.
        advisory["failures"] = [{
            "condition": "same_metric", "status": "fail", "cap": "weak_proxy", "hard": True,
            "claim_evidence": failure_quote, "market_evidence": RULES_QUOTE,
        }]
        card.provenance = provenance
        session.commit()
        return FitCardOut.model_construct(
            fit_card_id=card.id,
            thesis_analysis_id=card.thesis_analysis_id,
            provenance=provenance,
            what_it_captures="Same source-defined event.",
            what_it_misses="Advisory veto: same_metric failed (cap weak_proxy)",
        )


def test_full_card_boundary_allows_only_reverified_successor_evidence_leaves():
    outcome, sessions = _classify()
    model = _full_card_model(outcome, sessions)
    api = SimpleNamespace(_sessions=sessions)
    assert ProductApi._guard(api, model) is model
    assert_a7_clean({"what_it_misses": model.what_it_misses})
    with pytest.raises(A7Violation):
        ProductApi._guard(api, model.model_copy(update={"what_it_misses": "you should buy"}))

    for quote, policy in (("people buy inventions", SUCCESSOR), (CLAIM_QUOTE, PREDECESSOR)):
        model = _full_card_model(outcome, sessions, failure_quote=quote, policy=policy)
        with pytest.raises(A7Violation):
            ProductApi._guard(api, model)


def test_successor_veto_survives_service_full_card_and_trimmed_mcp_projection():
    outcome, sessions = _classify(veto=True)
    assert outcome.fit_class.value == "weak_proxy"
    assert outcome.authority == "deterministic_with_advisory_veto"
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        metadata = card.provenance["per_market"][MARKET_ID]["advisory"]
        assert metadata["failures"][0]["claim_evidence"] == CLAIM_QUOTE
        assert metadata["evidence_validation"]["claim_corpus"] == "input_text"
        assert metadata["source_binding"]["thesis_analysis_id"] == str(
            card.thesis_analysis_id
        )
        assert metadata["source_binding"]["candidate_set_id"] == str(
            card.candidate_set_id
        )
        assert metadata["source_binding"]["market_id"] == MARKET_ID
        assert metadata["source_binding"]["rules_capture_id"]
        assert metadata["source_binding"]["candidate_set_member_id"]
        rejected = session.scalars(select(RejectedMarketRow)).one()
        assert "same_metric" in rejected.reason
        full_card = FitCardOut.model_construct(
            fit_card_id=card.id, thesis_analysis_id=card.thesis_analysis_id,
            provenance=card.provenance, what_it_captures=card.what_it_captures,
            what_it_misses=card.what_it_misses,
        )
    assert ProductApi._guard(SimpleNamespace(_sessions=sessions), full_card) is full_card
    tools = McpTools(
        extraction=None, retrieval=None, fit=None, draft=None, ledger=None,
        session_factory=sessions,
    )
    mcp = tools._fit_result(
        outcome, SimpleNamespace(client_type=ClientType.HUMAN_UI),
        mcp_preview=True, current_odds=None, odds_side=None,
    )
    assert tools._guard(mcp) is mcp
    assert "per_market" not in mcp.provenance
    assert "same_metric" in mcp.what_it_misses


def test_full_card_source_declarations_cannot_replace_source_binding():
    outcome, sessions = _classify(veto=True)
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        provenance = deepcopy(card.provenance)
        provenance["per_market"][MARKET_ID]["advisory"]["source_binding"]["claim_input_sha256"] = "0" * 64
        card.provenance = provenance
        session.commit()
        model = FitCardOut.model_construct(
            fit_card_id=card.id, thesis_analysis_id=card.thesis_analysis_id,
            provenance=provenance, what_it_misses=card.what_it_misses,
        )
    with pytest.raises(A7Violation):
        ProductApi._guard(SimpleNamespace(_sessions=sessions), model)


def test_unknown_vocabulary_policy_is_a_configuration_error():
    with pytest.raises(ValueError, match="unsupported advisory vocabulary"):
        _merge(_quoted_advisory(), vocabulary_policy_version="unregistered-policy")


@pytest.mark.parametrize("field", ["contract_terms_text", "resolution_rules_text"])
def test_full_card_rejects_capture_bytes_drift_with_unchanged_hash_metadata(field):
    outcome, sessions = _classify(veto=True)
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        metadata = card.provenance["per_market"][MARKET_ID]["advisory"]
        capture = session.scalars(select(MarketRulesCapture).where(
            MarketRulesCapture.market_id == MARKET_ID,
            MarketRulesCapture.snapshot_id == metadata["source_binding"]["market_snapshot_id"],
        )).one()
        # Quotes remain literal substrings, but these are no longer the bound
        # frozen source bytes. Hash declarations alone cannot exempt evidence.
        setattr(capture, field, getattr(capture, field) + " altered retained capture")
        session.commit()
        model = FitCardOut.model_construct(
            fit_card_id=card.id, thesis_analysis_id=card.thesis_analysis_id,
            provenance=card.provenance, what_it_misses=card.what_it_misses,
        )
    with pytest.raises(A7Violation):
        ProductApi._guard(SimpleNamespace(_sessions=sessions), model)


def test_full_card_rejects_market_quote_spliced_across_source_fields():
    outcome, sessions = _classify(veto=True)
    splice = "people should\nbuy widgets"
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        provenance = deepcopy(card.provenance)
        metadata = provenance["per_market"][MARKET_ID]["advisory"]
        binding = metadata["source_binding"]
        capture = session.get(
            MarketRulesCapture, uuid.UUID(binding["rules_capture_id"])
        )
        capture.contract_terms_text = "The retained title says people should"
        capture.resolution_rules_text = "buy widgets under the retained rule"
        capture.contract_terms_hash = hashlib.sha256(
            capture.contract_terms_text.encode()
        ).hexdigest()
        capture.resolution_rules_hash = hashlib.sha256(
            capture.resolution_rules_text.encode()
        ).hexdigest()
        binding["contract_terms_hash"] = capture.contract_terms_hash
        binding["resolution_rules_hash"] = capture.resolution_rules_hash
        metadata["failures"][0]["market_evidence"] = splice
        card.provenance = provenance
        session.commit()
        model = FitCardOut.model_construct(
            fit_card_id=card.id,
            thesis_analysis_id=card.thesis_analysis_id,
            provenance=provenance,
            what_it_misses=card.what_it_misses,
        )
    with pytest.raises(A7Violation):
        ProductApi._guard(SimpleNamespace(_sessions=sessions), model)


def test_full_card_rejects_quote_token_binding_that_differs_from_fit_policy():
    outcome, sessions = _classify(veto=True)
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        provenance = deepcopy(card.provenance)
        per_market = provenance["per_market"][MARKET_ID]
        assert per_market["token_rules_version"] == "tok-v1"
        per_market["advisory"]["source_binding"]["token_rules_version"] = TOKEN_RULES_SUCCESSOR_VERSION
        card.provenance = provenance
        session.commit()
        model = FitCardOut.model_construct(
            fit_card_id=card.id, thesis_analysis_id=card.thesis_analysis_id,
            provenance=provenance, what_it_misses=card.what_it_misses,
        )
    with pytest.raises(A7Violation):
        ProductApi._guard(SimpleNamespace(_sessions=sessions), model)


def test_full_card_rejects_valid_capture_from_foreign_snapshot():
    outcome, sessions = _classify(veto=True)
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        provenance = deepcopy(card.provenance)
        binding = provenance["per_market"][MARKET_ID]["advisory"][
            "source_binding"
        ]
        original = session.get(
            MarketRulesCapture, uuid.UUID(binding["rules_capture_id"])
        )
        snapshot = session.get(MarketSnapshot, original.snapshot_id)
        foreign_snapshot = MarketSnapshot(
            id="foreign-source-snapshot",
            venue_id=snapshot.venue_id,
            as_of_ts=snapshot.as_of_ts,
            retrieval_id="foreign-source-retrieval",
        )
        foreign_capture = MarketRulesCapture(
            market_id=original.market_id,
            snapshot_id=foreign_snapshot.id,
            contract_terms_text=original.contract_terms_text,
            resolution_rules_text=original.resolution_rules_text,
            contract_terms_hash=original.contract_terms_hash,
            resolution_rules_hash=original.resolution_rules_hash,
        )
        session.add_all([foreign_snapshot, foreign_capture])
        session.flush()
        binding["market_snapshot_id"] = foreign_snapshot.id
        binding["rules_capture_id"] = str(foreign_capture.id)
        card.provenance = provenance
        session.commit()
        model = FitCardOut.model_construct(
            fit_card_id=card.id,
            thesis_analysis_id=card.thesis_analysis_id,
            provenance=provenance,
            what_it_misses=card.what_it_misses,
        )
    with pytest.raises(A7Violation):
        ProductApi._guard(SimpleNamespace(_sessions=sessions), model)


def test_full_card_rejects_member_from_another_market():
    outcome, sessions = _classify(veto=True)
    with sessions() as session:
        card = session.get(FitCard, outcome.fit_card_id)
        foreign_member = CandidateSetMember(
            candidate_set_id=card.candidate_set_id,
            market_id="foreign-market",
            rank=2,
            retrieval_score=0.1,
            current_probability=None,
            eligibility_flags={},
            excluded_reason=None,
        )
        session.add(foreign_member)
        session.flush()
        provenance = deepcopy(card.provenance)
        provenance["per_market"][MARKET_ID]["advisory"]["source_binding"][
            "candidate_set_member_id"
        ] = str(foreign_member.id)
        card.provenance = provenance
        session.commit()
        model = FitCardOut.model_construct(
            fit_card_id=card.id,
            thesis_analysis_id=card.thesis_analysis_id,
            provenance=provenance,
            what_it_misses=card.what_it_misses,
        )
    with pytest.raises(A7Violation):
        ProductApi._guard(SimpleNamespace(_sessions=sessions), model)
