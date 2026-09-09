"""Loop 3 service: structures -> verdicts -> fit card, one transaction.

Blueprint §14. Operates downstream of retrieval (Loop 2) and market
structure extraction (Loop 2.5): eligible candidates only — the
structure service already skips ineligible members, and markets whose
structure proposals were gate-rejected simply have no structure to
judge (they surface in StructureOutcome.rejected, headed to Loop 4
wiring at build-order step 8).

Persists per spec v2 Core Objects:
- one fit_card (check vectors, authority, thesis_side, escalation log
  in provenance JSON — partial provenance is no provenance);
- one market_recommendation ALWAYS (it anchors rejections;
  recommended_market_id null on weak/no-clean);
- one rejected_markets row per evaluated non-recommended market with
  check-derived reasons (validated rejections are first-class data).

Advisory integration (demotion-only, el.fitgate.gate): when a proposer
is configured, each (claim, market) pair gets one advisory call with a
retry budget of 1; a rejected or failing advisory falls back to the
deterministic card (authority=deterministic_fallback, fit_confidence
NULL, confidence_source=deterministic_fallback_uncalibrated) — a
published class is never dressed up as calibrated when nothing
calibrated it. The escalation predicate (Program 2 seam) stays
LOG-ONLY: deterministic, versioned, evaluated here, never by a model.
"""

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.enums import FitClass, HorizonMatch, ResolutionRisk
from el.domain.structures import ExtractedStructure, Provenance
from el.domain.tables import (
    CandidateSet,
    CandidateSetMember,
    FitCard,
    MarketRecommendation,
    MarketRulesCapture,
    MarketSnapshot,
    RejectedMarketRow,
    ThesisAnalysis,
)
from el.domain.vocabulary import vocabulary_violations
from el.fitgate.gate import (
    AdvisoryMerge,
    merge_advisory,
    quote_span_violations,
)
from el.fitgate.policy import (
    AUTHORITY_DETERMINISTIC_ONLY,
    AUTHORITY_FALLBACK,
    FIT_GATE_POLICY_VERSION,
    FitPolicy,
    MarketFitVerdict,
    ThesisFit,
    aggregate_thesis,
    class_rank,
    evaluate_market,
    new_trace_id,
)
from el.marketstructure.service import (
    SCHEMA_VERSION as MARKET_SCHEMA_VERSION,
    MarketStructureService,
)
from el.models.fit_adapter import FitAdvisoryProposer

EVAL_PACK_VERSION = "phase0-fit-v1"
CONFIDENCE_SOURCE_ADVISORY = "advisory"
CONFIDENCE_SOURCE_FALLBACK = "deterministic_fallback_uncalibrated"
ADVISORY_RETRY_BUDGET = 1  # ratification item 9: retry 1, then fallback
CAP_POLICY_VERSION = "adaptive-v1"


def _claim_corpus(claim: ExtractedStructure, input_text: str) -> str:
    """The claim-side text an advisory may quote from for citation
    checks: the raw input plus the extracted structure's text fields."""
    parts = [
        input_text,
        claim.claim_summary,
        claim.contractible_version,
        claim.metric.what,
        claim.metric.measured_by,
        *[entity.name for entity in claim.entities],
    ]
    return " ".join(part for part in parts if part)


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FitOutcome(_Out):
    fit_card_id: uuid.UUID
    market_recommendation_id: uuid.UUID
    thesis_analysis_id: uuid.UUID
    candidate_set_id: uuid.UUID
    fit_class: FitClass
    recommended_market_id: str | None
    thesis_side: str | None
    draft_contract_recommended: bool
    rejected_count: int
    skipped_ineligible: int
    structure_cache_hits: int
    structures_extracted: int
    # Top-N structure-cap funnel (mirrors StructureOutcome): the retrieved
    # universe is unbounded; structured_count (and Gemini spend) is capped.
    retrieved_count: int
    eligible_count: int
    structured_count: int
    skipped_unstructured_count: int
    structure_cap: int | None
    authority: str
    fit_confidence: float | None
    confidence_source: str
    escalation_eligible: bool
    # Cost observability (step-4 pattern): every advisory call is spend.
    advisory_calls: int
    advisory_rejected: int
    advisory_fallbacks: int
    gate_policy_version: str = FIT_GATE_POLICY_VERSION
    expanded: bool = False
    final_cap: int | None = None


class _MarketJudgment(_Out):
    """One market's merged judgment plus its advisory bookkeeping."""

    merge: AdvisoryMerge
    rank: int
    advisory_meta: dict


class FitService:
    def __init__(
        self,
        structure_service: MarketStructureService,
        session_factory: sessionmaker[Session],
        policy: FitPolicy = FitPolicy(),
        advisory: FitAdvisoryProposer | None = None,
        *,
        cap_initial: int | None = None,
        cap_expanded: int | None = None,
    ):
        self._structures = structure_service
        self._sessions = session_factory
        self._policy = policy
        self._advisory = advisory
        self._cap_initial = cap_initial
        self._cap_expanded = cap_expanded

    def classify_fit(
        self,
        thesis_analysis_id: uuid.UUID,
        candidate_set_id: uuid.UUID,
    ) -> FitOutcome:
        with self._sessions() as session:
            candidate_set = session.get(CandidateSet, candidate_set_id)
            if candidate_set is None:
                raise ValueError(f"candidate_set {candidate_set_id} not found")
            if candidate_set.thesis_analysis_id != thesis_analysis_id:
                raise ValueError(
                    "candidate set and thesis analysis belong to different runs"
                )

        expanded = False
        final_cap = self._cap_initial
        initial_fit_class = None
        initial_rec_id = None
        expansion_reason = None

        # Store judgments by market_id to avoid redundant evaluation / advisory proposer calls
        judgments_by_market: dict[str, _MarketJudgment] = {}

        # 1. Fetch structures with initial cap if specified, else baseline
        if self._cap_initial is not None:
            structures = self._structures.ensure_structures(
                candidate_set_id, top_n_override=self._cap_initial
            )
        else:
            structures = self._structures.ensure_structures(candidate_set_id)
            final_cap = structures.structure_cap

        # Load claim and perform initial judgment loop
        with self._sessions() as session:
            claim, input_text = self._load_claim(session, thesis_analysis_id)
            ranks = self._member_ranks(session, candidate_set_id)
            for market in structures.structures:
                j = self._judge_market(
                    session,
                    claim,
                    input_text,
                    market,
                    structures.snapshot_id,
                    ranks.get(market.market_id, len(ranks)),
                )
                judgments_by_market[market.market_id] = j

            # Rebuild judgments list in the order of structures
            judgments = [judgments_by_market[m.market_id] for m in structures.structures]
            verdicts = [(j.merge.verdict, j.rank) for j in judgments]
            thesis = aggregate_thesis(verdicts)

            initial_fit_class = thesis.fit_class.value
            initial_rec_id = thesis.recommended_market_id

            if (
                self._cap_initial is not None
                and self._cap_expanded is not None
                and thesis.fit_class != FitClass.DIRECT
                and structures.eligible_count > self._cap_initial
                and self._cap_expanded > self._cap_initial
            ):
                expanded = True
                final_cap = self._cap_expanded
                expansion_reason = f"fit_class_{thesis.fit_class.value}"

        # If we expanded, fetch again up to the expanded cap and run a fresh judgment loop
        if expanded:
            structures = self._structures.ensure_structures(
                candidate_set_id, top_n_override=self._cap_expanded
            )
            # Recompute judgments on the expanded set, but only for NEW ones!
            with self._sessions() as session:
                claim, input_text = self._load_claim(session, thesis_analysis_id)
                ranks = self._member_ranks(session, candidate_set_id)
                for market in structures.structures:
                    if market.market_id not in judgments_by_market:
                        j = self._judge_market(
                            session,
                            claim,
                            input_text,
                            market,
                            structures.snapshot_id,
                            ranks.get(market.market_id, len(ranks)),
                        )
                        judgments_by_market[market.market_id] = j

                # Rebuild judgments list in the final expanded order
                judgments = [judgments_by_market[m.market_id] for m in structures.structures]
                verdicts = [(j.merge.verdict, j.rank) for j in judgments]
                thesis = aggregate_thesis(verdicts)

        with self._sessions() as session:
            claim, input_text = self._load_claim(session, thesis_analysis_id)
            ranks = self._member_ranks(session, candidate_set_id)
            reference = self._reference_verdict(thesis, verdicts)
            reference_judgment = next(
                (
                    j
                    for j in judgments
                    if reference and j.merge.verdict.market_id == reference.market_id
                ),
                None,
            )
            side = (
                reference.thesis_side
                if reference and thesis.recommended_market_id
                else None
            )
            confidence, confidence_source = self._confidence(
                reference_judgment
            )
            authority = self._card_authority(reference_judgment)
            escalation = self._escalation_log(
                claim, judgments, reference_judgment, confidence
            )

            provenance = self._provenance(
                claim,
                judgments,
                escalation,
                side,
                authority,
                confidence_source,
                reference_judgment,
            )
            # Record the top-N structure-extraction funnel
            provenance["structure_extraction"] = {
                "retrieved_count": structures.retrieved_count,
                "eligible_count": structures.eligible_count,
                "structured_count": structures.structured_count,
                "skipped_unstructured_count": structures.skipped_unstructured_count,
                "structure_cap": structures.structure_cap,
                "cap_policy_version": CAP_POLICY_VERSION if self._cap_initial is not None else None,
                "expanded": expanded,
                "initial_cap": self._cap_initial,
                "final_cap": final_cap,
                "initial_fit_class": initial_fit_class,
                "initial_recommended_market_id": initial_rec_id,
                "final_fit_class": thesis.fit_class.value,
                "final_recommended_market_id": thesis.recommended_market_id,
                "expansion_reason": expansion_reason,
            }
            captures, misses = self._card_text(
                thesis, reference, reference_judgment
            )

            card = FitCard(
                thesis_analysis_id=thesis_analysis_id,
                candidate_set_id=candidate_set_id,
                semantic_fit_class=thesis.fit_class.value,
                recommended_market_id=thesis.recommended_market_id,
                what_it_captures=captures,
                what_it_misses=misses,
                horizon_match=(
                    reference.horizon_match.value
                    if reference and reference.horizon_match
                    else HorizonMatch.POOR.value
                ),
                resolution_risk=(
                    reference.resolution_risk.value
                    if reference and reference.resolution_risk
                    else ResolutionRisk.HIGH.value
                ),
                fit_confidence=confidence,
                provenance=provenance,
            )
            session.add(card)
            session.flush()

            snapshot = session.get(MarketSnapshot, structures.snapshot_id)
            if snapshot is None:
                raise ValueError(
                    f"snapshot {structures.snapshot_id} not found — "
                    "retrieval must run before fit classification"
                )
            candidate_set = session.get(CandidateSet, candidate_set_id)
            if candidate_set is None:
                raise ValueError(f"candidate_set {candidate_set_id} not found")
            capture = (
                self._rules_capture(
                    session, thesis.recommended_market_id, snapshot.id
                )
                if thesis.recommended_market_id
                else None
            )
            recommendation = MarketRecommendation(
                thesis_analysis_id=thesis_analysis_id,
                recommended_market_id=thesis.recommended_market_id,
                expression_type=thesis.fit_class.value,
                fit_score=confidence,  # mirrors fit_confidence
                fit_reason=captures,
                why_now=None,
                crowding_note=None,
                as_of_ts=snapshot.as_of_ts,
                snapshot_id=snapshot.id,
                # New rows carry claim-specific provenance on the candidate
                # set. The snapshot value remains a legacy fallback only.
                retrieval_id=(
                    candidate_set.retrieval_id or snapshot.retrieval_id
                ),
                venue_id=snapshot.venue_id,
                contract_terms_hash=(
                    capture.contract_terms_hash if capture else None
                ),
                resolution_rules_hash=(
                    capture.resolution_rules_hash if capture else None
                ),
                rules_captured_at=(
                    capture.rules_captured_at if capture else None
                ),
                provenance=provenance,
                fit_card_id=card.id,
            )
            session.add(recommendation)
            session.flush()

            for verdict in thesis.rejected:
                session.add(
                    RejectedMarketRow(
                        market_recommendation_id=recommendation.id,
                        market_id=verdict.market_id,
                        reason=self._rejection_reason(verdict),
                    )
                )

            session.commit()
            return FitOutcome(
                fit_card_id=card.id,
                market_recommendation_id=recommendation.id,
                thesis_analysis_id=thesis_analysis_id,
                candidate_set_id=candidate_set_id,
                fit_class=thesis.fit_class,
                recommended_market_id=thesis.recommended_market_id,
                thesis_side=side,
                draft_contract_recommended=thesis.draft_contract_recommended,
                rejected_count=len(thesis.rejected),
                skipped_ineligible=structures.skipped_ineligible,
                structure_cache_hits=structures.cache_hits,
                structures_extracted=structures.extracted,
                retrieved_count=structures.retrieved_count,
                eligible_count=structures.eligible_count,
                structured_count=structures.structured_count,
                skipped_unstructured_count=structures.skipped_unstructured_count,
                structure_cap=structures.structure_cap,
                authority=authority,
                fit_confidence=confidence,
                confidence_source=confidence_source,
                escalation_eligible=escalation["eligible"],
                advisory_calls=sum(
                    j.advisory_meta.get("calls", 0) for j in judgments
                ),
                advisory_rejected=sum(
                    j.advisory_meta.get("rejected", 0) for j in judgments
                ),
                advisory_fallbacks=sum(
                    1
                    for j in judgments
                    if j.advisory_meta.get("status") == "fallback"
                ),
                expanded=expanded,
                final_cap=final_cap,
            )

    # --- internals -----------------------------------------------------

    def _load_claim(
        self, session: Session, thesis_analysis_id: uuid.UUID
    ) -> tuple[ExtractedStructure, str]:
        analysis = session.get(ThesisAnalysis, thesis_analysis_id)
        if analysis is None:
            raise ValueError(f"thesis_analysis {thesis_analysis_id} not found")
        claim = ExtractedStructure.model_validate(analysis.extracted_structure)
        if claim.schema_version != 1:
            # Contract rule (blueprint §3): gates declare the schema
            # version they understand; mismatches fail loudly.
            raise ValueError(
                f"loop3-v1 understands extraction schema v1, got "
                f"v{claim.schema_version}"
            )
        return claim, analysis.input_text

    def _judge_market(
        self,
        session: Session,
        claim: ExtractedStructure,
        input_text: str,
        market,
        snapshot_id: str,
        rank: int,
    ) -> _MarketJudgment:
        """Deterministic verdict, then the demotion-only advisory merge.

        Advisory failures degrade, never break: one retry, then the
        deterministic verdict publishes as-is (authority=fallback).
        """
        deterministic = evaluate_market(claim, market, self._policy)
        if self._advisory is None:
            return _MarketJudgment(
                merge=merge_advisory(
                    deterministic, None, market_id=market.market_id
                ),
                rank=rank,
                advisory_meta={"status": "not_configured", "calls": 0,
                               "rejected": 0},
            )

        capture = self._rules_capture(session, market.market_id, snapshot_id)
        calls = rejected = 0
        rejected_reasons: list[str] = []
        for _ in range(1 + ADVISORY_RETRY_BUDGET):
            calls += 1
            try:
                result = self._advisory.propose_fit(
                    market_id=market.market_id,
                    snapshot_id=snapshot_id,
                    claim_structure=claim,
                    market_structure=market,
                    input_text=input_text,
                    contract_terms_text=(
                        capture.contract_terms_text if capture else ""
                    ),
                    resolution_rules_text=(
                        capture.resolution_rules_text if capture else ""
                    ),
                )
            except Exception as error:  # a dead proposer must not kill the card
                rejected += 1
                rejected_reasons.append(f"proposer error: {error}")
                continue
            spans = quote_span_violations(
                result.advisory,
                claim_text=_claim_corpus(claim, input_text),
                rules_text=(
                    f"{capture.contract_terms_text}\n"
                    f"{capture.resolution_rules_text}"
                    if capture
                    else ""
                ),
            )
            if spans:
                # Invented evidence -> discard the advisory (anti-mad-libs).
                rejected += 1
                rejected_reasons.extend(spans)
                continue
            merge = merge_advisory(
                deterministic,
                result.advisory,
                market_id=market.market_id,
                policy=self._policy,
            )
            if merge.accepted:
                return _MarketJudgment(
                    merge=merge,
                    rank=rank,
                    advisory_meta={
                        "status": "accepted",
                        "calls": calls,
                        "rejected": rejected,
                        "model_adapter": result.model_adapter,
                        "model_run_id": result.model_run_id,
                        "confidence": result.advisory.confidence,
                        "suggested_class": result.advisory.suggested_class.value,
                        "advisory_class": (
                            merge.advisory_class.value
                            if merge.advisory_class
                            else None
                        ),
                        "disagreement": merge.disagreement,
                        "what_it_captures": result.advisory.what_it_captures,
                        "what_it_misses": result.advisory.what_it_misses,
                    },
                )
            rejected += 1
            rejected_reasons.extend(merge.rejected_reasons)

        fallback = merge_advisory(
            deterministic, None, market_id=market.market_id
        )
        fallback = fallback.model_copy(
            update={
                "verdict": fallback.verdict.model_copy(
                    update={"authority": AUTHORITY_FALLBACK}
                )
            }
        )
        return _MarketJudgment(
            merge=fallback,
            rank=rank,
            advisory_meta={
                "status": "fallback",
                "calls": calls,
                "rejected": rejected,
                "rejected_reasons": rejected_reasons,
            },
        )

    def _confidence(
        self, reference_judgment: _MarketJudgment | None
    ) -> tuple[float | None, str]:
        if (
            reference_judgment
            and reference_judgment.advisory_meta.get("status") == "accepted"
        ):
            return (
                reference_judgment.advisory_meta["confidence"],
                CONFIDENCE_SOURCE_ADVISORY,
            )
        return None, CONFIDENCE_SOURCE_FALLBACK

    def _card_authority(
        self, reference_judgment: _MarketJudgment | None
    ) -> str:
        if reference_judgment is None:
            return AUTHORITY_DETERMINISTIC_ONLY
        return reference_judgment.merge.verdict.authority

    def _member_ranks(
        self, session: Session, candidate_set_id: uuid.UUID
    ) -> dict[str, int]:
        members = session.scalars(
            select(CandidateSetMember)
            .where(CandidateSetMember.candidate_set_id == candidate_set_id)
            .order_by(CandidateSetMember.rank)
        ).all()
        return {m.market_id: m.rank for m in members}

    def _reference_verdict(
        self,
        thesis: ThesisFit,
        verdicts: list[tuple[MarketFitVerdict, int]],
    ) -> MarketFitVerdict | None:
        """The verdict the card narrates: the recommended market, or the
        best refused candidate (the card explains the refusal)."""
        if thesis.recommended_market_id:
            return next(
                v
                for v, _ in verdicts
                if v.market_id == thesis.recommended_market_id
            )
        return thesis.rejected[0] if thesis.rejected else None

    def _escalation_log(
        self,
        claim: ExtractedStructure,
        judgments: list[_MarketJudgment],
        reference_judgment: _MarketJudgment | None,
        confidence: float | None,
    ) -> dict:
        """Program 2 escalation predicate — LOG-ONLY in v1 (§14).

        Deterministic, versioned; evaluated by the service, never by a
        model. Base rates accrue before the panel exists.
        """
        reasons: list[str] = []
        if confidence is None:
            reasons.append("advisory_absent")
        elif confidence < self._policy.escalation_confidence_floor:
            reasons.append("advisory_confidence_below_floor")
        if reference_judgment and reference_judgment.advisory_meta.get(
            "disagreement"
        ):
            reasons.append("advisory_class_disagreement")
        if claim.ambiguities:
            reasons.append("claim_ambiguities_present")
        published = sorted(
            (j.merge.verdict.published for j in judgments),
            key=lambda c: -class_rank(c),
        )
        if len(published) >= 2 and published[0] == published[1]:
            reasons.append("near_tie_top_candidates")
        return {"eligible": bool(reasons), "reasons": reasons}

    def _provenance(
        self,
        claim: ExtractedStructure,
        judgments: list[_MarketJudgment],
        escalation: dict,
        side: str | None,
        authority: str,
        confidence_source: str,
        reference_judgment: _MarketJudgment | None,
    ) -> dict:
        reference_meta = (
            reference_judgment.advisory_meta if reference_judgment else {}
        )
        base = Provenance(
            gate_policy_version=FIT_GATE_POLICY_VERSION,
            extraction_schema_version=claim.schema_version,
            market_structure_schema_version=MARKET_SCHEMA_VERSION,
            model_adapter=reference_meta.get(
                "model_adapter", "deterministic-only"
            ),
            model_run_id=reference_meta.get("model_run_id", "deterministic"),
            trace_id=new_trace_id(),
            eval_pack_version=EVAL_PACK_VERSION,
            judged_at=datetime.now(timezone.utc),
        )
        per_market = {}
        for judgment in judgments:
            v = judgment.merge.verdict
            advisory_meta = dict(judgment.advisory_meta)
            # Card text lives on the card; keep provenance lean.
            advisory_meta.pop("what_it_captures", None)
            advisory_meta.pop("what_it_misses", None)
            per_market[v.market_id] = {
                "ceiling": v.deterministic_ceiling.value,
                "published": v.published.value,
                "authority": v.authority,
                "hard_fails": v.hard_fail_count,
                "fired": v.fired(),
                "thesis_side": v.thesis_side,
                "horizon_match": (
                    v.horizon_match.value if v.horizon_match else None
                ),
                "resolution_risk": (
                    v.resolution_risk.value if v.resolution_risk else None
                ),
                "advisory": advisory_meta,
                "checks": [
                    {
                        "check_id": o.check_id,
                        "name": o.name,
                        "status": o.status.value,
                        "cap": o.cap.value if o.cap else None,
                        "hard": o.hard,
                        "detail": o.detail,
                    }
                    for o in v.checks
                ],
            }
        return {
            **base.model_dump(mode="json"),
            "authority": authority,
            "confidence_source": confidence_source,
            "thesis_side": side,
            "escalation": escalation,
            "per_market": per_market,
        }

    def _card_text(
        self,
        thesis: ThesisFit,
        reference: MarketFitVerdict | None,
        reference_judgment: "_MarketJudgment | None" = None,
    ) -> tuple[str, str]:
        """Card captures/misses: the accepted advisory's text when the
        card narrates a recommendation (already vocabulary-gated by the
        merge gate), template-composed from check outcomes otherwise."""
        if (
            reference_judgment is not None
            and thesis.recommended_market_id
            and reference_judgment.advisory_meta.get("status") == "accepted"
        ):
            return (
                reference_judgment.advisory_meta["what_it_captures"],
                reference_judgment.advisory_meta["what_it_misses"],
            )
        if reference is None:
            captures = (
                "No eligible candidate markets were available to evaluate."
            )
            misses = (
                "Nothing was evaluated; a draft contract is the cheapest "
                "test to resolve this claim."
            )
        else:
            passes = [
                o.name for o in reference.checks if o.status.value == "pass"
            ]
            caps = [o for o in reference.checks if o.cap is not None]
            if thesis.recommended_market_id:
                captures = (
                    f"Deterministic conditions met on {reference.market_id}: "
                    + ", ".join(passes)
                    + "."
                )
                misses = (
                    "; ".join(o.detail for o in caps)
                    if caps
                    else (
                        "No deterministic mismatches. Metric identity rests "
                        "on lexical overlap, not proof — read the resolution "
                        "rules before recording intent."
                    )
                )
            else:
                captures = (
                    f"No recommended expression: best candidate "
                    f"{reference.market_id} reaches "
                    f"{reference.published.value}."
                )
                misses = "; ".join(o.detail for o in caps) or (
                    "Stacked condition failures left no usable expression."
                )
        for text in (captures, misses):
            violations = vocabulary_violations(text)
            if violations:
                raise ValueError(
                    f"fit-card template produced restricted vocabulary: "
                    f"{violations} — fix the template, not the filter"
                )
        return captures, misses

    def _rejection_reason(self, verdict: MarketFitVerdict) -> str:
        fired = [o for o in verdict.checks if o.cap is not None]
        reason = (
            f"{verdict.published.value}: "
            + ("; ".join(o.detail for o in fired) if fired else "outranked")
        )
        violations = vocabulary_violations(reason)
        if violations:
            raise ValueError(
                f"rejection reason produced restricted vocabulary: "
                f"{violations}"
            )
        return reason

    def _rules_capture(
        self, session: Session, market_id: str, snapshot_id: str
    ) -> MarketRulesCapture | None:
        return session.execute(
            select(MarketRulesCapture).where(
                MarketRulesCapture.market_id == market_id,
                MarketRulesCapture.snapshot_id == snapshot_id,
            )
        ).scalar_one_or_none()
