"""Pure planning for one bounded classification attempt.

No database writes occur here. Provider/model work completes before the
worker opens the short fenced transaction that publishes the output bundle.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from el.classification.contracts import ClassificationPinManifest
from el.domain.enums import HorizonMatch, ResolutionRisk
from el.domain.structures import ExtractedStructure, MarketStructure, Provenance
from el.domain.vocabulary import vocabulary_violations
from el.fitgate.gate import merge_advisory
from el.fitgate.policy import (
    AUTHORITY_DETERMINISTIC_ONLY,
    FitPolicy,
    MarketFitVerdict,
    ThesisFit,
    aggregate_thesis,
    class_rank,
    evaluate_market,
)
from el.marketstructure.gate import MarketGateVerdict, market_structure_gate
from el.models.market_adapter import MarketStructureProposer
from el.retrieval.candidate_contracts import CandidateQueryResult
from el.retrieval.gate import Loop2Policy, evaluate_eligibility
from el.retrieval.provider import CandidateMarketRecord
from el.retrieval.ranking import rank_candidates


class ClassificationPlanError(RuntimeError):
    """A deterministic input or invariant prevents classification."""


@dataclass(frozen=True)
class CandidateEvidence:
    record: CandidateMarketRecord
    snapshot_ts: datetime
    contract_terms_hash: str
    resolution_rules_hash: str


@dataclass(frozen=True)
class CandidateMemberPlan:
    evidence: CandidateEvidence
    rank: int
    retrieval_score: float
    eligibility_flags: dict[str, str]
    excluded_reason: str | None


@dataclass(frozen=True)
class StructurePlan:
    evidence: CandidateEvidence
    structure: MarketStructure
    model_adapter: str
    model_run_id: str


@dataclass(frozen=True)
class JudgmentPlan:
    verdict: MarketFitVerdict
    rank: int
    model_adapter: str
    model_run_id: str


@dataclass(frozen=True)
class ClassificationPlan:
    judged_at: datetime
    retrieval_id: str
    query_digest: str
    members: tuple[CandidateMemberPlan, ...]
    structures: tuple[StructurePlan, ...]
    structure_rejections: tuple[dict, ...]
    judgments: tuple[JudgmentPlan, ...]
    thesis: ThesisFit
    reference: MarketFitVerdict | None
    authority: str
    thesis_side: str | None
    what_it_captures: str
    what_it_misses: str
    horizon_match: str
    resolution_risk: str
    provenance: dict
    rejection_reasons: tuple[tuple[str, str], ...]


Checkpoint = Callable[[str], None]
MAX_SELECTED_MARKET_IDS = 128


def build_classification_plan(
    *,
    claim: ExtractedStructure,
    query: CandidateQueryResult,
    pins: ClassificationPinManifest,
    proposer: MarketStructureProposer,
    checkpoint: Checkpoint,
    judged_at: datetime,
    attempt_trace_id: str,
) -> ClassificationPlan:
    records = [
        _record_from_hit(hit.market, pins.snapshot.venue) for hit in query.hits
    ]
    source_timestamps = {
        hit.market.market_id: hit.market.snapshot_ts for hit in query.hits
    }
    for record in records:
        if len(record.market_id) > 128:
            raise ClassificationPlanError(
                "candidate market ID exceeds the persistence contract"
            )
    if len({record.market_id for record in records}) != len(records):
        raise ClassificationPlanError("candidate query returned duplicate market IDs")

    retrieval_policy = Loop2Policy(
        min_liquidity_usd=pins.retrieval.min_liquidity_usd,
        min_taxonomy_confidence=pins.retrieval.min_taxonomy_confidence,
        horizon_slack_days=pins.retrieval.horizon_slack_days,
    )
    ranked = rank_candidates(claim, records)
    evaluated = [
        (record, score, evaluate_eligibility(record, claim, retrieval_policy))
        for record, score in ranked
    ]
    ordered = [item for item in evaluated if item[2].eligible] + [
        item for item in evaluated if not item[2].eligible
    ]
    members = tuple(
        CandidateMemberPlan(
            evidence=_evidence(record, source_timestamps[record.market_id]),
            rank=rank,
            retrieval_score=score,
            eligibility_flags=dict(verdict.flags),
            excluded_reason=verdict.excluded_reason,
        )
        for rank, (record, score, verdict) in enumerate(ordered, start=1)
    )
    retrieval_id = _retrieval_id(
        pins=pins,
        query_digest=query.query_digest,
        market_ids=[member.evidence.record.market_id for member in members],
    )

    eligible = [member for member in members if member.excluded_reason is None]
    selected = eligible[: pins.structure.structure_limit]
    if len(selected) > MAX_SELECTED_MARKET_IDS:
        raise ClassificationPlanError(
            "selected market count exceeds the worker contract"
        )
    structures: list[StructurePlan] = []
    structure_rejections: list[dict] = []
    for member in selected:
        market_id = member.evidence.record.market_id
        checkpoint(f"structure:{market_id}:before")
        proposed = proposer.propose_market_structure(
            market_id=market_id,
            snapshot_id=pins.snapshot.snapshot_id,
            contract_terms_text=member.evidence.record.title,
            resolution_rules_text=member.evidence.record.resolution_rules,
        )
        if proposed.model_adapter != pins.structure.model_adapter:
            raise ClassificationPlanError(
                "market proposer identity differs from the pinned adapter"
            )
        gated = market_structure_gate(
            market_id=market_id,
            snapshot_id=pins.snapshot.snapshot_id,
            structure=proposed.structure,
        )
        if gated.verdict is not MarketGateVerdict.PASS:
            structure_rejections.append(
                {
                    "market_id": market_id,
                    "reasons": list(gated.reasons),
                    "model_adapter": proposed.model_adapter,
                    "model_run_id": proposed.model_run_id,
                }
            )
        else:
            assert gated.structure is not None
            structures.append(
                StructurePlan(
                    evidence=member.evidence,
                    structure=gated.structure,
                    model_adapter=proposed.model_adapter,
                    model_run_id=proposed.model_run_id,
                )
            )
        checkpoint(f"structure:{market_id}:after")

    fit_policy = FitPolicy(
        stacking_threshold=pins.fit.stacking_threshold,
        escalation_confidence_floor=pins.fit.escalation_confidence_floor,
        horizon_tolerances=dict(pins.fit.horizon_tolerances),
        alias_rules_version=pins.fit.alias_rules_version,
        m1_direction_guard=pins.fit.m1_direction_guard,
    )
    ranks = {member.evidence.record.market_id: member.rank for member in members}
    judgments: list[JudgmentPlan] = []
    for planned in structures:
        deterministic = evaluate_market(claim, planned.structure, fit_policy)
        merged = merge_advisory(
            deterministic,
            None,
            market_id=planned.structure.market_id,
            policy=fit_policy,
        )
        judgments.append(
            JudgmentPlan(
                verdict=merged.verdict,
                rank=ranks[planned.structure.market_id],
                model_adapter=planned.model_adapter,
                model_run_id=planned.model_run_id,
            )
        )
    thesis = aggregate_thesis(
        [(judgment.verdict, judgment.rank) for judgment in judgments]
    )
    reference = _reference(thesis, judgments)
    authority = (
        reference.authority if reference is not None else AUTHORITY_DETERMINISTIC_ONLY
    )
    thesis_side = (
        reference.thesis_side
        if reference is not None and thesis.recommended_market_id is not None
        else None
    )
    captures, misses = _card_text(thesis, reference)
    escalation = _escalation(claim, judgments)
    provenance = _provenance(
        claim=claim,
        pins=pins,
        query=query,
        members=members,
        structures=structures,
        judgments=judgments,
        authority=authority,
        thesis_side=thesis_side,
        escalation=escalation,
        judged_at=judged_at,
        attempt_trace_id=attempt_trace_id,
    )
    rejection_reasons = tuple(
        (verdict.market_id, _rejection_reason(verdict))
        for verdict in thesis.rejected
    )
    return ClassificationPlan(
        judged_at=judged_at,
        retrieval_id=retrieval_id,
        query_digest=query.query_digest,
        members=members,
        structures=tuple(structures),
        structure_rejections=tuple(structure_rejections),
        judgments=tuple(judgments),
        thesis=thesis,
        reference=reference,
        authority=authority,
        thesis_side=thesis_side,
        what_it_captures=captures,
        what_it_misses=misses,
        horizon_match=(
            reference.horizon_match.value
            if reference is not None and reference.horizon_match is not None
            else HorizonMatch.POOR.value
        ),
        resolution_risk=(
            reference.resolution_risk.value
            if reference is not None and reference.resolution_risk is not None
            else ResolutionRisk.HIGH.value
        ),
        provenance=provenance,
        rejection_reasons=rejection_reasons,
    )


def _record_from_hit(market, venue: str) -> CandidateMarketRecord:
    return CandidateMarketRecord(
        market_id=market.market_id,
        title=market.title,
        venue=venue,
        description=market.description,
        resolution_rules=market.resolution_rules,
        close_date=market.close_date,
        outcomes=market.outcomes,
        current_probability=None,
        # The shared snapshot carries volume, not executable liquidity.
        liquidity_usd=None,
        taxonomy_l1=market.taxonomy_l1,
        taxonomy_confidence=market.taxonomy_confidence,
        taxonomy_low_confidence=None,
        tags=market.tags,
        source_url=market.source_url,
    )


def _evidence(
    record: CandidateMarketRecord,
    snapshot_ts: datetime | None = None,
) -> CandidateEvidence:
    if snapshot_ts is None:
        raise ClassificationPlanError("candidate observation time is missing")
    return CandidateEvidence(
        record=record,
        snapshot_ts=snapshot_ts,
        contract_terms_hash=hashlib.sha256(record.title.encode()).hexdigest(),
        resolution_rules_hash=hashlib.sha256(
            record.resolution_rules.encode()
        ).hexdigest(),
    )


def _retrieval_id(
    *, pins: ClassificationPinManifest, query_digest: str, market_ids: list[str]
) -> str:
    raw = json.dumps(
        {
            "snapshot_id": pins.snapshot.snapshot_id,
            "query_digest": query_digest,
            "ranking_policy_version": pins.retrieval.ranking_policy_version,
            "eligibility_policy_version": (
                pins.retrieval.eligibility_policy_version
            ),
            "market_ids": market_ids,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"retr_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _reference(
    thesis: ThesisFit, judgments: list[JudgmentPlan]
) -> MarketFitVerdict | None:
    if thesis.recommended_market_id is not None:
        return next(
            judgment.verdict
            for judgment in judgments
            if judgment.verdict.market_id == thesis.recommended_market_id
        )
    return thesis.rejected[0] if thesis.rejected else None


def _escalation(
    claim: ExtractedStructure, judgments: list[JudgmentPlan]
) -> dict:
    reasons = ["advisory_absent"]
    if claim.ambiguities:
        reasons.append("claim_ambiguities_present")
    published = sorted(
        (judgment.verdict.published for judgment in judgments),
        key=lambda fit_class: -class_rank(fit_class),
    )
    if len(published) >= 2 and published[0] == published[1]:
        reasons.append("near_tie_top_candidates")
    return {"eligible": bool(reasons), "reasons": reasons}


def _provenance(
    *,
    claim: ExtractedStructure,
    pins: ClassificationPinManifest,
    query: CandidateQueryResult,
    members: tuple[CandidateMemberPlan, ...],
    structures: list[StructurePlan],
    judgments: list[JudgmentPlan],
    authority: str,
    thesis_side: str | None,
    escalation: dict,
    judged_at: datetime,
    attempt_trace_id: str,
) -> dict:
    base = Provenance(
        gate_policy_version=pins.fit.gate_policy_version,
        extraction_schema_version=claim.schema_version,
        market_structure_schema_version=pins.structure.schema_version,
        model_adapter="deterministic-only",
        model_run_id="deterministic",
        trace_id=attempt_trace_id,
        eval_pack_version="classification-worker-v1",
        judged_at=judged_at,
    )
    structures_by_market = {
        planned.structure.market_id: planned for planned in structures
    }
    per_market = {}
    for judgment in judgments:
        verdict = judgment.verdict
        planned = structures_by_market[verdict.market_id]
        per_market[verdict.market_id] = {
            "ceiling": verdict.deterministic_ceiling.value,
            "published": verdict.published.value,
            "authority": verdict.authority,
            "hard_fails": verdict.hard_fail_count,
            "fired": verdict.fired(),
            "thesis_side": verdict.thesis_side,
            "advisory": {"status": "not_configured", "calls": 0, "rejected": 0},
            "structure_proposer": {
                "model_adapter": planned.model_adapter,
                "model_run_id": planned.model_run_id,
                "structure_sha256": _structure_sha256(planned.structure),
                "market_snapshot_ts": planned.evidence.snapshot_ts.isoformat(),
            },
            "checks": [
                {
                    "check_id": outcome.check_id,
                    "status": outcome.status.value,
                    "cap": outcome.cap.value if outcome.cap else None,
                    "hard": outcome.hard,
                    "detail": outcome.detail,
                }
                for outcome in verdict.checks
            ],
        }
    return {
        **base.model_dump(mode="json"),
        "authority": authority,
        "confidence_source": "deterministic_fallback_uncalibrated",
        "thesis_side": thesis_side,
        "escalation": escalation,
        "per_market": per_market,
        "retrieval": {
            "backend": query.backend,
            "index_identity": query.index_sha256,
            "query_digest": query.query_digest,
            "backend_request_id": query.backend_request_id,
            "source_results": [
                {
                    "market_id": hit.market.market_id,
                    "source_order": source_order,
                    "source_score": hit.source_score,
                }
                for source_order, hit in enumerate(query.hits, start=1)
            ],
        },
        "structure_extraction": {
            "retrieved_count": len(members),
            "eligible_count": sum(
                member.excluded_reason is None for member in members
            ),
            "structured_count": len(structures),
            "skipped_unstructured_count": max(
                0,
                sum(member.excluded_reason is None for member in members)
                - pins.structure.structure_limit,
            ),
            "structure_cap": pins.structure.structure_limit,
        },
    }


def _card_text(
    thesis: ThesisFit, reference: MarketFitVerdict | None
) -> tuple[str, str]:
    if reference is None:
        captures = "No eligible candidate markets were available to evaluate."
        misses = (
            "Nothing was evaluated; a draft contract is the cheapest test to "
            "resolve this claim."
        )
    else:
        passes = [
            outcome.name
            for outcome in reference.checks
            if outcome.status.value == "pass"
        ]
        caps = [outcome for outcome in reference.checks if outcome.cap is not None]
        if thesis.recommended_market_id:
            captures = (
                f"Deterministic conditions met on {reference.market_id}: "
                + ", ".join(passes)
                + "."
            )
            misses = "; ".join(outcome.detail for outcome in caps) or (
                "No deterministic mismatches. Metric identity rests on lexical "
                "overlap, not proof — read the resolution rules before recording "
                "intent."
            )
        else:
            captures = (
                f"No recommended expression: best candidate {reference.market_id} "
                f"reaches {reference.published.value}."
            )
            misses = "; ".join(outcome.detail for outcome in caps) or (
                "Stacked condition failures left no usable expression."
            )
    _require_clean_vocabulary(captures)
    _require_clean_vocabulary(misses)
    return captures, misses


def _rejection_reason(verdict: MarketFitVerdict) -> str:
    fired = [outcome for outcome in verdict.checks if outcome.cap is not None]
    reason = f"{verdict.published.value}: " + (
        "; ".join(outcome.detail for outcome in fired) if fired else "outranked"
    )
    _require_clean_vocabulary(reason)
    return reason


def _require_clean_vocabulary(text: str) -> None:
    violations = vocabulary_violations(text)
    if violations:
        raise ClassificationPlanError(
            f"classification template violates vocabulary policy: {violations}"
        )


def _structure_sha256(structure: MarketStructure) -> str:
    payload = json.dumps(
        structure.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
