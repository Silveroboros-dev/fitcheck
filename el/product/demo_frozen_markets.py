"""Two exact accepted theses, each with three synthetic market proposals.

This is a project-authored offline proposal projection, never a human
adjudication or replacement for the ordinary FitCard. No provider, model,
network, price, or outcome reads occur.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
from importlib.resources import files
import json
import math
from urllib.parse import urlsplit

from el.domain.structures import ExtractedStructure, MarketStructure
from el.marketpool.service import MarketPoolConflict, pair_class
from el.retrieval.provider import CandidateMarketRecord, MarketRetrievalResult

CURATION_POLICY = "frozen-demo-curation-v3"
ELIGIBILITY_POLICY = "frozen-demo-six-market-eligibility-v3"
EVIDENCE_SCHEMA = "fitcheck.frozen_demo_evidence.v3"
PROVIDER_VERSION = "frozen-demo-six-market-v3"
MARKET_FIXTURE_SCHEMA = "fitcheck.synthetic_market_fixture.v1"
EVENT_FIXTURE_SCHEMA = "fitcheck.synthetic_event_fixture.v1"
FIXTURE_KIND = "project_authored_synthetic"
FIXTURE_VERSIONED_UTC = "2026-10-15T00:00:00Z"
AUTHORITY = "offline_demo_proposal"
UNSUPPORTED_CURATION_POLICIES = frozenset({
    "frozen-demo-curation-v1",
    "frozen-demo-curation-v2",
})

_MARKET_FIELDS = frozenset({
    "schema_version", "fixture_kind", "market_id", "title",
    "resolution_rules", "condition_id", "outcomes", "close_at_utc",
    "liquidity_usd", "source_url", "fixture_versioned_utc",
})
_EVENT_FIELDS = frozenset({
    "schema_version", "fixture_kind", "event_id", "title", "description",
    "source_url", "fixture_versioned_utc", "markets",
})

POOLS = {
    "stablecoin": (
        "synthetic-stablecoin-total-cap",
        "synthetic-stablecoin-unit-cap",
        "synthetic-treasury-ledger-payment",
    ),
    "robinhood": (
        "synthetic-broker-tokenized-asset",
        "synthetic-broker-first-tokenized",
        "synthetic-broker-chain-token",
    ),
}

PAIRS = {
    "synthetic-stablecoin-total-cap": {
        "pool": "stablecoin", "pair_class": "weak_proxy",
        "rule_date": date(2026, 12, 31),
        "thesis_digest": "e823e9a4dc73c55b3c1c93e3d3b9fcd84b0607248a946dcadab6e726e4474851",
        "resource": "synthetic-market-stablecoin-total-cap.json",
        "fixture_sha256": "e3263c4832559ef09ca18f2ef18212d29eb0200f7814790c2117c81f500c0a59",
        "fixture_body_bytes": 881,
        "rules_sha256": "79d2a3fddc3a14ef3993fb4477fa5105eb53cc70e1df1384635e892871c116a2",
        "condition_id": "synthetic-condition-stablecoin-total-cap",
        "close_at_utc": "2027-01-01T05:00:00Z", "liquidity_usd": 2500.0,
        "source_url": "https://example.invalid/fitcheck/markets/synthetic-stablecoin-total-cap",
        "yes": "Total stablecoin market capitalization reaches at least $500 billion on a finalized daily point by December 31, 2026.",
        "no": "The stated capitalization condition is not met by the deadline.",
        "deadline": "December 31, 2026; the synthetic rules use a 05:00 UTC close marker.",
        "source": "The project-authored synthetic daily total-stablecoin capitalization dataset.",
        "captures": "Growth in total stablecoin market capitalization to at least $500 billion by December 31, 2026.",
        "misses": "Market capitalization does not measure stablecoin issuers' holdings of U.S. Treasury securities or establish the $200 billion target through public issuer attestations. The market deadline is one year earlier than the accepted thesis deadline of December 31, 2027.",
        "metric": "total stablecoin market capitalization", "subject": "stablecoins",
        "stage": "measured", "threshold": "at least $500 billion",
        "timezone": "unspecified",
    },
    "synthetic-stablecoin-unit-cap": {
        "pool": "stablecoin", "pair_class": "weak_proxy",
        "rule_date": date(2026, 12, 31),
        "thesis_digest": "e823e9a4dc73c55b3c1c93e3d3b9fcd84b0607248a946dcadab6e726e4474851",
        "resource": "synthetic-market-stablecoin-unit-cap.json",
        "fixture_sha256": "2b814db381374c245e20db42d5aee398e59de6c35bc93fa6d1fc3cda1549fc9e",
        "fixture_body_bytes": 854,
        "rules_sha256": "b6b0e92b52b6c02c2d6c99034550cdd4a4d911619b5581cfd29cdd5ec3313016",
        "condition_id": "synthetic-condition-stablecoin-unit-cap",
        "close_at_utc": "2027-01-01T05:00:00Z", "liquidity_usd": 1800.0,
        "source_url": "https://example.invalid/fitcheck/markets/synthetic-stablecoin-unit-cap",
        "event": {
            "event_id": "synthetic-event-stablecoin-cap",
            "resource": "synthetic-event-stablecoin-cap.json",
            "fixture_sha256": "971d2edf2fcc5af2986f3f134803c3a586002c02ecbcf80cc83807b841a1127e",
            "fixture_body_bytes": 1429,
            "source_url": "https://example.invalid/fitcheck/events/synthetic-event-stablecoin-cap",
        },
        "yes": "USDT market capitalization reaches at least $200 billion on a finalized daily point by December 31, 2026.",
        "no": "USDT market capitalization does not reach $200 billion by the stated deadline.",
        "deadline": "December 31, 2026; the synthetic rules use a 05:00 UTC close marker.",
        "source": "The project-authored synthetic daily USDT capitalization dataset.",
        "captures": "USDT market capitalization reaching $200 billion by December 31, 2026.",
        "misses": "USDT market capitalization does not establish that stablecoin issuers collectively hold at least $200 billion in U.S. Treasury securities, as publicly attested. The market ends in 2026, one year before the accepted thesis deadline.",
        "metric": "USDT market capitalization", "subject": "USDT",
        "stage": "measured", "threshold": "at least $200 billion",
        "timezone": "unspecified",
    },
    "synthetic-treasury-ledger-payment": {
        "pool": "stablecoin", "pair_class": "not_an_expression",
        "rule_date": date(2026, 12, 31),
        "thesis_digest": "e823e9a4dc73c55b3c1c93e3d3b9fcd84b0607248a946dcadab6e726e4474851",
        "resource": "synthetic-market-treasury-ledger-payment.json",
        "fixture_sha256": "e0a7c05bb7e448defae5209c0d28a7b99bf205602288016e018271a346ed6cf2",
        "fixture_body_bytes": 1185,
        "rules_sha256": "d1fca845d6a3065fb38ea9e11bd17ec5a948f106470c5269819264e6f28701b0",
        "condition_id": "synthetic-condition-treasury-ledger-payment",
        "close_at_utc": "2027-01-01T04:59:00Z", "liquidity_usd": 900.0,
        "source_url": "https://example.invalid/fitcheck/markets/synthetic-treasury-ledger-payment",
        "yes": "The primary clause requires a qualifying U.S. Treasury transaction on a public distributed ledger by December 31, 2026, 23:59 ET. A supplemental clause instead requires public announcement of an ordinary government payment by that deadline regardless of when the transaction occurred. The Treasury and authorized Treasury sub-bodies or entities are in scope. Pilots, tests, and proofs of concept do not qualify; both clauses remain explicit.",
        "no": "No transaction and announcement satisfying the retained synthetic clauses qualify by the deadline.",
        "deadline": "Primary clause: transaction by December 31, 2026, 23:59 ET. Supplemental clause: public announcement by that time regardless of transaction time. Both phrasings are retained without selecting one.",
        "source": "Project-authored synthetic official-government information.",
        "captures": "A publicly announced ordinary U.S. Treasury payment using a public distributed ledger under the synthetic rules.",
        "misses": "A Treasury payment does not test stablecoin issuers' U.S. Treasury holdings or a collective $200 billion publicly attested threshold. Its 2026 timing also differs from the accepted 2027 thesis.",
        "metric": "official Treasury payments using a public distributed ledger",
        "subject": "U.S. Treasury", "stage": "launched",
        "threshold": "one qualifying official payment", "timezone": "America/New_York",
    },
    "synthetic-broker-tokenized-asset": {
        "pool": "robinhood", "pair_class": "weak_proxy",
        "rule_date": date(2026, 12, 31),
        "thesis_digest": "87d421fe1263593b839e06b5bfe99352b00ff95b0dc42b8bc15b8f426998f49e",
        "resource": "synthetic-market-broker-tokenized-asset.json",
        "fixture_sha256": "c4f080a0aef6399a2e14a18455fbd363156c98bebcb4a19fb9a8b1d69502fb6a",
        "fixture_body_bytes": 939,
        "rules_sha256": "0563014db7f9c273cb24d4576037d39e0e01ddfc3dca331b568280578ae7c60e",
        "condition_id": "synthetic-condition-broker-tokenized-asset",
        "close_at_utc": "2027-01-01T04:59:00Z", "liquidity_usd": 1600.0,
        "source_url": "https://example.invalid/fitcheck/markets/synthetic-broker-tokenized-asset",
        "yes": "At least one qualifying tokenized U.S. exchange-listed stock or ETF is purchasable by a U.S. Robinhood brokerage customer in at least one state by the deadline.",
        "no": "No qualifying tokenized stock or ETF is purchasable by the deadline; announcements and tests do not qualify.",
        "deadline": "December 31, 2026, 23:59 ET (January 1, 2027, 04:59 UTC).",
        "source": "Project-authored synthetic platform availability evidence.",
        "captures": "Robinhood makes one qualifying tokenized U.S. stock or ETF purchasable by U.S. brokerage customers in at least one state by December 31, 2026.",
        "misses": "One tokenized stock or ETF does not establish onchain trading for at least five asset classes or an API for autonomous trading systems. The market deadline is one year earlier than the accepted thesis deadline.",
        "metric": "qualifying tokenized stocks available to U.S. Robinhood brokerage customers",
        "subject": "Robinhood", "stage": "launched",
        "threshold": "at least one qualifying tokenized stock or ETF in at least one U.S. state",
        "timezone": "America/New_York",
    },
    "synthetic-broker-first-tokenized": {
        "pool": "robinhood", "pair_class": "weak_proxy",
        "rule_date": date(2027, 12, 31),
        "thesis_digest": "87d421fe1263593b839e06b5bfe99352b00ff95b0dc42b8bc15b8f426998f49e",
        "resource": "synthetic-market-broker-first-tokenized.json",
        "fixture_sha256": "2270283243713c8d6cbed26fc4d8f53bee02d765336f8e254e4fabadb3f793bc",
        "fixture_body_bytes": 1129,
        "rules_sha256": "59e29318c720759b60f526c428c6d12c849a78d75629ae346da62dfff47d8aa3",
        "condition_id": "synthetic-condition-broker-first-tokenized",
        "close_at_utc": "2028-01-01T04:59:00Z", "liquidity_usd": 1200.0,
        "source_url": "https://example.invalid/fitcheck/markets/synthetic-broker-first-tokenized",
        "event": {
            "event_id": "synthetic-event-tokenized-listing",
            "resource": "synthetic-event-tokenized-listing.json",
            "fixture_sha256": "024e04ca26ec272fd146da921ae40f62b982c811e5467993c140f66a69daf540",
            "fixture_body_bytes": 2103,
            "source_url": "https://example.invalid/fitcheck/events/synthetic-event-tokenized-listing",
        },
        "yes": "Robinhood is first among the four named platforms to make a qualifying tokenized U.S. stock or ETF purchasable by a U.S. customer, using earlier purchasability and then earlier public confirmation as the same-day tie-breaks.",
        "no": "Robinhood is not first; another named platform may qualify first, or none may qualify by the deadline.",
        "deadline": "December 31, 2027, 23:59 ET (January 1, 2028, 04:59 UTC).",
        "source": "Project-authored synthetic platform availability evidence.",
        "captures": "Whether Robinhood is first among four named platforms to offer one qualifying tokenized U.S. stock or ETF to U.S. customers.",
        "misses": "Being first with one qualifying stock or ETF does not establish onchain trading across five asset classes or an API for autonomous systems. Another platform can qualify first even if Robinhood later meets the broader accepted thesis.",
        "metric": "first named platform offering a qualifying tokenized U.S. stock or ETF",
        "subject": "Robinhood", "stage": "launched",
        "threshold": "first among four named platforms", "timezone": "America/New_York",
    },
    "synthetic-broker-chain-token": {
        "pool": "robinhood", "pair_class": "not_an_expression",
        "rule_date": date(2027, 12, 31),
        "thesis_digest": "87d421fe1263593b839e06b5bfe99352b00ff95b0dc42b8bc15b8f426998f49e",
        "resource": "synthetic-market-broker-chain-token.json",
        "fixture_sha256": "20514dfa51bf9b321fdb7b99e699bd6f144c5bffb4657c5431c6df01f48e2d2b",
        "fixture_body_bytes": 1263,
        "rules_sha256": "fd983f9a53e57dc76bed44bf09c068b76b162125e6f27ae6af5b4a2760ef38b4",
        "condition_id": "synthetic-condition-broker-chain-token",
        "close_at_utc": "2028-01-01T04:59:00Z", "liquidity_usd": 800.0,
        "source_url": "https://example.invalid/fitcheck/markets/synthetic-broker-chain-token",
        "yes": "Both synthetic checks pass: a qualifying chain-role designation and launch occurs after the fixture version date, and the asset can actually be exchanged by the public before the deadline.",
        "no": "Either synthetic check fails. A statement of intent is insufficient, and the listed out-of-scope asset categories do not qualify.",
        "deadline": "December 31, 2027, 23:59 ET (January 1, 2028, 04:59 UTC).",
        "source": "Project-authored synthetic platform launch evidence.",
        "captures": "Whether one asset passes both the chain-role launch and functioning public-exchange checks.",
        "misses": "A chain token is distinct from tokenized stock and other asset-class trading. It does not test five onchain asset classes or an autonomous-trading API.",
        "metric": "asset passing both chain-role launch and public-exchange checks",
        "subject": "Robinhood Chain", "stage": "launched",
        "threshold": "one qualifying token", "timezone": "America/New_York",
    },
}


def structure_digest(structure: ExtractedStructure) -> str:
    return sha256(json.dumps(structure.model_dump(mode="json"), sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()


def registered_pair(structure: ExtractedStructure | None) -> str | None:
    digest = structure_digest(structure) if structure is not None else None
    return next((name for name, mids in POOLS.items()
                 if PAIRS[mids[0]]["thesis_digest"] == digest), None)


def _resource_bytes(resource: str) -> bytes:
    return files("el.product.demo_fixtures").joinpath("markets", resource).read_bytes()


def _is_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def _is_fixture_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "example.invalid"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and not parsed.query
        and not parsed.fragment
    )


def _pool_resource_digests(pool: str) -> list[str]:
    bindings = [
        f"provider:{PROVIDER_VERSION}",
        f"curation:{CURATION_POLICY}",
        f"eligibility:{ELIGIBILITY_POLICY}",
        f"evidence:{EVIDENCE_SCHEMA}",
        f"market-schema:{MARKET_FIXTURE_SCHEMA}",
        f"event-schema:{EVENT_FIXTURE_SCHEMA}",
    ]
    for mid in POOLS[pool]:
        pair = PAIRS[mid]
        bindings.append(f"market:{mid}:{pair['fixture_sha256']}")
        event = pair.get("event")
        if event is not None:
            bindings.append(f"event:{event['event_id']}:{event['fixture_sha256']}")
    return bindings


def snapshot_id(pool: str) -> str:
    digest = sha256("|".join(_pool_resource_digests(pool)).encode()).hexdigest()
    return f"frozen-demo-{pool}-{digest[:16]}"


def public_market(mid: str) -> dict:
    pair = PAIRS[mid]
    blob = _resource_bytes(pair["resource"])
    if (len(blob) != pair["fixture_body_bytes"]
            or sha256(blob).hexdigest() != pair["fixture_sha256"]):
        raise MarketPoolConflict("synthetic demo market resource digest mismatch")
    try:
        market = json.loads(blob)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MarketPoolConflict("synthetic demo market resource is invalid") from None
    if (not isinstance(market, dict) or set(market) != _MARKET_FIELDS
            or market.get("schema_version") != MARKET_FIXTURE_SCHEMA
            or market.get("fixture_kind") != FIXTURE_KIND
            or market.get("market_id") != mid
            or market.get("condition_id") != pair["condition_id"]
            or market.get("outcomes") != ["Yes", "No"]
            or market.get("close_at_utc") != pair["close_at_utc"]
            or market.get("liquidity_usd") != pair["liquidity_usd"]
            or market.get("source_url") != pair["source_url"]
            or market.get("fixture_versioned_utc") != FIXTURE_VERSIONED_UTC
            or not _is_utc_timestamp(market.get("close_at_utc"))
            or not _is_utc_timestamp(market.get("fixture_versioned_utc"))
            or not _is_fixture_url(market.get("source_url"))
            or isinstance(market.get("liquidity_usd"), bool)
            or not isinstance(market.get("liquidity_usd"), (int, float))
            or not math.isfinite(market["liquidity_usd"])
            or market["liquidity_usd"] < 0
            or not isinstance(market.get("title"), str) or not market["title"]
            or not isinstance(market.get("resolution_rules"), str)
            or not market["resolution_rules"]
            or sha256(market["resolution_rules"].encode()).hexdigest()
            != pair["rules_sha256"]):
        raise MarketPoolConflict("synthetic demo market binding mismatch")
    return market


def event_association(mid: str, market: dict) -> dict | None:
    event_binding = PAIRS[mid].get("event")
    if event_binding is None:
        return None
    blob = _resource_bytes(event_binding["resource"])
    if (len(blob) != event_binding["fixture_body_bytes"]
            or sha256(blob).hexdigest() != event_binding["fixture_sha256"]):
        raise MarketPoolConflict("synthetic demo event resource digest mismatch")
    try:
        event = json.loads(blob)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MarketPoolConflict("synthetic demo event resource is invalid") from None
    children = event.get("markets") if isinstance(event, dict) else None
    if (not isinstance(event, dict) or set(event) != _EVENT_FIELDS
            or event.get("schema_version") != EVENT_FIXTURE_SCHEMA
            or event.get("fixture_kind") != FIXTURE_KIND
            or event.get("event_id") != event_binding["event_id"]
            or event.get("source_url") != event_binding["source_url"]
            or event.get("fixture_versioned_utc") != FIXTURE_VERSIONED_UTC
            or not _is_utc_timestamp(event.get("fixture_versioned_utc"))
            or not _is_fixture_url(event.get("source_url"))
            or not isinstance(event.get("title"), str) or not event["title"]
            or not isinstance(event.get("description"), str) or not event["description"]
            or not isinstance(children, list) or len(children) != 1
            or children[0] != market):
        raise MarketPoolConflict("synthetic demo event child binding mismatch")
    return {
        "event_id": event_binding["event_id"],
        "event_resource": event_binding["resource"],
        "event_fixture_sha256": event_binding["fixture_sha256"],
        "matched_child_market_id": mid,
        "matched_child_condition_id": market["condition_id"],
        "parent_description_differs": event["description"] != market["resolution_rules"],
    }


class FrozenDemoMarketProvider:
    name = PROVIDER_VERSION

    def retrieve(self, structure: ExtractedStructure | None = None):
        pool = registered_pair(structure)
        if pool is None:
            raise MarketPoolConflict(
                "Frozen candidate-bound evidence is unavailable for this accepted thesis; "
                "no pool conclusion was made."
            )
        mids = POOLS[pool]
        records = []
        for mid in mids:
            pair = PAIRS[mid]
            market = public_market(mid)
            event_association(mid, market)
            records.append(CandidateMarketRecord(
                market_id=mid,
                title=market["title"],
                description=market["resolution_rules"],
                resolution_rules=market["resolution_rules"],
                close_date=pair["rule_date"],
                outcomes=market["outcomes"],
                liquidity_usd=market["liquidity_usd"],
                source_url=market["source_url"],
            ))
        return MarketRetrievalResult(
            mode=self.name,
            snapshot_id=snapshot_id(pool),
            as_of_ts=datetime.fromisoformat(FIXTURE_VERSIONED_UTC.replace("Z", "+00:00")),
            retrieval_id=(
                f"synthetic-pool-v3-{PAIRS[mids[0]]['thesis_digest'][:16]}-"
                f"{snapshot_id(pool).rsplit('-', 1)[-1]}"
            ),
            markets=records,
            query_summary={
                "source": "project_authored_synthetic_fixture",
                "returned_count": 3,
                "query_binding": PAIRS[mids[0]]["thesis_digest"],
            },
        )


def market_structures() -> dict[str, MarketStructure]:
    return {mid: MarketStructure(
        market_id=mid,
        snapshot_id="identity-stamped-by-request",
        event_stage=pair["stage"],
        metric={"what": pair["metric"], "measured_by": pair["source"], "objective": True},
        horizon={"resolution_date": pair["rule_date"], "timezone": pair["timezone"]},
        entities=[{"name": pair["subject"], "role": "subject"}],
        threshold=pair["threshold"],
        direction=None,
        resolution_source_class="official",
        extraction_policy_version=2,
    ) for mid, pair in PAIRS.items()}


def _bound_pair(assessment, thesis, capture) -> tuple[str, dict]:
    evidence = (assessment.provenance or {}).get("frozen_demo_evidence") or {}
    if (evidence.get("policy_version") in UNSUPPORTED_CURATION_POLICIES
            or evidence.get("schema_version") not in {None, EVIDENCE_SCHEMA}):
        raise MarketPoolConflict("legacy frozen demo evidence is unsupported by v3")
    structure = ExtractedStructure.model_validate(thesis.extracted_structure)
    pool = registered_pair(structure)
    mid = assessment.market_id
    if pool is None or mid not in POOLS[pool] or capture.market_id != mid:
        raise MarketPoolConflict("frozen demo thesis and market binding mismatch")
    pair = PAIRS[mid]
    market = public_market(mid)
    if (capture.resolution_rules_hash != pair["rules_sha256"]
            or capture.resolution_rules_text != market["resolution_rules"]
            or capture.contract_terms_text != market["title"]
            or assessment.snapshot_id != snapshot_id(pool)):
        raise MarketPoolConflict("frozen demo snapshot and rules binding mismatch")
    return mid, pair


def _expected_evidence(assessment, mid, pair):
    payload = assessment.provenance["per_market"]
    advisory = payload.get("advisory") or {}
    diagnostic = {
        "pair_class": pair_class(payload["published"]),
        "authority": payload["authority"],
        "fit_gate_policy_version": assessment.provenance.get("fit_gate_policy_version"),
        "fit_confidence": (
            advisory.get("confidence") if advisory.get("status") == "accepted" else None
        ),
    }
    market = public_market(mid)
    return {
        "schema_version": EVIDENCE_SCHEMA,
        "policy_version": CURATION_POLICY,
        "authority": AUTHORITY,
        "thesis_structure_sha256": pair["thesis_digest"],
        "market_id": mid,
        "condition_id": market["condition_id"],
        "fixture_resource": pair["resource"],
        "fixture_sha256": pair["fixture_sha256"],
        "rules_sha256": pair["rules_sha256"],
        "snapshot_id": assessment.snapshot_id,
        "automatic_diagnostic": diagnostic,
        "yes_condition": pair["yes"],
        "no_condition": pair["no"],
        "rule_deadline": pair["deadline"],
        "resolution_source": pair["source"],
        "resolution_source_basis": "project-authored synthetic fixture",
        "market_url": market["source_url"],
        "event_association": event_association(mid, market),
        "fixture_versioned_utc": market["fixture_versioned_utc"],
        "fixture_metadata_authority": FIXTURE_KIND,
        "eligibility_policy_version": ELIGIBILITY_POLICY,
        "eligibility_liquidity_floor_usd": 0,
        "fixture_liquidity_usd": market["liquidity_usd"],
        "current_prices_or_final_outcomes_asserted": False,
    }


def _v3_evidence(assessment, mid, pair):
    evidence = _expected_evidence(assessment, mid, pair)
    market = public_market(mid)
    evidence.update({
        "pair_class": pair["pair_class"],
        "fixture_body_bytes": pair["fixture_body_bytes"],
        "market_close_at_utc": market["close_at_utc"],
        "outcomes": market["outcomes"],
        "yes_no_summary_basis": "project_authored_market_rules",
        "child_specific_yes_no_prose": True,
    })
    return evidence


def project_assessment(assessment, thesis, capture):
    mid, pair = _bound_pair(assessment, thesis, capture)
    evidence = _v3_evidence(assessment, mid, pair)
    assessment.pair_class = pair["pair_class"]
    assessment.what_it_captures = pair["captures"]
    assessment.what_it_misses = pair["misses"]
    assessment.authority = AUTHORITY
    assessment.fit_confidence = None
    assessment.provenance = {**assessment.provenance, "frozen_demo_evidence": evidence}
    return assessment


def assessment_evidence(assessment, thesis, capture, fit_card=None) -> dict | None:
    evidence = (assessment.provenance or {}).get("frozen_demo_evidence")
    if assessment.authority != AUTHORITY:
        if evidence is not None:
            raise MarketPoolConflict("frozen demo assessment authority mismatch")
        return None
    mid, pair = _bound_pair(assessment, thesis, capture)
    if (fit_card is None or fit_card.id != assessment.fit_card_id
            or fit_card.thesis_analysis_id != thesis.id
            or fit_card.candidate_set_id != assessment.candidate_set_id
            or assessment.provenance.get("per_market")
            != (fit_card.provenance or {}).get("per_market", {}).get(mid)
            or assessment.provenance.get("fit_gate_policy_version")
            != (fit_card.provenance or {}).get("gate_policy_version")
            or assessment.provenance.get("fit_trace_id")
            != (fit_card.provenance or {}).get("trace_id")):
        raise MarketPoolConflict("frozen demo automatic diagnostic FitCard binding mismatch")
    expected = _v3_evidence(assessment, mid, pair)
    if (evidence != expected
            or assessment.pair_class != pair["pair_class"]
            or assessment.fit_confidence is not None
            or assessment.what_it_captures != pair["captures"]
            or assessment.what_it_misses != pair["misses"]):
        raise MarketPoolConflict("frozen demo assessment provenance mismatch")
    return deepcopy(evidence)


def validate_frozen_display_pool(display_set, thesis, cards) -> None:
    """Reject a frozen-pool count or identity change before read-back/choice."""
    if not display_set.snapshot_id.startswith("frozen-demo-"):
        return
    policies = {
        (card.frozen_demo_evidence or {}).get("policy_version") for card in cards
    }
    if policies & UNSUPPORTED_CURATION_POLICIES:
        raise MarketPoolConflict("legacy frozen demo evidence is unsupported by v3")
    pool = registered_pair(ExtractedStructure.model_validate(thesis.extracted_structure))
    if pool is None:
        raise MarketPoolConflict("frozen demo displayed thesis binding mismatch")
    if display_set.snapshot_id != snapshot_id(pool):
        raise MarketPoolConflict("legacy or unbound frozen demo snapshot is unsupported by v3")
    expected = set(POOLS[pool])
    shown = [card.market_id for card in cards]
    if (display_set.target_count != len(expected)
            or display_set.displayed_count != len(shown)
            or display_set.assessed_count != len(shown)
            or len(set(shown)) != len(shown)
            or not set(shown).issubset(expected)
            or any((card.frozen_demo_evidence or {}).get("policy_version")
                   != CURATION_POLICY for card in cards)
            or any((card.frozen_demo_evidence or {}).get("schema_version")
                   != EVIDENCE_SCHEMA for card in cards)):
        raise MarketPoolConflict("frozen demo displayed pool identity mismatch")
    if display_set.assessment_complete and set(shown) != expected:
        raise MarketPoolConflict("frozen demo complete pool identity mismatch")
    if display_set.system_pool_outcome == "no_clean_expression" and (
        not display_set.assessment_complete or set(shown) != expected
    ):
        raise MarketPoolConflict("frozen demo no-clean pool identity mismatch")
