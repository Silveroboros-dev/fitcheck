"""Product-UI FastAPI app (agent-guided-ui-contract-v0).

Thin HTTP shell over ``ProductApi``: routes, typed-error → status mapping,
and the single static page. Loop semantics live in the services; the typed
errors carry the contract's explicit failure states.
"""

import json
import uuid
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from importlib.resources import files
from ipaddress import ip_address
from pathlib import Path
from typing import Literal, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from starlette.routing import Match

from el.extraction.service import NormalizationAttemptConflict
from el.jobs import IdempotencyConflict
from el.marketpool.service import MarketPoolConflict, RelatedPairProposal
from el.mcp.contracts import (
    BlindPriorRequired,
    NotFound,
    SaveRejected,
    ToolRefused,
)
from el.models.vertex_canary import VertexCanaryRefused
from el.observability import configure_structured_stderr_logging, extract_traceparent
from el.product.api import (
    AcceptedThesisStateUiOut, MarketPoolUiOut, ProductApi, ProposerUnavailable,
)
from el.product.demo_admission import (
    DemoAdmission, DemoAdmissionDenied, MAX_REQUEST_BYTES,
)
from el.product.curated_demo import load_curated_agi_case
from el.product.wiring import HumanActor, ProductServices, build_services, local_actor
from el.product.demo_sessions import COOKIE_NAME, SESSION_SECONDS, DemoSessions
from el.revisit.service import RevisitConflict, RevisitNotFound, RevisitUnavailable
from el.sourceinterpretation.service import SourceCandidateChoiceConflict
from el.sourceinterpretation.jobs import (
    SourceJobAdmission,
    SourceJobAdmissionError,
)

STATIC_HTML = files("el.product").joinpath("static", "product_console.html")
ACCOUNT_COOKIE_NAME_PREFIX = "__Host-fitcheck_account_"
# Public-projection compatibility alias. The value is now a prefix; the
# concrete private session adapter appends the full token digest.
ACCOUNT_COOKIE_NAME = ACCOUNT_COOKIE_NAME_PREFIX
MAX_ACCOUNT_COOKIE_CANDIDATES = 8
MAX_ACCOUNT_COOKIE_HEADER_BYTES = 4096
_ACCOUNT_COOKIE_DIGEST_LENGTH = 64
SYNTHETIC_MARKET_FIXTURE_ASSETS = frozenset({
    "synthetic-market-stablecoin-total-cap.json",
    "synthetic-market-stablecoin-unit-cap.json",
    "synthetic-market-treasury-ledger-payment.json",
    "synthetic-market-broker-tokenized-asset.json",
    "synthetic-market-broker-first-tokenized.json",
    "synthetic-market-broker-chain-token.json",
})


class HistoricalDemoCase(BaseModel):
    """Private presentation evidence; never imported into visitor-owned rows."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["private-historical-demo-v1"]
    case_id: Literal["agi2028"]
    title: str = Field(min_length=1)
    recorded_at: datetime
    original_input: str = Field(min_length=1)
    acceptance_note: str = Field(min_length=1)
    provenance_note: str = Field(min_length=1)
    accepted_state: AcceptedThesisStateUiOut
    market_pool: MarketPoolUiOut

    @model_validator(mode="after")
    def bound_evidence(self):
        accepted, pool = self.accepted_state, self.market_pool
        if (
            accepted.thesis_analysis_id != pool.thesis_analysis_id
            or accepted.latest_market_display_set_id != pool.market_display_set_id
            or accepted.accepted_thesis_summary != pool.accepted_thesis_summary
            or not accepted.accepted_thesis_wording
            or accepted.accepted_thesis_wording != pool.accepted_thesis_wording
            or accepted.restored_source is not None
        ):
            raise ValueError("historical acceptance/pool binding mismatch")
        if (
            not pool.assessment_complete or pool.incomplete_reasons
            or pool.assessed_count != 3 or pool.target_count != 3
            or pool.displayed_count != 3 or len(pool.candidate_markets) != 3
            or pool.system_pool_outcome != "no_clean_expression"
            or pool.retrieval_scope is None
            or pool.retrieval_scope.scope_kind != "frozen_snapshot"
            or pool.retrieval_scope.snapshot_id != pool.snapshot_id
            or pool.retrieval_scope.returned_count != 3
        ):
            raise ValueError("historical three-contract pool is incomplete or unbound")
        for field in ("market_assessment_id", "candidate_set_member_id",
                      "rules_capture_id", "market_id"):
            if len({getattr(card, field) for card in pool.candidate_markets}) != 3:
                raise ValueError("duplicate historical candidate identity")
        for rank, card in enumerate(pool.candidate_markets, 1):
            if (
                card.snapshot_id != pool.snapshot_id or card.display_rank != rank
                or card.pair_class != "not_an_expression"
                or card.authority != "deterministic_only"
                or not card.resolution_conditions or card.frozen_demo_evidence is not None
                or card.relatedness_basis is not None
            ):
                raise ValueError("historical candidate evidence mismatch")
        return self


def _historical_case_digest(payload: dict) -> str:
    return sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


class HistoricalDemoPredecessor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case: HistoricalDemoCase
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def pinned_predecessor(cls, value):
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("case"), dict)
            or _historical_case_digest(value["case"]) != value.get("sha256")
        ):
            raise ValueError("historical predecessor digest mismatch")
        return value


class HistoricalRelatednessBasis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["source-bound-relatedness-v1"]
    predecessor_pair_class: Literal["not_an_expression"]
    authority: Literal["source_bound_relation_proposal"]
    proposal: RelatedPairProposal


class HistoricalDemoPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_input_en: str = Field(min_length=1)
    acceptance_note_en: str = Field(min_length=1)


class HistoricalDemoCaseV2(BaseModel):
    """Successor presentation with the complete original packet retained."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["private-historical-demo-v2"]
    case_id: Literal["agi2028"]
    title: str = Field(min_length=1)
    recorded_at: datetime
    original_input: str = Field(min_length=1)
    acceptance_note: str = Field(min_length=1)
    provenance_note: str = Field(min_length=1)
    presentation: HistoricalDemoPresentation
    accepted_state: AcceptedThesisStateUiOut
    market_pool: MarketPoolUiOut
    predecessor: HistoricalDemoPredecessor

    @model_validator(mode="after")
    def bound_successor(self):
        accepted, pool = self.accepted_state, self.market_pool
        old = self.predecessor.case
        old_accepted, old_pool = old.accepted_state, old.market_pool
        if (
            self.case_id != old.case_id or self.original_input != old.original_input
            or self.acceptance_note != old.acceptance_note
            or accepted.thesis_analysis_id != old_accepted.thesis_analysis_id
            or accepted.normalization_decision_id != old_accepted.normalization_decision_id
            or accepted.accepted_at != old_accepted.accepted_at
            or accepted.acceptance_origin != old_accepted.acceptance_origin
            or accepted.accepted_thesis_summary != old_accepted.accepted_thesis_summary
            or accepted.accepted_thesis_wording != old_accepted.accepted_thesis_wording
            or accepted.restored_source != old_accepted.restored_source
            or accepted.latest_market_display_set_id != pool.market_display_set_id
            or pool.thesis_analysis_id != accepted.thesis_analysis_id
            or pool.accepted_thesis_summary != accepted.accepted_thesis_summary
            or pool.accepted_thesis_wording != accepted.accepted_thesis_wording
        ):
            raise ValueError("historical successor acceptance binding mismatch")
        if (
            pool.market_display_set_id == old_pool.market_display_set_id
            or pool.fit_card_id == old_pool.fit_card_id
            or pool.candidate_set_id == old_pool.candidate_set_id
            or pool.snapshot_id != old_pool.snapshot_id
            or pool.snapshot_as_of != old_pool.snapshot_as_of
            or not pool.assessment_complete or pool.incomplete_reasons
            or pool.assessed_count != 3 or pool.target_count != 3
            or pool.displayed_count != 3 or len(pool.candidate_markets) != 3
            or pool.system_pool_outcome != "no_clean_expression"
            or pool.retrieval_scope is None
            or pool.retrieval_scope.scope_kind != "frozen_snapshot"
            or pool.retrieval_scope.snapshot_id != pool.snapshot_id
            or pool.retrieval_scope.returned_count != 3
        ):
            raise ValueError("historical successor pool binding mismatch")
        old_cards = {card.market_id: card for card in old_pool.candidate_markets}
        if {card.market_id for card in pool.candidate_markets} != set(old_cards):
            raise ValueError("historical successor market set mismatch")
        for field in ("market_assessment_id", "candidate_set_member_id",
                      "rules_capture_id", "market_id"):
            if len({getattr(card, field) for card in pool.candidate_markets}) != 3:
                raise ValueError("duplicate historical successor candidate identity")
        proposal_ids: set[uuid.UUID] = set()
        related_count = 0
        wording = accepted.accepted_thesis_wording or ""
        for rank, card in enumerate(pool.candidate_markets, 1):
            before = old_cards[card.market_id]
            if (
                card.display_rank != rank or card.snapshot_id != pool.snapshot_id
                or card.market_title != before.market_title
                or card.retrieval_rank != before.retrieval_rank
                or card.resolution_conditions != before.resolution_conditions
                or card.rules_capture_id != before.rules_capture_id
                or card.market_assessment_id == before.market_assessment_id
                or card.candidate_set_member_id == before.candidate_set_member_id
                or card.frozen_demo_evidence is not None
            ):
                raise ValueError("historical successor rules or candidate identity mismatch")
            if card.relatedness_basis is None:
                if card.pair_class != before.pair_class or card.authority != before.authority:
                    raise ValueError("unannotated historical successor class changed")
                continue
            basis = HistoricalRelatednessBasis.model_validate(card.relatedness_basis)
            proposal = basis.proposal
            if (
                card.pair_class != "weak_proxy"
                or card.authority != basis.authority
                or basis.predecessor_pair_class != before.pair_class
                or proposal.thesis_analysis_id != accepted.thesis_analysis_id
                or proposal.accepted_wording_sha256 != sha256(wording.encode("utf-8")).hexdigest()
                or proposal.market_id != card.market_id
                or proposal.snapshot_id != card.snapshot_id
                or proposal.rules_capture_id != card.rules_capture_id
                or proposal.rules_sha256 != sha256(card.resolution_conditions.encode("utf-8")).hexdigest()
                or not proposal.claim_quote.strip()
                or proposal.claim_quote not in wording
                or not proposal.rules_quote.strip()
                or proposal.rules_quote not in card.resolution_conditions
                or not proposal.proposer_id.strip()
                or not proposal.rationale.strip()
                or proposal.proposal_id in proposal_ids
            ):
                raise ValueError("historical successor relatedness evidence mismatch")
            proposal_ids.add(proposal.proposal_id)
            related_count += 1
        if not related_count:
            raise ValueError("historical successor lacks a relatedness proposal")
        return self


def load_historical_demo_case(path: Path, expected_sha256: str) -> dict:
    """Check the local envelope, retaining its canonical digest as a UI pin."""
    envelope = json.loads(path.read_text(encoding="utf-8"))
    payload = envelope["case"]
    digest = _historical_case_digest(payload)
    if digest != envelope["sha256"] or digest != expected_sha256:
        raise ValueError("historical demo digest mismatch")
    if payload.get("schema_version") == "private-historical-demo-v1":
        HistoricalDemoCase.model_validate(payload)
    elif payload.get("schema_version") == "private-historical-demo-v2":
        HistoricalDemoCaseV2.model_validate(payload)
    else:
        raise ValueError("unsupported historical demo schema")
    return {"case": payload, "sha256": digest}


class AccountVerifiedBinding(Protocol):
    actor: HumanActor


class AccountSessionIssue(Protocol):
    raw_token: str
    cookie_name: str
    expires_at: datetime


class AccountSessionResolution(Protocol):
    actor: HumanActor
    account_identity_id: uuid.UUID
    audience: str


class AccountSessionManager(Protocol):
    def rotate(self, binding: AccountVerifiedBinding) -> AccountSessionIssue: ...
    def resolve(self, raw_token: object) -> AccountSessionResolution | None: ...
    def revoke(self, raw_token: object) -> bool: ...


class AccountResolverProtocol(Protocol):
    require_https: bool
    allowed_origin: str

    async def resolve(self, request: Request) -> HumanActor: ...
    async def resolve_verified(self, request: Request) -> AccountVerifiedBinding: ...
    def revalidate_session(
        self, *, account_identity_id: uuid.UUID, audience: str, actor: HumanActor
    ) -> HumanActor: ...


def _account_cookie_name(raw_token: object) -> str | None:
    if not isinstance(raw_token, str):
        return None
    try:
        encoded = raw_token.encode("ascii")
    except UnicodeEncodeError:
        return None
    return f"{ACCOUNT_COOKIE_NAME_PREFIX}{sha256(encoded).hexdigest()}"


def _safe_account_cookie_name(name: str) -> bool:
    if not name.startswith(ACCOUNT_COOKIE_NAME_PREFIX):
        return False
    suffix = name[len(ACCOUNT_COOKIE_NAME_PREFIX) :]
    return (
        len(suffix) == _ACCOUNT_COOKIE_DIGEST_LENGTH
        and suffix == suffix.lower()
        and all(character in "0123456789abcdef" for character in suffix)
    )


def _account_cookie_candidates(
    request: Request,
) -> tuple[list[tuple[str, str]], bool] | None:
    raw_values = [
        value
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"cookie"
    ]
    if sum(len(value) for value in raw_values) > MAX_ACCOUNT_COOKIE_HEADER_BYTES:
        return None
    prefix = ACCOUNT_COOKIE_NAME_PREFIX.encode("ascii")
    candidates: list[tuple[str, str]] = []
    for raw_value in raw_values:
        for raw_pair in raw_value.split(b";"):
            raw_name, separator, raw_token = raw_pair.strip().partition(b"=")
            if separator and raw_name.startswith(prefix):
                candidates.append(
                    (
                        raw_name.decode("latin-1"),
                        raw_token.strip().decode("latin-1"),
                    )
                )
                if len(candidates) > MAX_ACCOUNT_COOKIE_CANDIDATES:
                    return candidates[:MAX_ACCOUNT_COOKIE_CANDIDATES], True
    return candidates, False


def _delete_account_cookies(response: Response, names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen or not _safe_account_cookie_name(name):
            continue
        seen.add(name)
        response.delete_cookie(
            name,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )


def _account_identity_denied(*, stale_names: list[str] | None = None) -> Response:
    response = JSONResponse({"error": "identity_denied"}, status_code=401)
    if stale_names:
        _delete_account_cookies(response, stale_names)
    response.headers["Cache-Control"] = "no-store"
    return response


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
    model_config = ConfigDict(extra="forbid")

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
    account_resolver: AccountResolverProtocol | None = None,
    account_sessions: AccountSessionManager | None = None,
    source_job_admission: SourceJobAdmission | None = None,
    demo_admission: DemoAdmission | None = None,
    historical_case: dict | None = None,
) -> FastAPI:
    if account_resolver is not None and not isinstance(api, ProductServices):
        raise ValueError("account mode requires services, not a prebound actor")
    if account_sessions is not None and account_resolver is None:
        raise ValueError("account sessions require account mode")
    if account_sessions is not None and not account_resolver.require_https:
        raise ValueError("account browser sessions require HTTPS")
    if source_job_admission is not None and account_resolver is None:
        raise ValueError("source job admission requires account mode")
    source_request_max_bytes = (
        source_job_admission.request_max_bytes
        if source_job_admission is not None
        else None
    )
    if source_request_max_bytes is not None and (
        type(source_request_max_bytes) is not int
        or not 1 <= source_request_max_bytes < 2**31
    ):
        raise ValueError("bounded source request byte limit is required")
    account_services = api if account_resolver is not None else None
    demo_services = api if isinstance(api, ProductServices) and account_resolver is None else None
    if demo_admission is not None and demo_services is None:
        raise ValueError("fixture admission requires anonymous demo services")
    if account_services is not None and account_services.mode != "fixture":
        raise ValueError("offline account mode requires fixture services")
    if demo_services is not None and demo_services.mode != "fixture":
        raise ValueError("anonymous demo requires fixture mode")
    if historical_case is not None:
        if demo_services is None or demo_admission is not None:
            raise ValueError("private history requires a local anonymous fixture demo")
        if historical_case["case"].get("schema_version") == "private-historical-demo-v1":
            payload = HistoricalDemoCase.model_validate(historical_case["case"])
        elif historical_case["case"].get("schema_version") == "private-historical-demo-v2":
            payload = HistoricalDemoCaseV2.model_validate(historical_case["case"])
        else:
            raise ValueError("unsupported historical demo schema")
        history_digest = _historical_case_digest(historical_case["case"])
        if history_digest != historical_case["sha256"]:
            raise ValueError("historical demo digest mismatch")
        history = deepcopy(historical_case)
        history_metadata = {
            "case_id": payload.case_id, "title": payload.title,
            "sha256": history_digest, "schema_version": payload.schema_version,
        }
    else:
        history = history_metadata = None
    curated_agi = load_curated_agi_case() if demo_services is not None else None
    curated_agi_metadata = (
        {"case_id": curated_agi["case"]["case_id"],
         "schema_version": curated_agi["case"]["schema_version"],
         "sha256": curated_agi["sha256"]}
        if curated_agi is not None else None
    )
    if fixture_revisit_explanation is not None and api.mode != "fixture":
        raise ValueError("fixture explanation is available only in fixture mode")
    fixture_note = deepcopy(fixture_revisit_explanation)
    request_api = _RequestApi() if demo_services is not None or account_services is not None else None
    if request_api is not None:
        api = request_api
    demo_sessions = DemoSessions(demo_services.session_factory, demo_admission) if demo_services else None
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
            if path == "/api/v3/account-session" and account_sessions is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            request_origin = f"{request.url.scheme}://{request.url.netloc}"
            if (
                account_sessions is not None
                and (
                    request.url.scheme != "https"
                    or request_origin != account_resolver.allowed_origin
                )
            ) or (
                account_sessions is None
                and account_resolver.require_https
                and request.url.scheme != "https"
            ):
                return JSONResponse(
                    {"error": "cross_origin_denied"}, status_code=403
                )
            if account_sessions is not None and (
                (
                    request.headers.get("origin") is not None
                    and request.headers.get("origin")
                    != account_resolver.allowed_origin
                )
                or request.headers.get("sec-fetch-site") == "cross-site"
            ):
                return JSONResponse(
                    {"error": "cross_origin_denied"}, status_code=403
                )
            if request.method not in {"GET", "HEAD"}:
                origin = request.headers.get("origin")
                if (
                    origin != account_resolver.allowed_origin
                    or request.headers.get("sec-fetch-site") == "cross-site"
                ):
                    return JSONResponse(
                        {"error": "cross_origin_denied"}, status_code=403
                    )
            account_cookie_candidates = None
            if account_sessions is not None:
                parsed_account_cookies = _account_cookie_candidates(request)
                if parsed_account_cookies is None:
                    return _account_identity_denied()
                account_cookie_candidates, account_cookie_overflow = (
                    parsed_account_cookies
                )
                if account_cookie_overflow:
                    # Bound response amplification while giving a legitimate
                    # browser a recovery path from delayed login responses.
                    return _account_identity_denied(
                        stale_names=[
                            name for name, _ in account_cookie_candidates
                        ]
                    )
            if request.method not in {"GET", "HEAD"}:
                if (
                    request.method == "POST"
                    and path == "/api/v3/source-interpretation-jobs"
                    and source_job_admission is None
                ):
                    return JSONResponse(
                        {"error": "pilot_admission_unconfigured"},
                        status_code=503,
                    )
                if source_request_max_bytes is not None:
                    declared_size = request.headers.get("content-length", "0")
                    if (
                        declared_size.isdecimal()
                        and int(declared_size) > source_request_max_bytes
                    ):
                        return JSONResponse(
                            {"error": "request_too_large"}, status_code=413
                        )
                    body = bytearray()
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > source_request_max_bytes:
                            return JSONResponse(
                                {"error": "request_too_large"}, status_code=413
                            )
                        body.extend(chunk)
                    request._body = bytes(body)

            if path == "/api/v3/account-session":
                candidates = account_cookie_candidates
                assert candidates is not None
                if request.method == "POST":
                    try:
                        binding = await account_resolver.resolve_verified(request)
                        issued = account_sessions.rotate(binding)
                    except (IdentityDenied, ValueError):
                        return _account_identity_denied()
                    if issued.cookie_name != _account_cookie_name(issued.raw_token):
                        raise RuntimeError(
                            "account session issued an invalid cookie name"
                        )
                    response = Response(status_code=204)
                    _delete_account_cookies(
                        response, [name for name, _ in candidates]
                    )
                    response.set_cookie(
                        issued.cookie_name,
                        issued.raw_token,
                        expires=issued.expires_at,
                        secure=True,
                        httponly=True,
                        samesite="strict",
                        path="/",
                    )
                    response.headers["Cache-Control"] = "no-store"
                    return response
                for name, raw_token in candidates:
                    if name == _account_cookie_name(raw_token):
                        account_sessions.revoke(raw_token)
                response = Response(status_code=204)
                _delete_account_cookies(
                    response, [name for name, _ in candidates]
                )
                response.headers["Cache-Control"] = "no-store"
                return response
            stale_cookie_names: list[str] = []
            try:
                if account_sessions is None:
                    actor = await account_resolver.resolve(request)
                else:
                    candidates = account_cookie_candidates
                    assert candidates is not None
                    valid_sessions = []
                    for name, raw_token in candidates:
                        if name != _account_cookie_name(raw_token):
                            stale_cookie_names.append(name)
                            continue
                        resolved = account_sessions.resolve(raw_token)
                        if resolved is None:
                            stale_cookie_names.append(name)
                        else:
                            valid_sessions.append(resolved)
                    if len(valid_sessions) != 1:
                        return _account_identity_denied(
                            stale_names=stale_cookie_names
                        )
                    resolved = valid_sessions[0]
                    actor = account_resolver.revalidate_session(
                        account_identity_id=resolved.account_identity_id,
                        audience=resolved.audience,
                        actor=resolved.actor,
                    )
            except IdentityDenied:
                return _account_identity_denied(
                    stale_names=[
                        name for name, _ in account_cookie_candidates
                    ]
                    if account_cookie_candidates is not None
                    else None
                )
            token = request_api.current.set(
                ProductApi(
                    account_services,
                    actor,
                    source_job_admission=source_job_admission,
                )
            )
            try:
                response = await call_next(request)
            finally:
                request_api.current.reset(token)
            if stale_cookie_names:
                _delete_account_cookies(response, stale_cookie_names)
            response.headers["Cache-Control"] = "no-store"
            return response

    if demo_sessions is not None:
        @app.middleware("http")
        async def demo_identity(request: Request, call_next):
            if history is not None:
                try:
                    local_client = request.client is not None and ip_address(
                        request.client.host
                    ).is_loopback
                except ValueError:
                    local_client = False
                if not local_client or request.url.hostname not in {
                    "localhost", "127.0.0.1", "::1"
                }:
                    return JSONResponse({"error": "private_history_local_only"}, status_code=403)
            # Count streamed bytes, including chunked bodies without a declared
            # Content-Length, before any identity lookup or database mutation.
            if demo_admission is not None:
                declared_size = request.headers.get("content-length", "0")
                if declared_size.isdecimal() and int(declared_size) > MAX_REQUEST_BYTES:
                    return JSONResponse({"error": "request_too_large"}, status_code=413)
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > MAX_REQUEST_BYTES:
                        return JSONResponse({"error": "request_too_large"}, status_code=413)
                request._body = bytes(body)
            origin = request.headers.get("origin")
            host_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if request.method not in {"GET", "HEAD"} and (
                origin != host_origin
                or request.headers.get("sec-fetch-site") == "cross-site"
            ):
                return JSONResponse({"error": "cross_origin_denied"}, status_code=403)
            path = request.url.path
            if path in {"/livez", "/readyz"}:
                # Cloud probes do not bootstrap a visitor or spend demo quota.
                return await call_next(request)
            if (
                demo_admission is not None
                and request.method == "POST"
                and path == "/api/v3/source-interpretations"
            ):
                # The bounded cloud slice uses the asynchronous path, where
                # request and Job creation share one quota transaction.
                return JSONResponse({"error": "not_found"}, status_code=404)
            if path.startswith("/api/") and not (
                path == "/api/health"
                or path == "/api/demo/reset"
                or path == "/api/demo/hormuz"
                or (curated_agi is not None and path == "/api/demo/agi2028")
                or (history is not None and path == "/api/demo/history/agi2028")
                or (path.startswith("/api/v3/") and not path.endswith("/feedback"))
            ):
                return JSONResponse({"error": "not_found"}, status_code=404)
            if path != "/api/demo/reset" and not any(
                route.matches(request.scope)[0] == Match.FULL
                for route in app.router.routes
            ):
                return JSONResponse({"error": "not_found"}, status_code=404)
            if (
                path.startswith("/assets/brand/")
                or path.startswith("/assets/demo-fixtures/markets/")
            ):
                # Static assets contain no visitor state. In particular, an
                # expired cookie must not prevent the page from rendering.
                return await call_next(request)
            if demo_admission is not None and request.method not in {"GET", "HEAD"}:
                try:
                    demo_admission.require_writable()
                except DemoAdmissionDenied as exc:
                    return JSONResponse({"error": exc.reason}, status_code=exc.status_code)
            cookie = request.cookies.get(COOKIE_NAME)
            resolved = demo_sessions.resolve(cookie)
            if cookie and resolved is None and path not in {"/", "/api/demo/reset"}:
                # Never send a deletion header for an old token: this response
                # can arrive after a reset response set the new cookie.
                return JSONResponse({"error": "session_expired"}, status_code=401)
            if path == "/api/demo/reset":
                if request.method != "POST":
                    return JSONResponse({"error": "not_found"}, status_code=404)
                if resolved is not None and demo_admission is None:
                    demo_sessions.revoke(cookie)
                try:
                    actor, session_ref, new_cookie = demo_sessions.create(
                        cookie if resolved is not None else None
                    )
                except DemoAdmissionDenied as exc:
                    return JSONResponse({"error": exc.reason}, status_code=exc.status_code)
                response = JSONResponse({"session_ref": session_ref})
                response.set_cookie(COOKIE_NAME, new_cookie, max_age=SESSION_SECONDS,
                                    httponly=True, samesite="lax", secure=request.url.scheme == "https")
                return response
            new_cookie = None
            if resolved is None:
                if request.method not in {"GET", "HEAD"}:
                    return JSONResponse({"error": "session_required"}, status_code=401)
                try:
                    actor, session_ref, new_cookie = demo_sessions.create()
                except DemoAdmissionDenied as exc:
                    return JSONResponse({"error": exc.reason}, status_code=exc.status_code)
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

    @app.exception_handler(DemoAdmissionDenied)
    async def _demo_admission_denied(_, exc: DemoAdmissionDenied):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.reason})

    @app.exception_handler(SourceJobAdmissionError)
    async def _source_job_admission_error(_, exc: SourceJobAdmissionError):
        content = {"error": exc.error}
        if exc.detail is not None:
            content["detail"] = exc.detail
        return JSONResponse(status_code=exc.status_code, content=content)

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
                    "historical_case": history_metadata,
                    "curated_agi_case": curated_agi_metadata,
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

        @app.get("/api/demo/agi2028")
        def curated_agi_demo(request: Request):
            return {**deepcopy(curated_agi), "session_ref": request.state.demo_session_ref}

    if history is not None:
        @app.get("/api/demo/history/agi2028")
        def historical_demo(request: Request):
            return {**deepcopy(history), "session_ref": request.state.demo_session_ref}

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
        if demo_admission is not None:
            return JSONResponse({"error": "not_found"}, status_code=404)
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
                demo_admission=demo_admission,
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
    import os
    history_path = os.environ.get("FITCHECK_PRIVATE_HISTORY_FILE")
    history_pin = os.environ.get("FITCHECK_PRIVATE_HISTORY_SHA256")
    if bool(history_path) != bool(history_pin):
        raise ValueError("private history requires an explicit file and digest pin")
    if history_path and os.environ.get("FITCHECK_ANONYMOUS_DEMO") != "1":
        raise ValueError("private history requires anonymous fixture mode")
    services = build_services()
    if os.environ.get("FITCHECK_ANONYMOUS_DEMO") == "1":
        history = load_historical_demo_case(Path(history_path), history_pin) if history_path else None
        return build_app(services, historical_case=history)
    actor = local_actor(services.session_factory)
    return build_app(ProductApi(services, actor))


def __getattr__(name: str):
    # Lazy `el.product.app:app` for uvicorn — importing this module must not
    # create the local DB as a side effect (tests inject their own factory).
    if name == "app":
        return build_default_app()
    raise AttributeError(name)
