"""FastMCP transport for the FitCheck MCP tools (step 7 C2).

Thin transport: resolve the api-key Principal, atomically admit usage, dispatch
to McpTools (which owns the field-aware A7 guard + blind-prior protocol), and
map typed McpError to MCP tool errors. Primary transport = Streamable HTTP on
Cloud Run; stdio for local dev. The server owns transport admission only;
McpTools remains the domain composition root.
"""

import uuid
from typing import Callable

from mcp.server.fastmcp import Context, FastMCP

from el.mcp.auth import Principal, resolve_principal
from el.mcp.contracts import McpError
from el.mcp.rate_limit import TOOL_COST_UNITS
from el.mcp.tools import McpTools

try:  # surface typed errors as MCP tool errors when the SDK exposes it
    from mcp.server.fastmcp.exceptions import ToolError
except Exception:  # pragma: no cover - SDK-version fallback
    ToolError = RuntimeError  # type: ignore[assignment, misc]

# Resolves the authenticated Principal for a request. In prod this reads the
# api key from the request headers; injected (stubbed) in tests.
PrincipalResolver = Callable[[Context], Principal]
UsageConsumer = Callable[[Principal, str], None]


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
    consume_usage: UsageConsumer,
    *,
    name: str = "fitcheck",
    host: str = "127.0.0.1",
    port: int = 8000,
    stateless_http: bool = False,
    json_response: bool = False,
) -> FastMCP:
    """Register the 10 Phase-1 tools, each: resolve Principal -> atomically
    consume usage -> McpTools -> dict; typed McpError -> ToolError. The A7
    guard runs inside McpTools.

    Transport binding is parameterized: the defaults (127.0.0.1:8000, stateful)
    preserve local/stdio behavior, while the production entrypoint passes
    host="0.0.0.0", port=$PORT, stateless_http=True for Cloud Run (which routes
    to 0.0.0.0:$PORT and autoscales across instances with no shared session
    state)."""
    if frozenset(TOOL_NAMES) != frozenset(TOOL_COST_UNITS):
        raise RuntimeError("every MCP tool must have exactly one usage weight")

    mcp = FastMCP(
        name,
        host=host,
        port=port,
        stateless_http=stateless_http,
        json_response=json_response,
    )

    def _principal_for(ctx: Context, tool_name: str) -> Principal:
        principal = resolve(ctx)
        consume_usage(principal, tool_name)
        return principal

    @mcp.tool(description="Normalize a messy claim into an extracted structure.")
    def normalize_claim(input_text: str, ctx: Context) -> dict:
        try:
            return tools.normalize_claim(
                _principal_for(ctx, "normalize_claim"), input_text=input_text
            ).model_dump(mode="json")
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    @mcp.tool(description="Preview the Market Fit Card (odds always withheld).")
    def preview_market_fit(thesis_analysis_id: str, ctx: Context) -> dict:
        try:
            return tools.preview_market_fit(
                _principal_for(ctx, "preview_market_fit"),
                thesis_analysis_id=uuid.UUID(thesis_analysis_id),
            ).model_dump(mode="json")
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    @mcp.tool(description="Preview a draft contract for a no-clean card.")
    def draft_contract_preview(fit_card_id: str, ctx: Context) -> dict:
        try:
            return tools.draft_contract_preview(
                _principal_for(ctx, "draft_contract_preview"),
                fit_card_id=uuid.UUID(fit_card_id),
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
                _principal_for(ctx, "classify_market_fit"),
                thesis_analysis_id=uuid.UUID(thesis_analysis_id),
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
                _principal_for(ctx, "get_ledger_entry"),
                ledger_entry_id=uuid.UUID(ledger_entry_id),
            ).model_dump(mode="json")
        except (McpError, ValueError, TypeError) as e:
            raise _fail(e)

    @mcp.tool(description="List the caller's ledger entries.")
    def get_ledger_entries(ctx: Context) -> list[dict]:
        try:
            return [
                e.model_dump(mode="json")
                for e in tools.get_ledger_entries(
                    _principal_for(ctx, "get_ledger_entries")
                )
            ]
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
