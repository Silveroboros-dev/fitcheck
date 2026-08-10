"""Product-UI API layer — human-flavored composition over the loop services.

Mirrors ``el.mcp.tools.McpTools`` for a local human_ui actor: no new
fit/ledger/odds logic, marshaling only. Reuses the typed loop errors and the
A7 vocabulary guard from ``el.mcp`` (they are loop semantics, not transport
semantics). Differences from the MCP surface, per
docs/agent-guided-ui-contract-v0.md:

- actor is the single local human (client_type=human_ui; saves are attested);
- client_ref is ``product_ui`` (honest provenance, never "mcp");
- the fit response separates the gate verdict from candidate metadata and
  carries the recommended market's title/rules snapshot for the card;
- fixture-mode proposer misses surface as an explicit unavailable state.
"""

import json
import uuid
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.contracts import ThesisAnalysisIn
from el.domain.enums import (
    ConvictionLevel,
    ExposureBucket,
    FitClass,
    PriorConfidence,
    ReviewSource,
    ReviewStatus,
)
from el.domain.tables import (
    CandidateSetMember,
    FitCard,
    LedgerEntry,
    MarketRecommendation,
    MarketRulesCapture,
    RejectedMarketRow,
    ReviewCandidate,
    ThesisAnalysis,
)
from el.mcp.contracts import (
    BlindPriorRequired,
    NotFound,
    SaveRejected,
    ToolRefused,
)
from el.mcp.vocab_guard import assert_a7_clean
from el.product.wiring import CLIENT_REF, HumanActor, ProductServices


class ProposerUnavailable(Exception):
    """Model service unavailable (dead adapter, missing credentials, or a
    fixture-mode input outside the fixture set). Explicit state — AC-7."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset()


class IntakeResult(_Out):
    thesis_analysis_id: uuid.UUID
    normalized_claim_summary: str
    extracted_structure: dict
    input_text: str
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"input_text", "extracted_structure.entities[].name"}
    )


class BlindPriorOut(_Out):
    thesis_analysis_id: uuid.UUID
    conviction_event_id: uuid.UUID | None
    status: str


class MarketSnapshotOut(_Out):
    market_id: str
    title: str | None
    resolution_rules: str | None
    # Market text is quoted venue source, not system copy.
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"title", "resolution_rules"}
    )


class RejectedMarketUiOut(_Out):
    market_id: str
    title: str | None
    reason: str
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset({"title"})


class CandidateEvidenceOut(_Out):
    """Retrieval provenance — candidate evidence, never the verdict (AC-3)."""

    candidate_set_id: uuid.UUID
    snapshot_id: str
    candidate_count: int
    rejected_count: int
    skipped_ineligible: int
    structures_extracted: int


class FitCardOut(_Out):
    fit_card_id: uuid.UUID
    thesis_analysis_id: uuid.UUID
    semantic_fit_class: str
    recommended_market_id: str | None
    recommended_market: MarketSnapshotOut | None
    current_odds: float | None
    odds_side: str | None
    what_it_captures: str | None
    what_it_misses: str | None
    horizon_match: str | None
    resolution_risk: str | None
    fit_confidence: float | None
    draft_contract_recommended: bool
    rejected_markets: list[RejectedMarketUiOut]
    candidate_evidence: CandidateEvidenceOut
    provenance: dict
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {
            "recommended_market.title",
            "recommended_market.resolution_rules",
            "rejected_markets[].title",
        }
    )


class DraftPreviewOut(_Out):
    generated: bool
    gate_verdict: str | None
    label: str | None = None
    draft_contract_id: uuid.UUID | None = None
    proposed_title: str | None = None
    proposed_resolution_logic: str | None = None
    resolution_source: str | None = None
    resolution_deadline: str | None = None
    subject_entity: str | None = None


class LedgerEntryUiOut(_Out):
    id: uuid.UUID
    thesis_analysis_id: uuid.UUID | None
    thesis_summary: str | None
    user_justification: str | None
    linked_market_id: str | None
    linked_market_title: str | None
    odds_at_entry: float | None
    odds_at_entry_side: str | None
    fit_class: str | None
    attestation_status: str | None
    status: str | None
    client_type: str | None
    created_at: str | None
    SOURCE_PATHS: ClassVar[frozenset[str]] = frozenset(
        {"user_justification", "linked_market_title"}
    )


class CorrectionOut(_Out):
    """Acknowledgment only: the card this correction is about is unchanged."""

    review_candidate_id: uuid.UUID
    status: str
    already_recorded: bool


class ProductApi:
    def __init__(self, services: ProductServices, actor: HumanActor):
        self._s = services
        self._actor = actor
        self._sessions: sessionmaker[Session] = services.session_factory

    @property
    def mode(self) -> str:
        return self._s.mode

    def _guard(self, model: _Out) -> _Out:
        assert_a7_clean(model.model_dump(mode="json"), source_paths=model.SOURCE_PATHS)
        return model

    # --- ownership (same no-existence-leak rule as the MCP surface) --------
    def _require_thesis_access(self, thesis_analysis_id: uuid.UUID) -> None:
        with self._sessions() as s:
            t = s.get(ThesisAnalysis, thesis_analysis_id)
        if (
            t is None
            or t.agent_client_id != self._actor.agent_client_id
            or t.client_type != self._actor.client_type.value
        ):
            raise NotFound("thesis_analysis not found")

    def _require_fit_card_access(self, fit_card_id: uuid.UUID) -> uuid.UUID:
        with self._sessions() as s:
            card = s.get(FitCard, fit_card_id)
            thesis_analysis_id = card.thesis_analysis_id if card else None
        if thesis_analysis_id is None:
            raise NotFound("fit_card not found")
        self._require_thesis_access(thesis_analysis_id)
        return thesis_analysis_id

    # --- S1 intake ----------------------------------------------------------
    def intake(self, input_text: str) -> IntakeResult:
        try:
            outcome = self._s.extraction.analyze(
                ThesisAnalysisIn(
                    input_text=input_text,
                    client_type=self._actor.client_type,
                    agent_client_id=self._actor.agent_client_id,
                )
            )
        except KeyError:
            raise ProposerUnavailable(
                "model service unavailable — fixture mode only recognizes the "
                "checked-in fixture theses; paste one of those or run in "
                "gemini mode"
            )
        if outcome.analysis is None:
            raise ToolRefused(outcome.result.reasons)
        a = outcome.analysis
        return self._guard(
            IntakeResult(
                thesis_analysis_id=a.id,
                normalized_claim_summary=a.normalized_claim_summary,
                extracted_structure=a.extracted_structure.model_dump(mode="json"),
                input_text=a.input_text,
            )
        )

    # --- S2 blind prior -----------------------------------------------------
    def submit_blind_prior(
        self,
        thesis_analysis_id: uuid.UUID,
        *,
        prior_probability: float,
        prior_confidence: str | None = None,
        prior_reason: str | None = None,
    ) -> BlindPriorOut:
        self._require_thesis_access(thesis_analysis_id)
        outcome = self._s.ledger.submit_blind_prior(
            thesis_analysis_id,
            client_type=self._actor.client_type,
            actor_id=self._actor.actor_id,
            client_ref=CLIENT_REF,
            prior_probability=prior_probability,
            prior_confidence=(
                PriorConfidence(prior_confidence) if prior_confidence else None
            ),
            prior_reason=prior_reason,
            agent_client_id=self._actor.agent_client_id,
        )
        return self._guard(
            BlindPriorOut(
                thesis_analysis_id=thesis_analysis_id,
                conviction_event_id=outcome.conviction_event_id,
                status=outcome.status,
            )
        )

    # --- S3 classify → Market Fit Card --------------------------------------
    def classify(self, thesis_analysis_id: uuid.UUID) -> FitCardOut:
        self._require_thesis_access(thesis_analysis_id)
        if not self._s.ledger.has_blind_prior(
            thesis_analysis_id,
            client_type=self._actor.client_type,
            actor_id=self._actor.actor_id,
        ):
            raise BlindPriorRequired(thesis_analysis_id)
        retrieval = self._s.retrieval.retrieve_candidates(thesis_analysis_id)
        outcome = self._s.fit.classify_fit(
            thesis_analysis_id, retrieval.candidate_set_id
        )
        reveal = self._s.ledger.reveal_current_odds(
            outcome.fit_card_id,
            client_type=self._actor.client_type,
            actor_id=self._actor.actor_id,
            client_ref=CLIENT_REF,
        )
        # Titles are retrieval-record data (frozen snapshot), not persisted on
        # structures; take them from THIS request's retrieval outcome so the
        # card can never show a title from another chain.
        titles = {c.market_id: c.title for c in retrieval.candidates}
        with self._sessions() as s:
            card = s.get(FitCard, outcome.fit_card_id)
            prov = card.provenance or {}
            candidate_count = (
                s.scalar(
                    select(func.count())
                    .select_from(CandidateSetMember)
                    .where(
                        CandidateSetMember.candidate_set_id
                        == outcome.candidate_set_id
                    )
                )
                or 0
            )
            result = FitCardOut(
                fit_card_id=card.id,
                thesis_analysis_id=card.thesis_analysis_id,
                semantic_fit_class=card.semantic_fit_class,
                recommended_market_id=card.recommended_market_id,
                recommended_market=self._market_snapshot(
                    s,
                    card.recommended_market_id,
                    titles=titles,
                    snapshot_id=retrieval.snapshot_id,
                ),
                current_odds=reveal.current_odds,
                odds_side=reveal.side if reveal.revealed else None,
                what_it_captures=card.what_it_captures,
                what_it_misses=card.what_it_misses,
                horizon_match=card.horizon_match,
                resolution_risk=card.resolution_risk,
                fit_confidence=card.fit_confidence,
                draft_contract_recommended=outcome.draft_contract_recommended,
                rejected_markets=self._rejected_markets(
                    s,
                    card.id,
                    titles=titles,
                ),
                candidate_evidence=CandidateEvidenceOut(
                    candidate_set_id=outcome.candidate_set_id,
                    snapshot_id=retrieval.snapshot_id,
                    candidate_count=candidate_count,
                    rejected_count=outcome.rejected_count,
                    skipped_ineligible=outcome.skipped_ineligible,
                    structures_extracted=outcome.structures_extracted,
                ),
                provenance={
                    "gate_policy_version": prov.get("gate_policy_version"),
                    "authority": prov.get("authority"),
                    "confidence_source": prov.get("confidence_source"),
                    "thesis_side": prov.get("thesis_side"),
                    "client_type": self._actor.client_type.value,
                    "client_ref": CLIENT_REF,
                },
            )
        return self._guard(result)

    def _market_snapshot(
        self,
        s: Session,
        market_id: str | None,
        *,
        titles: dict[str, str | None],
        snapshot_id: str,
    ) -> MarketSnapshotOut | None:
        if market_id is None:
            return None
        capture = s.scalars(
            select(MarketRulesCapture).where(
                MarketRulesCapture.market_id == market_id,
                MarketRulesCapture.snapshot_id == snapshot_id,
            )
        ).first()
        return MarketSnapshotOut(
            market_id=market_id,
            title=titles.get(market_id),
            resolution_rules=capture.resolution_rules_text if capture else None,
        )

    def _rejected_markets(
        self,
        s: Session,
        fit_card_id: uuid.UUID,
        *,
        titles: dict[str, str | None],
    ) -> list[RejectedMarketUiOut]:
        rec = s.scalar(
            select(MarketRecommendation).where(
                MarketRecommendation.fit_card_id == fit_card_id
            )
        )
        # Unbound legacy cards fail closed instead of borrowing another run's
        # rejection evidence through a thesis-wide "latest" query.
        if rec is None:
            return []
        rows = s.scalars(
            select(RejectedMarketRow).where(
                RejectedMarketRow.market_recommendation_id == rec.id
            )
        ).all()
        return [
            RejectedMarketUiOut(
                market_id=r.market_id,
                title=titles.get(r.market_id),
                reason=r.reason,
            )
            for r in rows
        ]

    # --- no-clean draft preview ---------------------------------------------
    def draft_preview(self, fit_card_id: uuid.UUID) -> DraftPreviewOut:
        self._require_fit_card_access(fit_card_id)
        outcome = self._s.draft.generate(fit_card_id)
        if not outcome.generated:
            return self._guard(
                DraftPreviewOut(generated=False, gate_verdict=outcome.gate_verdict)
            )
        from el.domain.tables import DraftContract

        with self._sessions() as s:
            d = s.get(DraftContract, outcome.draft_contract_id)
            return self._guard(
                DraftPreviewOut(
                    generated=True,
                    gate_verdict="pass",
                    label="shape-valid draft candidate",
                    draft_contract_id=d.id,
                    proposed_title=d.proposed_title,
                    proposed_resolution_logic=d.proposed_resolution_logic,
                    resolution_source=d.resolution_source,
                    resolution_deadline=(
                        d.resolution_deadline.isoformat()
                        if d.resolution_deadline
                        else None
                    ),
                    subject_entity=d.subject_entity,
                )
            )

    # --- S4 save ------------------------------------------------------------
    def save_ledger_entry(
        self,
        fit_card_id: uuid.UUID,
        *,
        conviction_level: str,
        intended_exposure_bucket: str,
        user_justification: str,
    ) -> LedgerEntryUiOut:
        self._require_fit_card_access(fit_card_id)
        outcome = self._s.ledger.create_ledger_entry(
            fit_card_id,
            user_id=self._actor.user_id,
            client_type=self._actor.client_type,
            actor_id=self._actor.actor_id,
            client_ref=CLIENT_REF,
            conviction_level=ConvictionLevel(conviction_level),
            intended_exposure_bucket=ExposureBucket(intended_exposure_bucket),
            user_justification=user_justification,
            agent_client_id=self._actor.agent_client_id,
        )
        if not outcome.saved and outcome.status == "rejected_incomplete":
            raise SaveRejected(outcome.violations)
        return self.get_ledger_entry(outcome.ledger_entry_id)

    # --- S5 read-back ---------------------------------------------------------
    def get_ledger_entry(self, ledger_entry_id: uuid.UUID) -> LedgerEntryUiOut:
        with self._sessions() as s:
            entry = s.get(LedgerEntry, ledger_entry_id)
            if entry is None or entry.user_id != self._actor.user_id:
                raise NotFound("ledger_entry not found")
            return self._guard(self._to_ledger_out(s, entry))

    def list_ledger_entries(self) -> list[LedgerEntryUiOut]:
        with self._sessions() as s:
            rows = s.scalars(
                select(LedgerEntry)
                .where(LedgerEntry.user_id == self._actor.user_id)
                .order_by(LedgerEntry.created_at.desc())
            ).all()
            return [self._guard(self._to_ledger_out(s, e)) for e in rows]

    def _to_ledger_out(self, s: Session, entry: LedgerEntry) -> LedgerEntryUiOut:
        # Titles are not persisted (frozen-snapshot record data); read-back
        # shows the durable market_id and leaves the title best-effort empty.
        snap = None
        return LedgerEntryUiOut(
            id=entry.id,
            thesis_analysis_id=entry.thesis_analysis_id,
            thesis_summary=entry.thesis_summary,
            user_justification=entry.user_justification,
            linked_market_id=entry.linked_market_id,
            linked_market_title=snap.title if snap else None,
            odds_at_entry=entry.odds_at_entry,
            odds_at_entry_side=entry.odds_at_entry_side,
            fit_class=entry.fit_class,
            attestation_status=entry.attestation_status,
            status=entry.status,
            client_type=entry.client_type,
            created_at=entry.created_at.isoformat() if entry.created_at else None,
        )

    # --- correction loop (docs/correction-loop-ui-contract-v0.md) ----------
    # Mirrors the MCP _intake semantics for the human actor: mint a PENDING
    # review_candidates row, dedupe on identical payloads, mutate NOTHING
    # else. Candidates leave pending only via the existing human triage
    # machinery, which this surface never calls.
    def correct_fit(
        self, fit_card_id: uuid.UUID, *, corrected_class: str, note: str
    ) -> "CorrectionOut":
        if not note.strip():
            raise ValueError("note is required")
        corrected = FitClass(corrected_class)  # closed vocabulary or ValueError
        self._require_fit_card_access(fit_card_id)
        return self._intake(
            object_type="fit_card",
            object_id=fit_card_id,
            source=ReviewSource.USER_CORRECTION,
            payload={"corrected_class": corrected.value, "notes": note.strip()},
        )

    def reject_market(
        self, fit_card_id: uuid.UUID, *, market_id: str, reason: str
    ) -> "CorrectionOut":
        if not reason.strip():
            raise ValueError("reason is required")
        self._require_fit_card_access(fit_card_id)
        with self._sessions() as s:
            card = s.get(FitCard, fit_card_id)
            surfaced = set()
            if card.recommended_market_id:
                surfaced.add(card.recommended_market_id)
            rec = s.scalar(
                select(MarketRecommendation).where(
                    MarketRecommendation.fit_card_id == card.id
                )
            )
            if rec is not None:
                surfaced.update(
                    r.market_id
                    for r in s.scalars(
                        select(RejectedMarketRow).where(
                            RejectedMarketRow.market_recommendation_id == rec.id
                        )
                    )
                )
        if market_id not in surfaced:
            raise ValueError("market was not part of this card")
        return self._intake(
            object_type="market_rejection",
            object_id=fit_card_id,
            source=ReviewSource.USER_REJECTION,
            payload={"market_id": market_id, "reason": reason.strip()},
        )

    def _intake(
        self, *, object_type: str, object_id: uuid.UUID, source: ReviewSource,
        payload: dict,
    ) -> "CorrectionOut":
        # Deterministic note encoding, matching the MCP intake shape, so the
        # Step-8 queue sees one provenance format regardless of surface.
        note = json.dumps(
            {
                "actor_id": self._actor.actor_id,
                "client_type": self._actor.client_type.value,
                **{k: v for k, v in payload.items() if v is not None},
            },
            sort_keys=True,
        )
        with self._sessions() as s:
            for c in s.scalars(
                select(ReviewCandidate).where(
                    ReviewCandidate.object_type == object_type,
                    ReviewCandidate.object_id == object_id,
                    ReviewCandidate.source == source.value,
                )
            ).all():
                if c.reviewer_notes == note:
                    return self._guard(
                        CorrectionOut(
                            review_candidate_id=c.id,
                            status=c.status,
                            already_recorded=True,
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
            result = CorrectionOut(
                review_candidate_id=row.id,
                status=ReviewStatus.PENDING.value,
                already_recorded=False,
            )
            s.commit()
            return self._guard(result)

    def stats(self) -> dict[str, Any]:
        with self._sessions() as s:
            return {
                "mode": self._s.mode,
                "ledger_entries": s.scalar(
                    select(func.count())
                    .select_from(LedgerEntry)
                    .where(LedgerEntry.user_id == self._actor.user_id)
                )
                or 0,
                "review_candidates": s.scalar(
                    select(func.count()).select_from(ReviewCandidate)
                )
                or 0,
            }
