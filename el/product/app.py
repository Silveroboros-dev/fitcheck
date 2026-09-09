"""Product-UI FastAPI app (agent-guided-ui-contract-v0).

Thin HTTP shell over ``ProductApi``: routes, typed-error → status mapping,
and the single static page. Loop semantics live in the services; the typed
errors carry the contract's explicit failure states.
"""

import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from el.extraction.service import NormalizationAttemptConflict
from el.marketpool.service import MarketPoolConflict
from el.mcp.contracts import (
    BlindPriorRequired,
    NotFound,
    SaveRejected,
    ToolRefused,
)
from el.product.api import ProductApi, ProposerUnavailable
from el.product.wiring import build_services, local_actor
from el.sourceinterpretation.service import SourceCandidateChoiceConflict

STATIC_HTML = Path(__file__).resolve().parent / "static" / "product_console.html"


class IntakeIn(BaseModel):
    input_text: str = Field(min_length=1, max_length=20_000)


class NormalizationProposalIn(IntakeIn):
    source_url: str | None = Field(default=None, max_length=2_048)


class SourceCandidateChoiceIn(BaseModel):
    selection_kind: Literal["candidate", "none"]
    source_thesis_candidate_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=2_000)


class NormalizationDecisionIn(BaseModel):
    expected_input_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class NormalizationRevisionIn(NormalizationDecisionIn):
    input_text: str = Field(min_length=1, max_length=20_000)
    source_url: str | None = Field(default=None, max_length=2_048)


class NormalizationRejectIn(NormalizationDecisionIn):
    reason: str | None = Field(default=None, max_length=2_000)


class MarketChoiceIn(BaseModel):
    selection_kind: Literal["market", "none"]
    market_assessment_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=2_000)


class MarketAssessmentFeedbackIn(BaseModel):
    feedback_kind: Literal["wrong_class", "not_an_expression", "other"]
    note: str = Field(min_length=1, max_length=2_000)


class BlindPriorIn(BaseModel):
    prior_probability: float = Field(ge=0.0, le=1.0)
    prior_confidence: str | None = None
    prior_reason: str | None = None


class SaveIn(BaseModel):
    conviction_level: str
    intended_exposure_bucket: str
    user_justification: str


class CorrectIn(BaseModel):
    corrected_class: str
    note: str = Field(min_length=1, max_length=2_000)


class RejectMarketIn(BaseModel):
    market_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2_000)


def build_app(api: ProductApi) -> FastAPI:
    app = FastAPI(title="FitCheck", version="product_ui_v3")

    @app.exception_handler(ToolRefused)
    async def _refused(_, exc: ToolRefused):
        return JSONResponse(
            status_code=422,
            content={"error": "refused", "reasons": exc.reasons},
        )

    @app.exception_handler(BlindPriorRequired)
    async def _prior_required(_, exc: BlindPriorRequired):
        return JSONResponse(
            status_code=409,
            content={
                "error": "blind_prior_required",
                "thesis_analysis_id": str(exc.thesis_analysis_id),
            },
        )

    @app.exception_handler(NotFound)
    async def _not_found(_, exc: NotFound):
        return JSONResponse(status_code=404, content={"error": "not_found"})

    @app.exception_handler(SaveRejected)
    async def _save_rejected(_, exc: SaveRejected):
        return JSONResponse(
            status_code=422,
            content={"error": "save_rejected", "violations": exc.violations},
        )

    @app.exception_handler(ProposerUnavailable)
    async def _unavailable(_, exc: ProposerUnavailable):
        return JSONResponse(
            status_code=503,
            content={"error": "service_unavailable", "detail": exc.detail},
        )

    @app.exception_handler(NormalizationAttemptConflict)
    async def _normalization_conflict(_, exc: NormalizationAttemptConflict):
        return JSONResponse(
            status_code=409,
            content={"error": "normalization_conflict", "detail": str(exc)},
        )

    @app.exception_handler(MarketPoolConflict)
    async def _market_pool_conflict(_, exc: MarketPoolConflict):
        return JSONResponse(
            status_code=409,
            content={"error": "market_pool_conflict", "detail": str(exc)},
        )

    @app.exception_handler(SourceCandidateChoiceConflict)
    async def _source_choice_conflict(_, exc: SourceCandidateChoiceConflict):
        return JSONResponse(
            status_code=409,
            content={"error": "source_choice_conflict", "detail": str(exc)},
        )

    @app.exception_handler(ValueError)
    async def _value_error(_, exc: ValueError):
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_argument", "detail": str(exc)},
        )

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return STATIC_HTML.read_text(encoding="utf-8")

    @app.get("/api/health")
    def health():
        return {"ok": True, **api.stats()}

    @app.post("/api/thesis")
    def intake(payload: IntakeIn):
        return api.intake(payload.input_text).model_dump(mode="json")

    @app.post("/api/v3/normalization-attempts")
    def propose_normalization(payload: NormalizationProposalIn):
        return api.propose_normalization(
            payload.input_text, source_url=payload.source_url
        ).model_dump(mode="json")

    @app.post("/api/v3/source-interpretations")
    def interpret_source(payload: NormalizationProposalIn):
        return api.interpret_source(
            payload.input_text, source_url=payload.source_url
        ).model_dump(mode="json")

    @app.post("/api/v3/source-interpretations/{interpretation_id}/choice")
    def choose_source_candidate(
        interpretation_id: uuid.UUID, payload: SourceCandidateChoiceIn
    ):
        return api.choose_source_candidate(
            interpretation_id,
            selection_kind=payload.selection_kind,
            source_thesis_candidate_id=payload.source_thesis_candidate_id,
            reason=payload.reason,
        ).model_dump(mode="json")

    @app.post(
        "/api/v3/source-thesis-candidates/{candidate_id}/normalization-attempt"
    )
    def normalize_source_candidate(candidate_id: uuid.UUID):
        return api.propose_selected_normalization(candidate_id).model_dump(
            mode="json"
        )

    @app.post("/api/v3/normalization-attempts/{attempt_id}/accept")
    def accept_normalization(
        attempt_id: uuid.UUID, payload: NormalizationDecisionIn
    ):
        return api.accept_normalization(
            attempt_id,
            expected_input_digest=payload.expected_input_digest,
        ).model_dump(mode="json")

    @app.post("/api/v3/normalization-attempts/{attempt_id}/revise")
    def revise_normalization(
        attempt_id: uuid.UUID, payload: NormalizationRevisionIn
    ):
        return api.revise_normalization(
            attempt_id,
            input_text=payload.input_text,
            source_url=payload.source_url,
            expected_input_digest=payload.expected_input_digest,
        ).model_dump(mode="json")

    @app.post("/api/v3/normalization-attempts/{attempt_id}/reject")
    def reject_normalization(
        attempt_id: uuid.UUID, payload: NormalizationRejectIn
    ):
        return api.reject_normalization(
            attempt_id,
            expected_input_digest=payload.expected_input_digest,
            reason=payload.reason,
        ).model_dump(mode="json")

    @app.post("/api/v3/theses/{thesis_analysis_id}/market-pool")
    def assess_market_pool(thesis_analysis_id: uuid.UUID):
        return api.assess_market_pool(thesis_analysis_id).model_dump(
            mode="json"
        )

    @app.post("/api/v3/market-pools/{market_display_set_id}/choice")
    def choose_market(
        market_display_set_id: uuid.UUID, payload: MarketChoiceIn
    ):
        return api.choose_market(
            market_display_set_id,
            selection_kind=payload.selection_kind,
            market_assessment_id=payload.market_assessment_id,
            reason=payload.reason,
        ).model_dump(mode="json")

    @app.post(
        "/api/v3/market-assessments/{market_assessment_id}/feedback"
    )
    def assessment_feedback(
        market_assessment_id: uuid.UUID,
        payload: MarketAssessmentFeedbackIn,
    ):
        return api.record_assessment_feedback(
            market_assessment_id,
            feedback_kind=payload.feedback_kind,
            note=payload.note,
        ).model_dump(mode="json")

    @app.post("/api/thesis/{thesis_analysis_id}/blind-prior")
    def blind_prior(thesis_analysis_id: uuid.UUID, payload: BlindPriorIn):
        return api.submit_blind_prior(
            thesis_analysis_id,
            prior_probability=payload.prior_probability,
            prior_confidence=payload.prior_confidence,
            prior_reason=payload.prior_reason,
        ).model_dump(mode="json")

    @app.post("/api/thesis/{thesis_analysis_id}/classify")
    def classify(thesis_analysis_id: uuid.UUID):
        return api.classify(thesis_analysis_id).model_dump(mode="json")

    @app.post("/api/fit-card/{fit_card_id}/draft-preview")
    def draft_preview(fit_card_id: uuid.UUID):
        return api.draft_preview(fit_card_id).model_dump(mode="json")

    @app.post("/api/fit-card/{fit_card_id}/ledger")
    def save_entry(fit_card_id: uuid.UUID, payload: SaveIn):
        return api.save_ledger_entry(
            fit_card_id,
            conviction_level=payload.conviction_level,
            intended_exposure_bucket=payload.intended_exposure_bucket,
            user_justification=payload.user_justification,
        ).model_dump(mode="json")

    # Correction loop (docs/correction-loop-ui-contract-v0.md): intake only —
    # mints pending review candidates, never mutates the card or any truth.
    @app.post("/api/fit-card/{fit_card_id}/correct")
    def correct_fit(fit_card_id: uuid.UUID, payload: CorrectIn):
        return api.correct_fit(
            fit_card_id,
            corrected_class=payload.corrected_class,
            note=payload.note,
        ).model_dump(mode="json")

    @app.post("/api/fit-card/{fit_card_id}/reject-market")
    def reject_market(fit_card_id: uuid.UUID, payload: RejectMarketIn):
        return api.reject_market(
            fit_card_id,
            market_id=payload.market_id,
            reason=payload.reason,
        ).model_dump(mode="json")

    @app.get("/api/ledger")
    def ledger_list():
        return {
            "entries": [
                e.model_dump(mode="json") for e in api.list_ledger_entries()
            ]
        }

    @app.get("/api/ledger/{ledger_entry_id}")
    def ledger_detail(ledger_entry_id: uuid.UUID):
        return api.get_ledger_entry(ledger_entry_id).model_dump(mode="json")

    return app


def build_default_app() -> FastAPI:
    services = build_services()
    actor = local_actor(services.session_factory)
    return build_app(ProductApi(services, actor))


def __getattr__(name: str):
    # Lazy `el.product.app:app` for uvicorn — importing this module must not
    # create the local DB as a side effect (tests inject their own factory).
    if name == "app":
        return build_default_app()
    raise AttributeError(name)
