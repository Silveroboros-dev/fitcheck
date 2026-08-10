"""MCP tools — thin composition over the existing services (step 7).

No new fit/ledger/odds/learning logic: each tool marshals to the Loop 1-3 +
ledger services and shapes a response model. Auth is resolved upstream (the
server) into a Principal; the actor_id on that Principal is what the odds lock
is scoped to. Every returned model passes the field-aware A7 guard.
"""

import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.contracts import ThesisAnalysisIn
from el.domain.enums import (
    ClientType,
    ConvictionLevel,
    ExposureBucket,
    PriorConfidence,
    ReviewSource,
    ReviewStatus,
)
from el.domain.tables import (
    CandidateSet,
    DraftContract,
    FitCard,
    LedgerEntry,
    MarketRecommendation,
    RejectedMarketRow,
    ReviewCandidate,
    ThesisAnalysis,
)
from el.mcp.auth import Principal
from el.mcp.contracts import (
    BlindPriorRequired,
    BlindPriorResult,
    DraftResult,
    FitCardResult,
    LedgerEntryResult,
    NormalizeResult,
    NotFound,
    RejectedMarketOut,
    ReviewCandidateResult,
    SaveRejected,
    ToolRefused,
)
from el.mcp.vocab_guard import assert_a7_clean

# client_ref is session/idempotency metadata only — the odds lock authorizes
# on actor_id, never on this. Fixed for the MCP surface.
_MCP_CLIENT_REF = "mcp"

# Non-human MCP surfaces: their corrections/rejections enter review as
# AGENT_* sources (queryable by source alone), not the human user_* sources.
_AGENT_CLIENT_TYPES = frozenset({ClientType.AGENT_MCP, ClientType.API})


class McpTools:
    def __init__(self, *, extraction, retrieval, fit, draft, ledger, session_factory):
        self._extraction = extraction
        self._retrieval = retrieval
        self._fit = fit
        self._draft = draft
        self._ledger = ledger
        self._sessions: sessionmaker[Session] = session_factory

    def _guard(self, model):
        assert_a7_clean(model.model_dump(mode="json"), source_paths=model.SOURCE_PATHS)
        return model

    # --- object authorization (P0: no cross-actor access by guessed id) ----
    def _require_thesis_access(
        self, thesis_analysis_id: uuid.UUID, principal: Principal
    ) -> None:
        """The thesis must exist AND belong to the calling actor. Missing OR
        unauthorized -> identical NotFound (no existence leak)."""
        with self._sessions() as s:
            t = s.get(ThesisAnalysis, thesis_analysis_id)
        if (
            t is None
            or t.agent_client_id != principal.agent_client_id
            or t.client_type != principal.client_type.value
        ):
            raise NotFound("thesis_analysis not found")

    def _require_fit_card_access(
        self, fit_card_id: uuid.UUID, principal: Principal
    ) -> None:
        with self._sessions() as s:
            card = s.get(FitCard, fit_card_id)
            thesis_analysis_id = card.thesis_analysis_id if card else None
        if thesis_analysis_id is None:
            raise NotFound("fit_card not found")
        self._require_thesis_access(thesis_analysis_id, principal)

    # 1 — normalize_claim (free tier; authenticated, no prior needed) --------
    def normalize_claim(self, principal: Principal, *, input_text: str) -> NormalizeResult:
        outcome = self._extraction.analyze(
            ThesisAnalysisIn(
                input_text=input_text,
                client_type=principal.client_type,
                agent_client_id=principal.agent_client_id,
            )
        )
        if outcome.analysis is None:
            raise ToolRefused(outcome.result.reasons)
        a = outcome.analysis
        return self._guard(
            NormalizeResult(
                thesis_analysis_id=a.id,
                normalized_claim_summary=a.normalized_claim_summary,
                extracted_structure=a.extracted_structure.model_dump(mode="json"),
                input_text=a.input_text,
            )
        )

    # 2 — preview_market_fit (pre-prior; odds withheld everywhere) -----------
    def preview_market_fit(
        self, principal: Principal, *, thesis_analysis_id: uuid.UUID
    ) -> FitCardResult:
        self._require_thesis_access(thesis_analysis_id, principal)
        outcome = self._run_fit(thesis_analysis_id)
        return self._guard(
            self._fit_result(
                outcome, principal, mcp_preview=True, current_odds=None, odds_side=None
            )
        )

    # 3 — draft_contract_preview --------------------------------------------
    def draft_contract_preview(
        self, principal: Principal, *, fit_card_id: uuid.UUID
    ) -> DraftResult:
        self._require_fit_card_access(fit_card_id, principal)
        outcome = self._draft.generate(fit_card_id)
        if not outcome.generated:
            return self._guard(
                DraftResult(generated=False, gate_verdict=outcome.gate_verdict)
            )
        with self._sessions() as s:
            d = s.get(DraftContract, outcome.draft_contract_id)
            return self._guard(
                DraftResult(
                    generated=True,
                    gate_verdict="pass",
                    label="shape-valid draft candidate",
                    draft_contract_id=d.id,
                    proposed_title=d.proposed_title,
                    proposed_resolution_logic=d.proposed_resolution_logic,
                    resolution_source=d.resolution_source,
                    resolution_source_class=d.resolution_source_class,
                    resolution_deadline=d.resolution_deadline,
                    subject_entity=d.subject_entity,
                )
            )

    # 4 — submit_blind_prior -------------------------------------------------
    def submit_blind_prior(
        self,
        principal: Principal,
        *,
        thesis_analysis_id: uuid.UUID,
        prior_probability: float,
        prior_confidence: str | None = None,
        prior_reason: str | None = None,
    ) -> BlindPriorResult:
        self._require_thesis_access(thesis_analysis_id, principal)
        outcome = self._ledger.submit_blind_prior(
            thesis_analysis_id,
            client_type=principal.client_type,
            actor_id=principal.actor_id,
            client_ref=_MCP_CLIENT_REF,
            prior_probability=prior_probability,
            prior_confidence=(
                PriorConfidence(prior_confidence) if prior_confidence else None
            ),
            prior_reason=prior_reason,
            agent_client_id=principal.agent_client_id,
        )
        return self._guard(
            BlindPriorResult(
                thesis_analysis_id=thesis_analysis_id,
                conviction_event_id=outcome.conviction_event_id,
                status=outcome.status,
            )
        )

    # 5 — classify_market_fit (REQUIRES a blind prior) ----------------------
    def classify_market_fit(
        self, principal: Principal, *, thesis_analysis_id: uuid.UUID
    ) -> FitCardResult:
        self._require_thesis_access(thesis_analysis_id, principal)
        if not self._ledger.has_blind_prior(
            thesis_analysis_id,
            client_type=principal.client_type,
            actor_id=principal.actor_id,
        ):
            # Typed error — never a silent downgrade to preview.
            raise BlindPriorRequired(thesis_analysis_id)
        outcome = self._run_fit(thesis_analysis_id)
        reveal = self._ledger.reveal_current_odds(
            outcome.fit_card_id,
            client_type=principal.client_type,
            actor_id=principal.actor_id,
            client_ref=_MCP_CLIENT_REF,
        )
        # reveal.revealed is False for a no-clean card (no market) — that is a
        # graceful no-odds classify, NOT a missing prior (already checked).
        return self._guard(
            self._fit_result(
                outcome,
                principal,
                mcp_preview=False,
                current_odds=reveal.current_odds,
                odds_side=reveal.side if reveal.revealed else None,
            )
        )

    # 6 — create_ledger_entry ------------------------------------------------
    def create_ledger_entry(
        self,
        principal: Principal,
        *,
        fit_card_id: uuid.UUID,
        conviction_level: str,
        intended_exposure_bucket: str,
        user_justification: str,
    ) -> LedgerEntryResult:
        self._require_fit_card_access(fit_card_id, principal)
        outcome = self._ledger.create_ledger_entry(
            fit_card_id,
            user_id=principal.user_id,  # always the caller — no cross-user write
            client_type=principal.client_type,
            actor_id=principal.actor_id,
            client_ref=_MCP_CLIENT_REF,
            conviction_level=ConvictionLevel(conviction_level),
            intended_exposure_bucket=ExposureBucket(intended_exposure_bucket),
            user_justification=user_justification,
            agent_client_id=principal.agent_client_id,
        )
        if not outcome.saved and outcome.status == "rejected_incomplete":
            raise SaveRejected(outcome.violations)
        return self._load_ledger_result(outcome.ledger_entry_id, principal)

    # 7/8 — get_ledger_entry / get_ledger_entries (user-scoped) -------------
    def get_ledger_entry(
        self, principal: Principal, *, ledger_entry_id: uuid.UUID
    ) -> LedgerEntryResult:
        return self._load_ledger_result(ledger_entry_id, principal)

    def get_ledger_entries(self, principal: Principal) -> list[LedgerEntryResult]:
        with self._sessions() as s:
            rows = s.scalars(
                select(LedgerEntry)
                .where(LedgerEntry.user_id == principal.user_id)
                .order_by(LedgerEntry.created_at.desc())
            ).all()
            return [self._guard(self._to_ledger_result(e)) for e in rows]

    def artifact_stats(self) -> dict[str, int]:
        """Observability (P2): every preview_market_fit / classify_market_fit
        persists a candidate_set + fit_card — there is no ephemeral path, so
        repeated calls accumulate. Counts are read from current state (durable),
        so an operator can monitor growth before real agent usage; an actual
        cleanup/TTL is a deliberate pre-external follow-up. review_candidates
        counts correction/rejection intake — the Step-8 review fuel — so an
        operator can see whether any is being produced yet."""
        with self._sessions() as s:
            return {
                "candidate_sets": s.scalar(
                    select(func.count()).select_from(CandidateSet)
                )
                or 0,
                "fit_cards": s.scalar(select(func.count()).select_from(FitCard))
                or 0,
                "review_candidates": s.scalar(
                    select(func.count()).select_from(ReviewCandidate)
                )
                or 0,
            }

    # 9/10 — correct_fit / reject_market (candidate intake ONLY) ------------
    def correct_fit(
        self,
        principal: Principal,
        *,
        fit_card_id: uuid.UUID,
        corrected_class: str,
        notes: str | None = None,
    ) -> ReviewCandidateResult:
        return self._intake(
            principal,
            object_type="fit_card",
            object_id=fit_card_id,
            source=(
                ReviewSource.AGENT_CORRECTION
                if principal.client_type in _AGENT_CLIENT_TYPES
                else ReviewSource.USER_CORRECTION
            ),
            payload={"corrected_class": corrected_class, "notes": notes},
        )

    def reject_market(
        self,
        principal: Principal,
        *,
        fit_card_id: uuid.UUID,
        market_id: str,
        reason: str,
    ) -> ReviewCandidateResult:
        return self._intake(
            principal,
            object_type="market_rejection",
            object_id=fit_card_id,
            source=(
                ReviewSource.AGENT_REJECTION
                if principal.client_type in _AGENT_CLIENT_TYPES
                else ReviewSource.USER_REJECTION
            ),
            payload={"market_id": market_id, "reason": reason},
        )

    # --- internals ---------------------------------------------------------
    def _run_fit(self, thesis_analysis_id: uuid.UUID):
        retrieval = self._retrieval.retrieve_candidates(thesis_analysis_id)
        return self._fit.classify_fit(thesis_analysis_id, retrieval.candidate_set_id)

    def _fit_result(
        self, outcome, principal: Principal, *, mcp_preview, current_odds, odds_side
    ) -> FitCardResult:
        with self._sessions() as s:
            card = s.get(FitCard, outcome.fit_card_id)
            prov = card.provenance or {}
            # Trimmed, ODDS-FREE provenance + the required preview markers.
            provenance = {
                "gate_policy_version": prov.get("gate_policy_version"),
                "authority": prov.get("authority"),
                "confidence_source": prov.get("confidence_source"),
                "thesis_side": prov.get("thesis_side"),
                "eval_pack_version": prov.get("eval_pack_version"),
                "mcp_preview": mcp_preview,
                "odds_withheld": mcp_preview,
                "client_type": principal.client_type.value,
            }
            return FitCardResult(
                fit_card_id=card.id,
                thesis_analysis_id=card.thesis_analysis_id,
                candidate_set_id=card.candidate_set_id,
                semantic_fit_class=card.semantic_fit_class,
                recommended_market_id=card.recommended_market_id,
                current_odds=current_odds,
                odds_side=odds_side,
                what_it_captures=card.what_it_captures,
                what_it_misses=card.what_it_misses,
                horizon_match=card.horizon_match,
                resolution_risk=card.resolution_risk,
                fit_confidence=card.fit_confidence,
                draft_contract_recommended=outcome.draft_contract_recommended,
                rejected_markets=self._rejected_markets(s, card.id),
                provenance=provenance,
            )

    def _rejected_markets(
        self,
        session: Session,
        fit_card_id: uuid.UUID,
    ) -> list[RejectedMarketOut]:
        rec = session.scalar(
            select(MarketRecommendation).where(
                MarketRecommendation.fit_card_id == fit_card_id
            )
        )
        # Unbound legacy cards fail closed; thesis-wide "latest" selection
        # could borrow rejection evidence from a different classification run.
        if rec is None:
            return []
        rows = session.scalars(
            select(RejectedMarketRow).where(
                RejectedMarketRow.market_recommendation_id == rec.id
            )
        ).all()
        return [RejectedMarketOut(market_id=r.market_id, reason=r.reason) for r in rows]

    def _load_ledger_result(
        self, ledger_entry_id: uuid.UUID, principal: Principal
    ) -> LedgerEntryResult:
        with self._sessions() as s:
            entry = s.get(LedgerEntry, ledger_entry_id)
            # Missing OR not owned -> identical NotFound (no existence leak).
            if entry is None or entry.user_id != principal.user_id:
                raise NotFound("ledger_entry not found")
            return self._guard(self._to_ledger_result(entry))

    def _to_ledger_result(self, entry: LedgerEntry) -> LedgerEntryResult:
        return LedgerEntryResult(
            id=entry.id,
            thesis_analysis_id=entry.thesis_analysis_id,
            thesis_summary=entry.thesis_summary,
            user_justification=entry.user_justification,
            linked_market_id=entry.linked_market_id,
            odds_at_entry=entry.odds_at_entry,
            odds_at_entry_side=entry.odds_at_entry_side,
            fit_class=entry.fit_class,
            attestation_status=entry.attestation_status,
            status=entry.status,
            client_type=entry.client_type,
            created_at=entry.created_at,
        )

    def _intake(
        self, principal: Principal, *, object_type, object_id, source, payload
    ) -> ReviewCandidateResult:
        self._require_fit_card_access(object_id, principal)
        # Deterministic note encodes actor + client + payload; candidate intake
        # ONLY (no promotion), and the response echoes no free text.
        note = json.dumps(
            {
                "actor_id": principal.actor_id,
                "client_type": principal.client_type.value,
                **{k: v for k, v in payload.items() if v is not None},
            },
            sort_keys=True,
        )
        with self._sessions() as s:
            # Idempotency (P1): a repeated identical correction/rejection
            # (same actor + object + source + payload) returns the existing
            # candidate instead of creating a duplicate row.
            for c in s.scalars(
                select(ReviewCandidate).where(
                    ReviewCandidate.object_type == object_type,
                    ReviewCandidate.object_id == object_id,
                    ReviewCandidate.source == source.value,
                )
            ).all():
                if c.reviewer_notes == note:
                    return self._guard(
                        ReviewCandidateResult(
                            review_candidate_id=c.id,
                            object_type=object_type,
                            object_id=object_id,
                            source=source.value,
                            status=c.status,
                        )
                    )
            row = ReviewCandidate(
                object_type=object_type,
                object_id=object_id,
                source=source.value,
                status=ReviewStatus.PENDING.value,
                reviewer_notes=note,
            )
            s.add(row)
            s.flush()
            result = ReviewCandidateResult(
                review_candidate_id=row.id,
                object_type=object_type,
                object_id=object_id,
                source=source.value,
                status=ReviewStatus.PENDING.value,
            )
            s.commit()
            return self._guard(result)
