"""Draft-contract service: propose -> gate -> persist + back-link (step 5b).

Blueprint §13. Runs downstream of Loop 3 (FitService): a fit card that
landed weak_proxy or no_clean_expression carries draft_contract_recommended,
and this service turns that flag into the productized "cheapest test to
resolve the claim" (Popperian reframe, blueprint Appendix A).

Flow: load the card -> guard (only weak/no-clean recommend a draft;
already-linked cards are idempotent no-ops) -> load the claim structure +
the fit gate's rejection reasons (fed to the proposer as anti-patterns:
the draft must fix what the rejections named) -> propose (retry budget 1)
-> deterministic draft gate -> on PASS persist the DraftContract with
complete provenance and back-link fit_cards.draft_contract_id; on failure
(proposer dead or gate rejection after the budget) degrade to a graceful
no-draft outcome — the card stays valid WITHOUT a draft, never broken.
"""

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.enums import FitClass
from el.domain.structures import ExtractedStructure
from el.domain.tables import (
    DraftContract,
    FitCard,
    MarketRecommendation,
    RejectedMarketRow,
    ThesisAnalysis,
)
from el.draftcontract.gate import (
    GATE_POLICY_VERSION,
    DraftGateResult,
    DraftGateVerdict,
    draft_contract_gate,
)
from el.models.draft_adapter import (
    DRAFT_PROPOSER_POLICY_VERSION,
    DraftContractProposer,
    DraftProposerResult,
)

DRAFT_EVAL_PACK_VERSION = "phase0-draftgen-v1"
PROPOSER_RETRY_BUDGET = 1  # one retry, then graceful no-draft

# Only these classes recommend a draft (A4: a recommended direct/indirect
# expression already exists, so there is nothing to draft).
_DRAFT_CLASSES = frozenset(
    {FitClass.WEAK_PROXY.value, FitClass.NO_CLEAN_EXPRESSION.value}
)


class DraftOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fit_card_id: uuid.UUID
    thesis_analysis_id: uuid.UUID
    generated: bool
    draft_contract_id: uuid.UUID | None
    # pass | rejected_invalid | proposer_failed | not_applicable |
    # already_present
    gate_verdict: str
    reasons: list[str]
    proposer_calls: int
    proposer_failures: int
    gate_policy_version: str = GATE_POLICY_VERSION


class DraftContractService:
    def __init__(
        self,
        proposer: DraftContractProposer,
        session_factory: sessionmaker[Session],
    ):
        self._proposer = proposer
        self._sessions = session_factory

    def generate(self, fit_card_id: uuid.UUID) -> DraftOutcome:
        with self._sessions() as session:
            card = session.get(FitCard, fit_card_id)
            if card is None:
                raise ValueError(f"fit_card {fit_card_id} not found")
            thesis_analysis_id = card.thesis_analysis_id

            if card.semantic_fit_class not in _DRAFT_CLASSES:
                return self._outcome(
                    card,
                    generated=False,
                    draft_contract_id=None,
                    gate_verdict="not_applicable",
                    reasons=[
                        f"card class {card.semantic_fit_class} does not "
                        "recommend a draft"
                    ],
                    calls=0,
                    failures=0,
                )
            if card.draft_contract_id is not None:
                return self._outcome(
                    card,
                    generated=False,
                    draft_contract_id=card.draft_contract_id,
                    gate_verdict="already_present",
                    reasons=["draft already generated for this card"],
                    calls=0,
                    failures=0,
                )

            claim = self._load_claim(session, thesis_analysis_id)
            rejection_reasons = self._rejection_reasons(session, card.id)

            calls = failures = 0
            last_reasons: list[str] = []
            for _ in range(1 + PROPOSER_RETRY_BUDGET):
                calls += 1
                try:
                    result = self._proposer.propose_draft(
                        thesis_analysis_id=thesis_analysis_id,
                        claim_structure=claim,
                        rejection_reasons=rejection_reasons,
                    )
                except Exception as error:  # a dead proposer must not break
                    failures += 1
                    last_reasons = [f"proposer error: {error}"]
                    continue
                gate = draft_contract_gate(claim=claim, proposed=result.draft)
                if gate.verdict is DraftGateVerdict.PASS:
                    draft = self._persist(
                        session,
                        card,
                        claim,
                        result,
                        gate,
                        rejection_reasons,
                        calls,
                        failures,
                    )
                    session.commit()
                    return self._outcome(
                        card,
                        generated=True,
                        draft_contract_id=draft.id,
                        gate_verdict="pass",
                        reasons=[],
                        calls=calls,
                        failures=failures,
                    )
                last_reasons = gate.reasons

            # Graceful no-draft: the card is valid without one.
            return self._outcome(
                card,
                generated=False,
                draft_contract_id=None,
                gate_verdict=(
                    "proposer_failed" if failures == calls else "rejected_invalid"
                ),
                reasons=last_reasons,
                calls=calls,
                failures=failures,
            )

    # --- internals -----------------------------------------------------

    def _outcome(
        self,
        card: FitCard,
        *,
        generated: bool,
        draft_contract_id: uuid.UUID | None,
        gate_verdict: str,
        reasons: list[str],
        calls: int,
        failures: int,
    ) -> DraftOutcome:
        return DraftOutcome(
            fit_card_id=card.id,
            thesis_analysis_id=card.thesis_analysis_id,
            generated=generated,
            draft_contract_id=draft_contract_id,
            gate_verdict=gate_verdict,
            reasons=reasons,
            proposer_calls=calls,
            proposer_failures=failures,
        )

    def _persist(
        self,
        session: Session,
        card: FitCard,
        claim: ExtractedStructure,
        result: DraftProposerResult,
        gate: DraftGateResult,
        rejection_reasons: list[str],
        calls: int,
        failures: int,
    ) -> DraftContract:
        proposed = gate.draft  # the re-validated draft, never the raw input
        assert proposed is not None
        draft = DraftContract(
            thesis_analysis_id=card.thesis_analysis_id,
            proposed_title=proposed.proposed_title,
            proposed_resolution_logic=proposed.proposed_resolution_logic,
            resolution_source=proposed.resolution_source,
            category=proposed.category,
            time_horizon=proposed.time_horizon,
            # First-class gate-verified echo fields (audit substance).
            resolution_deadline=proposed.resolution_deadline,
            resolution_source_class=proposed.resolution_source_class.value,
            subject_entity=proposed.subject_entity,
            provenance=self._provenance(
                claim, result, gate, rejection_reasons, calls, failures
            ),
        )
        session.add(draft)
        session.flush()  # assign draft.id before the back-link
        card.draft_contract_id = draft.id
        return draft

    def _provenance(
        self,
        claim: ExtractedStructure,
        result: DraftProposerResult,
        gate: DraftGateResult,
        rejection_reasons: list[str],
        calls: int,
        failures: int,
    ) -> dict:
        proposed = gate.draft
        assert proposed is not None
        return {
            "gate_policy_version": GATE_POLICY_VERSION,
            "proposer_policy_version": DRAFT_PROPOSER_POLICY_VERSION,
            "extraction_schema_version": claim.schema_version,
            "model_adapter": result.model_adapter,
            "model_run_id": result.model_run_id,
            # Run UUID until Phoenix wiring lands (blueprint §9).
            "trace_id": str(uuid.uuid4()),
            "eval_pack_version": DRAFT_EVAL_PACK_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "token_rules_version": gate.token_rules_version,
            # Echo fields the gate verified — the anti-mad-libs evidence.
            "echo": {
                "resolution_deadline": proposed.resolution_deadline.isoformat(),
                "subject_entity": proposed.subject_entity,
                "resolution_source_class": proposed.resolution_source_class.value,
                "event_stage": proposed.event_stage.value,
            },
            "checks": [
                {
                    "check_id": c.check_id,
                    "name": c.name,
                    "status": c.status,
                    "detail": c.detail,
                }
                for c in gate.checks
            ],
            # What the draft was asked to fix (validated rejections are
            # first-class data).
            "rejection_reasons_input": rejection_reasons,
            "proposer_calls": calls,
            "proposer_failures": failures,
        }

    def _load_claim(
        self, session: Session, thesis_analysis_id: uuid.UUID
    ) -> ExtractedStructure:
        analysis = session.get(ThesisAnalysis, thesis_analysis_id)
        if analysis is None:
            raise ValueError(f"thesis_analysis {thesis_analysis_id} not found")
        claim = ExtractedStructure.model_validate(analysis.extracted_structure)
        if claim.schema_version != 1:
            raise ValueError(
                f"draftgen-v1 understands extraction schema v1, got "
                f"v{claim.schema_version}"
            )
        return claim

    def _rejection_reasons(
        self,
        session: Session,
        fit_card_id: uuid.UUID,
    ) -> list[str]:
        """The exact fit card's validated rejection reasons.

        An unbound legacy card returns no reasons instead of borrowing another
        run's recommendation by thesis identity.
        """
        recommendation = session.scalar(
            select(MarketRecommendation).where(
                MarketRecommendation.fit_card_id == fit_card_id
            )
        )
        if recommendation is None:
            return []
        rows = session.scalars(
            select(RejectedMarketRow).where(
                RejectedMarketRow.market_recommendation_id == recommendation.id
            )
        ).all()
        return [row.reason for row in rows]
