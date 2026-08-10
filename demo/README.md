# FitCheck MCP Demo

This package is a demo composition root and two clients for the Step-7 MCP
surface. It does not add domain logic; it wires the real `el` services behind
`el.mcp.server.build_server`.

## Modes

| Env var | Default | Meaning |
| --- | --- | --- |
| `FITCHECK_DB_URL` | `sqlite:///demo.db` | Demo database URL |
| `FITCHECK_PROPOSER` | `fixture` | `fixture` or `gemini` |
| `FITCHECK_MCP_TRANSPORT` | `stdio` | Server transport: `stdio`, `streamable-http`, or compatibility-only `sse` |
| `FITCHECK_MCP_URL` | unset | Client URL for an already-running HTTP/SSE server |
| `FITCHECK_MCP_CLIENT_TRANSPORT` | `streamable-http` | Client URL transport: `streamable-http` or compatibility-only `sse` |
| `FITCHECK_API_KEY` | generated | Demo API key seeded into the DB |
| `FITCHECK_API_KEY_HEADER` | `x-api-key` | Header used outside stdio |
| `FITCHECK_CLOUD_RUN_ID_TOKEN` | unset | Short-lived Google ID token for a direct IAM-private Cloud Run connection |
| `FITCHECK_CLOUD_RUN_AUDIENCE` | unset | Exact Cloud Run service origin; mint an ID token from service-account or metadata-server ADC |
| `FITCHECK_ADK_MODEL` | `gemini-2.5-flash` | ADK agent model |

`stdio + fixture` is the default because it is deterministic, self-contained,
and requires no model key. Streamable HTTP is the deployment-shaped transport.
SSE is exposed only because some demo clients still force it; do not lead with
it.

## Scripted Client

No LLM, no keys:

```bash
python -m demo.client_scripted
```

It spawns `python -m demo.server` over stdio, runs:

1. `normalize_claim`
2. `preview_market_fit`
3. `submit_blind_prior`
4. `classify_market_fit`
5. `create_ledger_entry`
6. `get_ledger_entries`

The smoke asserts the blind-prior invariant: preview withholds odds, classify
reveals thesis-side odds after the prior.

## Streamable HTTP

In one terminal:

```bash
FITCHECK_MCP_TRANSPORT=streamable-http FITCHECK_API_KEY=demo-key python -m demo.server
```

In another:

```bash
FITCHECK_MCP_URL=http://127.0.0.1:8000/mcp FITCHECK_API_KEY=demo-key python -m demo.client_scripted
```

## ADK Gemini Client

Install the optional demo dependency:

```bash
pip install -e ".[demo]"
```

Then run the ADK client:

```bash
GOOGLE_API_KEY=... GOOGLE_GENAI_USE_VERTEXAI=0 python -m demo.client_adk
```

By default, the ADK client consumes the local stdio fixture server. To consume
a deployed IAM-private Streamable HTTP server directly, send the FitCheck key
in `x-api-key` and a Google ID token in `Authorization`:

```bash
FITCHECK_MCP_URL=https://SERVICE_URL/mcp \
FITCHECK_API_KEY=... \
FITCHECK_CLOUD_RUN_ID_TOKEN="$(gcloud auth print-identity-token)" \
GOOGLE_API_KEY=... \
python -m demo.client_adk
```

`FITCHECK_CLOUD_RUN_ID_TOKEN` is short-lived. For service-account ADC or a
Google-hosted runtime, set `FITCHECK_CLOUD_RUN_AUDIENCE=https://SERVICE_URL`
instead and the client will mint the token through `google-auth`. The audience
must be the service origin without `/mcp`; set only one token mode.

For local development, the simplest alternative is an authenticated proxy;
the client then connects to localhost and needs no Cloud Run token setting:

```bash
gcloud run services proxy SERVICE --project=PROJECT --region=REGION --port=8080
FITCHECK_MCP_URL=http://127.0.0.1:8080/mcp FITCHECK_API_KEY=... python -m demo.client_scripted
```

Direct IAM-private access is intentionally incompatible with
`FITCHECK_API_KEY_HEADER=Authorization`; keep the application key in
`x-api-key` so `Authorization` remains available for the Google ID token.

Set `FITCHECK_PROPOSER=gemini` when you want the server itself to make Gemini
proposer calls. In fixture mode, the ADK agent may still call Gemini, but the
FitCheck backend remains deterministic.
