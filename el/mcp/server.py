"""FastMCP transport for the FitCheck MCP tools (step 7 C2).

Thin transport: resolve the api-key Principal from the request, dispatch to
McpTools (which owns the field-aware A7 guard + the blind-prior protocol), and
map typed McpError to MCP tool errors. Primary transport = Streamable HTTP on
Cloud Run; stdio for local dev. The server adds NO logic — McpTools is the
composition root.
"""

import uuid
from typing import Annotated, Callable, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from el.mcp.auth import Principal, resolve_principal
from el.mcp.contracts import McpError
from el.mcp.rate_limit import TOOL_COST_UNITS
from el.mcp.tools import McpTools
from el.mcp.v3_tools import McpV3Tools

try:  # surface typed errors as MCP tool errors when the SDK exposes it
    from mcp.server.fastmcp.exceptions import ToolError
except Exception:  # pragma: no cover - SDK-version fallback
    ToolError = RuntimeError  # type: ignore[assignment, misc]

# Resolves the authenticated Principal for a request. In prod this reads the
# api key from the request headers; injected (stubbed) in tests.
PrincipalResolver = Callable[[Context], Principal]

V3InputText = Annotated[str, Field(min_length=1, max_length=20_000)]
V3IdempotencyKey = Annotated[str, Field(min_length=1, max_length=160)]
V3SourceUrl = Annotated[str | None, Field(max_length=2_048)]
V3Reason = Annotated[str | None, Field(max_length=2_000)]
V3InputDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def header_principal_resolver(
    session_factory, *, header: str = "x-api-key"
) -> PrincipalResolver:
    """Default resolver: read the api key from the request header (Streamable
    HTTP) and resolve it to a Principal. stdio has no headers — wire auth
    differently there (out of scope for the minimal surface)."""

    def _resolve(ctx: Context) -> Principal:
        request = getattr(ctx.request_context, "request", None)
        api_key = None
        if request is not None:
            api_key = request.headers.get(header)
            if not api_key:
                # Parse "Authorization: Bearer <key>" — extract the token, do
                # NOT hash the whole header string (P2).
                authorization = request.headers.get("authorization", "")
                if authorization.lower().startswith("bearer "):
                    api_key = authorization[len("bearer ") :].strip()
        with session_factory() as session:
            return resolve_principal(session, api_key)

    return _resolve


def _fail(error: Exception) -> "ToolError":
    if isinstance(error, McpError):
        return ToolError(f"{error.code}: {error}")
    # UUID / enum parse failures surface as a typed invalid_argument error, not
    # a generic transport failure (P2).
    return ToolError(f"invalid_argument: {error}")


def build_server(
    tools: McpTools,
    resolve: PrincipalResolver,
    consume_usage: Callable[[Principal, str], None],
    *,
    v3_tools: McpV3Tools | None = None,
    name: str = "fitcheck",
    host: str = "127.0.0.1",
    port: int = 8000,
    stateless_http: bool = False,
    json_response: bool = False,
) -> FastMCP:
    """Register the ten legacy tools and, when supplied, ten additive v3 tools.

    Every call resolves Principal -> tool adapter -> response dict; typed
    McpError values become ToolError values. A7 guards run inside the adapters.

    Transport binding is parameterized: the defaults (127.0.0.1:8000, stateful)
    preserve local/stdio behavior, while the production entrypoint passes
    host="0.0.0.0", port=$PORT, stateless_http=True for Cloud Run (which routes
    to 0.0.0.0:$PORT and autoscales across instances with no shared session
    state)."""
    mcp = FastMCP(
        name,
        instructions=V3_SERVER_INSTRUCTIONS if v3_tools is not None else None,
        host=host,
        port=port,
        stateless_http=stateless_http,
        json_response=json_response,
    )

    if frozenset(ALL_TOOL_NAMES) != frozenset(TOOL_COST_UNITS):
        raise RuntimeError("MCP tool-cost map does not cover the registered surface")

    def _principal_for(ctx: Context, tool_name: str) -> Principal:
        principal = resolve(ctx)
        consume_usage(principal, tool_name)
        return principal

    @mcp.tool(description="Normalize a messy claim into an extracted structure.")
    def normalize_claim(input_text: str, ctx: Context) -> dict:
        try:
            return tools.normalize_claim(_principal_for(ctx, "normalize_claim"), input_text=input_text).model_dump(
                mode="json"
            )
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    @mcp.tool(description="Preview the Market Fit Card (odds always withheld).")
    def preview_market_fit(thesis_analysis_id: str, ctx: Context) -> dict:
        try:
            return tools.preview_market_fit(
                _principal_for(ctx, "preview_market_fit"), thesis_analysis_id=uuid.UUID(thesis_analysis_id)
            ).model_dump(mode="json")
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    @mcp.tool(description="Preview a draft contract for a no-clean card.")
    def draft_contract_preview(fit_card_id: str, ctx: Context) -> dict:
        try:
            return tools.draft_contract_preview(
                _principal_for(ctx, "draft_contract_preview"), fit_card_id=uuid.UUID(fit_card_id)
            ).model_dump(mode="json")
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    @mcp.tool(description="Record a blind prior before odds are revealed.")
    def submit_blind_prior(
        thesis_analysis_id: str,
        prior_probability: float,
        ctx: Context,
        prior_confidence: str | None = None,
        prior_reason: str | None = None,
    ) -> dict:
        try:
            return tools.submit_blind_prior(
                _principal_for(ctx, "submit_blind_prior"),
                thesis_analysis_id=uuid.UUID(thesis_analysis_id),
                prior_probability=prior_probability,
                prior_confidence=prior_confidence,
                prior_reason=prior_reason,
            ).model_dump(mode="json")
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    @mcp.tool(
        description="Full Market Fit Card; requires a blind prior for odds."
    )
    def classify_market_fit(thesis_analysis_id: str, ctx: Context) -> dict:
        try:
            return tools.classify_market_fit(
                _principal_for(ctx, "classify_market_fit"), thesis_analysis_id=uuid.UUID(thesis_analysis_id)
            ).model_dump(mode="json")
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    @mcp.tool(description="Save a ledger entry with conviction + exposure.")
    def create_ledger_entry(
        fit_card_id: str,
        conviction_level: str,
        intended_exposure_bucket: str,
        user_justification: str,
        ctx: Context,
    ) -> dict:
        try:
            return tools.create_ledger_entry(
                _principal_for(ctx, "create_ledger_entry"),
                fit_card_id=uuid.UUID(fit_card_id),
                conviction_level=conviction_level,
                intended_exposure_bucket=intended_exposure_bucket,
                user_justification=user_justification,
            ).model_dump(mode="json")
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    @mcp.tool(description="Read one of the caller's ledger entries.")
    def get_ledger_entry(ledger_entry_id: str, ctx: Context) -> dict:
        try:
            return tools.get_ledger_entry(
                _principal_for(ctx, "get_ledger_entry"), ledger_entry_id=uuid.UUID(ledger_entry_id)
            ).model_dump(mode="json")
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    @mcp.tool(description="List the caller's ledger entries.")
    def get_ledger_entries(ctx: Context) -> list[dict]:
        try:
            return [e.model_dump(mode="json") for e in tools.get_ledger_entries(_principal_for(ctx, "get_ledger_entries"))]
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    @mcp.tool(description="Submit a fit correction (review candidate intake).")
    def correct_fit(
        fit_card_id: str,
        corrected_class: str,
        ctx: Context,
        notes: str | None = None,
    ) -> dict:
        try:
            return tools.correct_fit(
                _principal_for(ctx, "correct_fit"),
                fit_card_id=uuid.UUID(fit_card_id),
                corrected_class=corrected_class,
                notes=notes,
            ).model_dump(mode="json")
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    @mcp.tool(description="Reject a market (validated-rejection intake).")
    def reject_market(
        fit_card_id: str, market_id: str, reason: str, ctx: Context
    ) -> dict:
        try:
            return tools.reject_market(
                _principal_for(ctx, "reject_market"),
                fit_card_id=uuid.UUID(fit_card_id),
                market_id=market_id,
                reason=reason,
            ).model_dump(mode="json")
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    if v3_tools is not None:
        read_only = ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
        local_idempotent_write = ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
        durable_model_submission = ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
        model_non_idempotent_write = ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        )

        @mcp.tool(
            description=(
                "Queue durable v3 source interpretation. Poll status; this "
                "call never waits for a model result."
            ),
            annotations=durable_model_submission,
        )
        def v3_submit_source_interpretation(
            input_text: V3InputText,
            idempotency_key: V3IdempotencyKey,
            ctx: Context,
            source_url: V3SourceUrl = None,
        ) -> dict:
            try:
                return v3_tools.submit_source_interpretation(
                    _principal_for(ctx, "v3_submit_source_interpretation"),
                    input_text=input_text,
                    source_url=source_url,
                    idempotency_key=idempotency_key,
                ).model_dump(mode="json")
            except (McpError, ValueError, TypeError) as e:
                raise _fail(e)

        @mcp.tool(
            description="Read an owned durable v3 source job by job ID.",
            annotations=read_only,
        )
        def v3_get_source_interpretation_job(
            job_id: uuid.UUID, ctx: Context
        ) -> dict:
            try:
                return v3_tools.get_source_interpretation_job(
                    _principal_for(ctx, "v3_get_source_interpretation_job"), job_id=job_id
                ).model_dump(mode="json")
            except (McpError, ValueError, TypeError) as e:
                raise _fail(e)

        @mcp.tool(
            description=(
                "Recover an owned durable v3 source job by idempotency key."
            ),
            annotations=read_only,
        )
        def v3_get_source_interpretation_job_by_idempotency(
            idempotency_key: V3IdempotencyKey, ctx: Context
        ) -> dict:
            try:
                return v3_tools.get_source_interpretation_job_by_idempotency(
                    _principal_for(ctx, "v3_get_source_interpretation_job_by_idempotency"), idempotency_key=idempotency_key
                ).model_dump(mode="json")
            except (McpError, ValueError, TypeError) as e:
                raise _fail(e)

        @mcp.tool(
            description=(
                "Relay the user's explicit candidate-or-none decision over "
                "the exact displayed source interpretation. The agent call "
                "is not direct human attestation."
            ),
            annotations=local_idempotent_write,
        )
        def v3_choose_source_candidate(
            source_interpretation_id: uuid.UUID,
            selection_kind: Literal["candidate", "none"],
            ctx: Context,
            source_thesis_candidate_id: uuid.UUID | None = None,
            reason: V3Reason = None,
        ) -> dict:
            try:
                return v3_tools.choose_source_candidate(
                    _principal_for(ctx, "v3_choose_source_candidate"),
                    source_interpretation_id=source_interpretation_id,
                    selection_kind=selection_kind,
                    source_thesis_candidate_id=source_thesis_candidate_id,
                    reason=reason,
                ).model_dump(mode="json")
            except (McpError, ValueError, TypeError) as e:
                raise _fail(e)

        @mcp.tool(
            description=(
                "Propose normalization only for the exact source candidate "
                "previously selected through the v3 gate."
            ),
            annotations=model_non_idempotent_write,
        )
        def v3_propose_selected_normalization(
            source_thesis_candidate_id: uuid.UUID, ctx: Context
        ) -> dict:
            try:
                return v3_tools.propose_selected_normalization(
                    _principal_for(ctx, "v3_propose_selected_normalization"),
                    source_thesis_candidate_id=source_thesis_candidate_id,
                ).model_dump(mode="json")
            except (McpError, ValueError, TypeError) as e:
                raise _fail(e)

        @mcp.tool(
            description=(
                "Relay the user's revision or clarification answer. It "
                "creates a successor proposal and never accepts it."
            ),
            annotations=model_non_idempotent_write,
        )
        def v3_revise_normalization(
            normalization_attempt_id: uuid.UUID,
            expected_input_digest: V3InputDigest,
            input_text: V3InputText,
            ctx: Context,
            source_url: V3SourceUrl = None,
        ) -> dict:
            try:
                return v3_tools.revise_normalization(
                    _principal_for(ctx, "v3_revise_normalization"),
                    normalization_attempt_id=normalization_attempt_id,
                    expected_input_digest=expected_input_digest,
                    input_text=input_text,
                    source_url=source_url,
                ).model_dump(mode="json")
            except (McpError, ValueError, TypeError) as e:
                raise _fail(e)

        @mcp.tool(
            description=(
                "Relay the user's explicit acceptance of the exact current "
                "normalization digest. The agent call is not direct human "
                "attestation."
            ),
            annotations=local_idempotent_write,
        )
        def v3_accept_normalization(
            normalization_attempt_id: uuid.UUID,
            expected_input_digest: V3InputDigest,
            ctx: Context,
        ) -> dict:
            try:
                return v3_tools.accept_normalization(
                    _principal_for(ctx, "v3_accept_normalization"),
                    normalization_attempt_id=normalization_attempt_id,
                    expected_input_digest=expected_input_digest,
                ).model_dump(mode="json")
            except (McpError, ValueError, TypeError) as e:
                raise _fail(e)

        @mcp.tool(
            description=(
                "Relay the user's explicit rejection of the exact current "
                "normalization digest. The agent call is not direct human "
                "attestation."
            ),
            annotations=local_idempotent_write,
        )
        def v3_reject_normalization(
            normalization_attempt_id: uuid.UUID,
            expected_input_digest: V3InputDigest,
            ctx: Context,
            reason: V3Reason = None,
        ) -> dict:
            try:
                return v3_tools.reject_normalization(
                    _principal_for(ctx, "v3_reject_normalization"),
                    normalization_attempt_id=normalization_attempt_id,
                    expected_input_digest=expected_input_digest,
                    reason=reason,
                ).model_dump(mode="json")
            except (McpError, ValueError, TypeError) as e:
                raise _fail(e)

        @mcp.tool(
            description=(
                "Assess and persist the v3 top-three market pool for an "
                "explicitly accepted thesis."
            ),
            annotations=model_non_idempotent_write,
        )
        def v3_assess_market_pool(
            thesis_analysis_id: uuid.UUID, ctx: Context
        ) -> dict:
            try:
                return v3_tools.assess_market_pool(
                    _principal_for(ctx, "v3_assess_market_pool"),
                    thesis_analysis_id=thesis_analysis_id,
                ).model_dump(mode="json")
            except (McpError, ValueError, TypeError) as e:
                raise _fail(e)

        @mcp.tool(
            description=(
                "Relay the user's explicit displayed-market-or-none choice. "
                "The agent call is not direct human attestation."
            ),
            annotations=local_idempotent_write,
        )
        def v3_choose_market(
            market_display_set_id: uuid.UUID,
            selection_kind: Literal["market", "none"],
            ctx: Context,
            market_assessment_id: uuid.UUID | None = None,
            reason: V3Reason = None,
        ) -> dict:
            try:
                return v3_tools.choose_market(
                    _principal_for(ctx, "v3_choose_market"),
                    market_display_set_id=market_display_set_id,
                    selection_kind=selection_kind,
                    market_assessment_id=market_assessment_id,
                    reason=reason,
                ).model_dump(mode="json")
            except (McpError, ValueError, TypeError) as e:
                raise _fail(e)

    return mcp


# Tool names registered, for discovery + the smoke test.
TOOL_NAMES = (
    "normalize_claim",
    "preview_market_fit",
    "draft_contract_preview",
    "submit_blind_prior",
    "classify_market_fit",
    "create_ledger_entry",
    "get_ledger_entry",
    "get_ledger_entries",
    "correct_fit",
    "reject_market",
)
V3_TOOL_NAMES = (
    "v3_submit_source_interpretation",
    "v3_get_source_interpretation_job",
    "v3_get_source_interpretation_job_by_idempotency",
    "v3_choose_source_candidate",
    "v3_propose_selected_normalization",
    "v3_revise_normalization",
    "v3_accept_normalization",
    "v3_reject_normalization",
    "v3_assess_market_pool",
    "v3_choose_market",
)

ALL_TOOL_NAMES = TOOL_NAMES + V3_TOOL_NAMES

V3_SERVER_INSTRUCTIONS = (
    "Use only v3_* tools for the governed v3 journey. Submit source "
    "interpretation, poll status, show every candidate without choosing a "
    "priority, and wait for an explicit user message before each source "
    "choice, normalization revision/accept/reject, and market choice. Treat "
    "decision calls as authenticated agent relays, never direct human "
    "attestation. Only accepted normalization may reach the market pool. "
    "Call market assessment once and retain its returned display-set ID."
)
