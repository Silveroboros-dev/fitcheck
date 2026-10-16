"""Product-UI FastAPI app (agent-guided-ui-contract-v0).

Thin HTTP shell over ``ProductApi``: routes, typed-error → status mapping,
and the single static page. Loop semantics live in the services; the typed
errors carry the contract's explicit failure states.
"""

import uuid
from contextvars import ContextVar
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from el.extraction.service import NormalizationAttemptConflict
from el.jobs import IdempotencyConflict
from el.marketpool.service import MarketPoolConflict
from el.mcp.contracts import (
    BlindPriorRequired,
    NotFound,
    SaveRejected,
    ToolRefused,
)
from el.models.vertex_canary import VertexCanaryRefused
from el.observability import configure_structured_stderr_logging, extract_traceparent
from el.product.api import ProductApi, ProposerUnavailable
from el.product.wiring import ProductServices, build_services, local_actor
from el.product.demo_sessions import COOKIE_NAME, SESSION_SECONDS, DemoSessions
from el.revisit.service import RevisitConflict, RevisitNotFound, RevisitUnavailable
from el.sourceinterpretation.service import SourceCandidateChoiceConflict

if TYPE_CHECKING:
    from el.product.accounts import AccountResolver


STATIC_HTML = files("el.product").joinpath("static", "product_console.html")
SYNTHETIC_MARKET_FIXTURE_ASSETS = frozenset({
    "synthetic-market-stablecoin-total-cap.json",
    "synthetic-market-stablecoin-unit-cap.json",
    "synthetic-market-treasury-ledger-payment.json",
    "synthetic-market-broker-tokenized-asset.json",
    "synthetic-market-broker-first-tokenized.json",
    "synthetic-market-broker-chain-token.json",
})


class _RequestApi:
    """Resolve the trusted product actor for each request."""

    def __init__(self):
        self.current: ContextVar[ProductApi] = ContextVar("product_request_api")

    def __getattr__(self, name: str):
        return getattr(self.current.get(), name)


class IntakeIn(BaseModel):
    input_text: str = Field(min_length=1, max_length=20_000)


class NormalizationProposalIn(IntakeIn):
    source_url: str | None = Field(default=None, max_length=2_048)


class SourceInterpretationJobIn(NormalizationProposalIn):
    idempotency_key: str = Field(min_length=1, max_length=160)


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


class RevisitCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=160)
    fixture_scenario: Literal["unchanged", "rules_added", "unavailable"] | None = None
    capture_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def one_capture_source(self):
        if (self.fixture_scenario is None) == (self.capture_id is None):
            raise ValueError("select exactly one registered capture or fixture scenario")
        return self


class RevisitDecisionIn(BaseModel):
    action: Literal["dismiss", "reaffirm"]
    expected_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_observation_id: uuid.UUID


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


def _bound_fixture_impact(note: object, saved: dict) -> dict | None:
    """Optional fixture copy, outside immutable evidence and semantic assessments."""
    if not isinstance(note, dict):
        return None
    activation = saved.get("activation") or {}
    latest = saved.get("latest") or {}
    observation = latest.get("observation") or {}
    packet = latest.get("packet") or {}
    binding = latest.get("binding")
    if (not activation.get("active") or not binding
        or activation.get("binding") != binding
        or observation.get("result") != "revisit_proposed"
        or "rules_changed" not in observation.get("triggers", [])
        or not packet.get("id")):
        return None
    expected = {
        "binding": binding,
        "observation_id": observation.get("id"),
        "capture_id": observation.get("capture_id"),
        "capture_digest": observation.get("capture_digest"),
        "source_kind": observation.get("source_kind"),
        "source_ref": observation.get("source_ref"),
        "policy_version": observation.get("policy_version"),
        "packet_id": packet.get("id"),
        "packet_digest": packet.get("digest"),
        "before_rules_hash": (observation.get("before") or {}).get("rules_hash"),
        "after_rules_hash": (observation.get("after") or {}).get("rules_hash"),
        "version": "fixture-possible-impact-v1",
        "approval_scope": "copy_only_not_semantic_adjudication",
        "provenance": "Owner-approved fixture explanation",
    }
    if (set(note) != set(expected) | {"resolution_effect", "fit_effect"}
        or any(value is None or note[key] != value for key, value in expected.items())
        or any(not isinstance(note[key], str) or not 1 <= len(note[key]) <= 2000
               for key in ("resolution_effect", "fit_effect"))):
        return None
    return deepcopy(note)


def build_app(
    api: ProductApi | ProductServices, *, fixture_revisit_explanation: dict | None = None,
    account_resolver: "AccountResolver | None" = None,
) -> FastAPI:
    if account_resolver is not None and not isinstance(api, ProductServices):
        raise ValueError("account mode requires services, not a prebound actor")
    account_services = api if account_resolver is not None else None
    demo_services = api if isinstance(api, ProductServices) and account_resolver is None else None
    if account_services is not None and account_services.mode != "fixture":
        raise ValueError("offline account mode requires fixture services")
    if demo_services is not None and demo_services.mode != "fixture":
        raise ValueError("anonymous demo requires fixture mode")
    if fixture_revisit_explanation is not None and api.mode != "fixture":
        raise ValueError("fixture explanation is available only in fixture mode")
    fixture_note = deepcopy(fixture_revisit_explanation)
    request_api = _RequestApi() if demo_services is not None or account_services is not None else None
    if request_api is not None:
        api = request_api
    demo_sessions = DemoSessions(demo_services.session_factory) if demo_services else None
    configure_structured_stderr_logging()
    app = FastAPI(title="FitCheck", version="product_ui_v3")

    if account_services is not None:
        # The public fixture projection omits the private identity adapter.
        try:
            from el.product.accounts import IdentityDenied, account_route_allowed
        except ModuleNotFoundError as error:
            if error.name != "el.product.accounts":
                raise
            raise ValueError(
                "account mode is unavailable in this public distribution"
            ) from None

        @app.middleware("http")
        async def account_identity(request: Request, call_next):
            path = request.url.path
            if path == "/api/health" or not path.startswith("/api/"):
                return await call_next(request)
            if not account_route_allowed(request.method, path):
                return JSONResponse({"error": "not_found"}, status_code=404)
            if account_resolver.require_https and request.url.scheme != "https":
                return JSONResponse(
                    {"error": "cross_origin_denied"}, status_code=403
                )
            if request.method not in {"GET", "HEAD"}:
                origin = request.headers.get("origin")
                host_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
                if (
                    origin != host_origin
                    or request.headers.get("sec-fetch-site") == "cross-site"
                ):
                    return JSONResponse(
                        {"error": "cross_origin_denied"}, status_code=403
                    )
            try:
                actor = await account_resolver.resolve(request)
            except IdentityDenied:
                return JSONResponse({"error": "identity_denied"}, status_code=401)
            token = request_api.current.set(ProductApi(account_services, actor))
            try:
                response = await call_next(request)
            finally:
                request_api.current.reset(token)
            response.headers["Cache-Control"] = "no-store"
            return response

    if demo_sessions is not None:
        @app.middleware("http")
        async def demo_identity(request: Request, call_next):
            origin = request.headers.get("origin")
            host_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if request.method not in {"GET", "HEAD"} and (
                origin != host_origin
                or request.headers.get("sec-fetch-site") == "cross-site"
            ):
                return JSONResponse({"error": "cross_origin_denied"}, status_code=403)
            path = request.url.path
            if path.startswith("/api/") and not (
                path == "/api/health"
                or path == "/api/demo/reset"
                or path == "/api/demo/hormuz"
                or (path.startswith("/api/v3/") and not path.endswith("/feedback"))
            ):
                return JSONResponse({"error": "not_found"}, status_code=404)
            if (
                path.startswith("/assets/brand/")
                or path.startswith("/assets/demo-fixtures/markets/")
            ):
                # Static assets contain no visitor state. In particular, an
                # expired cookie must not prevent the page from rendering.
                return await call_next(request)
            cookie = request.cookies.get(COOKIE_NAME)
            resolved = demo_sessions.resolve(cookie)
            if cookie and resolved is None and path not in {"/", "/api/demo/reset"}:
                # Never send a deletion header for an old token: this response
                # can arrive after a reset response set the new cookie.
                return JSONResponse({"error": "session_expired"}, status_code=401)
            if path == "/api/demo/reset":
                if request.method != "POST":
                    return JSONResponse({"error": "not_found"}, status_code=404)
                if resolved is not None:
                    demo_sessions.revoke(cookie)
                actor, session_ref, new_cookie = demo_sessions.create()
                response = JSONResponse({"session_ref": session_ref})
                response.set_cookie(COOKIE_NAME, new_cookie, max_age=SESSION_SECONDS,
                                    httponly=True, samesite="lax", secure=request.url.scheme == "https")
                return response
            new_cookie = None
            if resolved is None:
                if request.method not in {"GET", "HEAD"}:
                    return JSONResponse({"error": "session_required"}, status_code=401)
                actor, session_ref, new_cookie = demo_sessions.create()
            else:
                actor, session_ref = resolved
            request.state.demo_session_ref = session_ref
            token = request_api.current.set(ProductApi(demo_services, actor))
            try:
                response = await call_next(request)
            finally:
                request_api.current.reset(token)
            if new_cookie is not None:
                response.set_cookie(COOKIE_NAME, new_cookie, max_age=SESSION_SECONDS,
                                    httponly=True, samesite="lax", secure=request.url.scheme == "https")
            response.headers["Cache-Control"] = "no-store"
            return response

    @app.on_event("shutdown")
    def _shutdown_source_telemetry() -> None:
        # Best effort only: telemetry shutdown cannot alter a committed job or
        # prompt another provider call.
        (account_services or demo_services or api._s).source_interpretation_jobs.telemetry.shutdown()

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

    @app.exception_handler(RevisitNotFound)
    async def _revisit_not_found(_, __):
        return JSONResponse(status_code=404, content={"error": "revisit_not_found"})

    @app.exception_handler(RevisitConflict)
    async def _revisit_conflict(_, exc: RevisitConflict):
        return JSONResponse(status_code=409, content={"error": "revisit_conflict", "detail": str(exc)})

    @app.exception_handler(RevisitUnavailable)
    async def _revisit_unavailable(_, exc: RevisitUnavailable):
        return JSONResponse(status_code=422, content={"error": "revisit_unavailable", "detail": str(exc)})

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

    @app.exception_handler(VertexCanaryRefused)
    async def _terminal_provider_operation(_, exc: VertexCanaryRefused):
        # A canary stage is intentionally non-retryable once reserved.  Do not
        # expose its private receipt paths, SDK exception, or provider body to
        # the browser, and do not make a local reservation look like a human
        # reject/none decision.
        return JSONResponse(status_code=503, content=exc.public_terminal_error())

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

    @app.exception_handler(IdempotencyConflict)
    async def _idempotency_conflict(_, exc: IdempotencyConflict):
        return JSONResponse(
            status_code=409,
            content={
                "error": "idempotency_conflict",
                "detail": "idempotency key conflicts with submitted source or pins",
            },
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

    @app.get("/assets/brand/{asset}")
    def brand_asset(asset: str):
        if asset not in {"fitcheck-logo-on-dark.svg", "fitcheck-logo-dark-panel.svg"}:
            return Response(status_code=404)
        data = files("el.product").joinpath("static", "brand", asset).read_bytes()
        return Response(data, media_type="image/svg+xml")

    @app.get("/assets/demo-fixtures/markets/{asset}")
    def synthetic_market_fixture_asset(asset: str):
        if asset not in SYNTHETIC_MARKET_FIXTURE_ASSETS:
            return Response(status_code=404)
        data = files("el.product.demo_fixtures").joinpath(
            "markets", asset
        ).read_bytes()
        return Response(
            data,
            media_type="application/json",
            headers={"Content-Disposition": f'inline; filename="{asset}"'},
        )

    @app.get("/api/health")
    def health(request: Request):
        if account_services is not None:
            return {"ok": True, "mode": "account"}
        if demo_services is not None:
            from el.product.wiring import (
                MULTI_THESIS_FIXTURE, ROBINHOOD_CLARIFICATION_ANSWER,
                ROBINHOOD_CLARIFICATION_QUESTION, STABLECOIN_CLARIFICATION_ANSWER,
                STABLECOIN_CLARIFICATION_QUESTION,
            )
            return {"ok": True, "mode": "fixture", "anonymous_demo": True,
                    "session_ref": request.state.demo_session_ref,
                    "founder_acceptance": {"enabled": False},
                    "fixture_source_examples": [MULTI_THESIS_FIXTURE],
                    "fixture_clarification_examples": [
                        {"question": STABLECOIN_CLARIFICATION_QUESTION,
                         "answer": STABLECOIN_CLARIFICATION_ANSWER},
                        {"question": ROBINHOOD_CLARIFICATION_QUESTION,
                         "answer": ROBINHOOD_CLARIFICATION_ANSWER},
                    ]}
        return {"ok": True, **api.stats()}

    if demo_services is not None:
        @app.post("/api/demo/hormuz")
        def start_hormuz():
            from el.product.demo_hormuz import ensure_hormuz
            return ensure_hormuz(demo_services, api._actor)

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

    @app.post("/api/v3/source-interpretation-jobs", status_code=202)
    def submit_source_interpretation(
        payload: SourceInterpretationJobIn, request: Request
    ):
        # This is deliberately manual instrumentation after FastAPI/Pydantic
        # parsed the body.  It neither auto-captures headers/body nor reads
        # baggage.  A malformed traceparent is discarded without logging it.
        raw_traceparent = request.headers.get("traceparent")
        parent_traceparent = extract_traceparent(request.headers)
        with api._s.source_interpretation_jobs.telemetry.span(
            "fitcheck.source.accept", parent_traceparent=parent_traceparent
        ) as span:
            span.set_attributes(
                {
                    "fitcheck.source.stage": "accepted",
                    "fitcheck.source.context": (
                        "accepted"
                        if parent_traceparent is not None
                        else "invalid" if raw_traceparent is not None else "absent"
                    ),
                }
            )
            result = api.submit_source_interpretation(
                payload.input_text,
                source_url=payload.source_url,
                idempotency_key=payload.idempotency_key,
            ).model_dump(mode="json")
            span.set_attributes(
                {
                    "fitcheck.source.result": (
                        "queued" if result["created"] else "reused"
                    )
                }
            )
            return result

    @app.get(
        "/api/v3/source-interpretation-jobs/by-idempotency/{idempotency_key}"
    )
    def source_interpretation_job_by_idempotency(idempotency_key: str):
        return api.get_source_interpretation_job_by_idempotency(
            idempotency_key
        ).model_dump(mode="json")

    @app.get("/api/v3/source-interpretation-jobs/{job_id}")
    def source_interpretation_job_status(job_id: uuid.UUID):
        return api.get_source_interpretation_job(job_id).model_dump(mode="json")

    @app.get("/api/v3/source-interpretations/{interpretation_id}")
    def source_interpretation_state(interpretation_id: uuid.UUID):
        return api.get_source_interpretation(interpretation_id).model_dump(
            mode="json"
        )

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
        if demo_services is not None:
            from el.domain.tables import ThesisAnalysis
            from el.domain.structures import ExtractedStructure
            from el.product.demo_frozen_markets import FrozenDemoMarketProvider, registered_pair
            api._require_thesis_access(thesis_analysis_id)
            with demo_services.session_factory() as session:
                thesis = session.get(ThesisAnalysis, thesis_analysis_id)
                try:
                    structure = ExtractedStructure.model_validate(thesis.extracted_structure)
                except ValueError:
                    structure = None
            if (not isinstance(demo_services.retrieval._provider, FrozenDemoMarketProvider)
                or registered_pair(structure) is None):
                return JSONResponse(status_code=503, content={
                    "error": "market_pool_unavailable",
                    "detail": "Frozen candidate-bound evidence is unavailable for this accepted thesis. No market pool or no-clean conclusion is available.",
                })
        return api.assess_market_pool(thesis_analysis_id).model_dump(
            mode="json"
        )

    @app.get("/api/v3/theses/{thesis_analysis_id}/accepted-state")
    def accepted_thesis_state(thesis_analysis_id: uuid.UUID):
        if demo_services is not None:
            from el.domain.tables import ThesisAnalysis
            from el.product.demo_hormuz import MARKER
            api._require_thesis_access(thesis_analysis_id)
            with demo_services.session_factory() as s:
                analysis = s.get(ThesisAnalysis, thesis_analysis_id)
                if (analysis is not None and
                    (analysis.extracted_structure or {}).get("reconstructed") == MARKER):
                    return JSONResponse(status_code=404, content={"error": "not_found"})
        return api.get_accepted_thesis_state(thesis_analysis_id).model_dump(
            mode="json"
        )

    @app.get("/api/v3/account-state")
    def account_state():
        return api.get_account_state().model_dump(mode="json")

    @app.get("/api/v3/market-pools/{market_display_set_id}")
    def market_pool_state(market_display_set_id: uuid.UUID):
        return api.get_market_pool(market_display_set_id).model_dump(mode="json")

    @app.get("/api/v3/market-choices/{market_choice_id}")
    def market_choice_state(market_choice_id: uuid.UUID):
        return api.get_market_choice(market_choice_id).model_dump(mode="json")

    @app.post("/api/v3/market-pools/{market_display_set_id}/choice")
    def choose_market(
        market_display_set_id: uuid.UUID, payload: MarketChoiceIn
    ):
        if demo_services is not None:
            from el.product.demo_frozen_markets import AUTHORITY
            from el.domain.tables import MarketAssessment, MarketDisplayItem
            with demo_services.session_factory() as session:
                assessments = session.scalars(select(MarketAssessment).join(
                    MarketDisplayItem, MarketDisplayItem.market_assessment_id == MarketAssessment.id
                ).where(MarketDisplayItem.market_display_set_id == market_display_set_id)).all()
                if not assessments or any(a.authority != AUTHORITY for a in assessments):
                    return JSONResponse(status_code=404, content={"error": "not_found"})
        if demo_services is not None:
            api._require_thesis_access(assessments[0].thesis_analysis_id)
            api._market_pool_out(market_display_set_id)
        return api.choose_market(
            market_display_set_id,
            selection_kind=payload.selection_kind,
            market_assessment_id=payload.market_assessment_id,
            reason=payload.reason,
        ).model_dump(mode="json")

    @app.post("/api/v3/market-choices/{market_choice_id}/revisit-activation")
    def activate_revisit(market_choice_id: uuid.UUID):
        return api.activate_revisit(market_choice_id)

    @app.get("/api/v3/market-choices/{market_choice_id}/revisit-activation")
    def get_revisit_activation(market_choice_id: uuid.UUID):
        return api.get_revisit_activation(market_choice_id)

    @app.post("/api/v3/thesis-revisions/{revision_id}/revisit-check")
    def check_revisit(revision_id: uuid.UUID, payload: RevisitCheckIn):
        if demo_services is not None and payload.fixture_scenario is not None:
            return JSONResponse(status_code=422, content={
                "error": "revisit_unavailable",
                "detail": "Only the registered retained Hormuz capture is available in this demo.",
            })
        return api.check_revisit(
            revision_id, idempotency_key=payload.idempotency_key,
            fixture_scenario=payload.fixture_scenario,
            capture_id=payload.capture_id,
        )

    @app.get("/api/v3/thesis-revisions/{revision_id}/revisit")
    def read_revisit(revision_id: uuid.UUID):
        saved = api.get_revisit(revision_id)
        if demo_services is not None:
            from el.product.demo_hormuz import bound_impact_copy
            impact = bound_impact_copy(saved)
        else:
            impact = _bound_fixture_impact(fixture_note, saved)
        if impact is not None:
            saved = deepcopy(saved)
            saved["latest"]["fixture_possible_impact"] = impact
        return saved

    @app.post("/api/v3/revisit-packets/{packet_id}/decision")
    def decide_revisit(packet_id: uuid.UUID, payload: RevisitDecisionIn):
        return api.decide_revisit(
            packet_id, action=payload.action,
            expected_revision_digest=payload.expected_revision_digest,
            expected_observation_id=payload.expected_observation_id,
        )

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
    if __import__("os").environ.get("FITCHECK_ANONYMOUS_DEMO") == "1":
        return build_app(services)
    actor = local_actor(services.session_factory)
    return build_app(ProductApi(services, actor))


def __getattr__(name: str):
    # Lazy `el.product.app:app` for uvicorn — importing this module must not
    # create the local DB as a side effect (tests inject their own factory).
    if name == "app":
        return build_default_app()
    raise AttributeError(name)
