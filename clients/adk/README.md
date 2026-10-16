# FitCheck ADK client

This subproject keeps the Google ADK client in an environment separate from
the FitCheck server. FitCheck pins the server telemetry API to 1.44.0, while
the tested ADK 2.2.0 dependency set requires an older telemetry range. Do not
install the two projects into the same virtual environment.

From a clean public checkout, create the server environment first:

```bash
python3.12 -m venv .venv-server
.venv-server/bin/python -m pip install -e .
```

Create the client environment independently:

```bash
python3.12 -m venv .venv-adk
.venv-adk/bin/python -m pip install -e ./clients/adk
```

For local STDIO, the client must be told which Python owns the server and
which checkout contains `demo.server`:

```bash
FITCHECK_SERVER_PYTHON="$PWD/.venv-server/bin/python" \
FITCHECK_SERVER_ROOT="$PWD" \
GOOGLE_API_KEY=... \
.venv-adk/bin/fitcheck-adk-client
```

The client does not copy its environment into the STDIO server. To pass a
server-only setting explicitly, prefix its name with `FITCHECK_SERVER_ENV_`;
the client strips that prefix for the child process. For example,
`FITCHECK_SERVER_ENV_DATABASE_URL=...` becomes `DATABASE_URL=...`. Client
credentials, Cloud Run tokens, ADC paths, `PYTHONPATH`, and unrelated settings
are not forwarded unless they are separately supplied with this prefix. An
explicit `FITCHECK_SERVER_ENV_FITCHECK_PROPOSER` takes precedence over the
legacy `FITCHECK_PROPOSER` setting. `FITCHECK_API_KEY` and
`FITCHECK_MCP_TRANSPORT` remain client-managed and cannot be overridden through
the prefix.

For an already running Streamable HTTP server, the client environment does
not need the server package:

```bash
FITCHECK_MCP_URL=http://127.0.0.1:8000/mcp \
FITCHECK_API_KEY=... \
GOOGLE_API_KEY=... \
.venv-adk/bin/fitcheck-adk-client
```

An IAM-private Cloud Run endpoint may use either a short-lived explicit ID
token or service-account/metadata-server ADC. Keep the FitCheck application
key outside the `Authorization` header:

```bash
FITCHECK_MCP_URL=https://SERVICE_URL/mcp \
FITCHECK_API_KEY=... \
FITCHECK_CLOUD_RUN_ID_TOKEN="$(gcloud auth print-identity-token)" \
GOOGLE_API_KEY=... \
.venv-adk/bin/fitcheck-adk-client
```

`FITCHECK_CLOUD_RUN_ID_TOKEN` and `FITCHECK_CLOUD_RUN_AUDIENCE` are mutually
exclusive. The audience form must be the exact HTTPS service origin without
`/mcp`.

The client invokes Gemini. Installing it and running the credential-free
contracts does not make a provider call; invoking `fitcheck-adk-client` does.
