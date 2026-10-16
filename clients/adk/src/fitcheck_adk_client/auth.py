"""Optional Google Cloud Run authentication for the isolated ADK client."""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit


class CloudRunAuthError(RuntimeError):
    """Raised when optional Cloud Run authentication is misconfigured."""


def _cloud_run_audience(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise CloudRunAuthError(
            "FITCHECK_CLOUD_RUN_AUDIENCE must be the HTTPS Cloud Run service "
            "URL with no path, query, fragment, credentials, or trailing slash."
        )
    return value


def _https_endpoint_origin(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise CloudRunAuthError(
            "Cloud Run authentication requires FITCHECK_MCP_URL to be an "
            "HTTPS URL without embedded credentials or a fragment."
        )
    return f"{parts.scheme}://{parts.netloc}"


def _fetch_google_id_token(audience: str) -> str:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token
    except ImportError as exc:
        raise CloudRunAuthError(
            "FITCHECK_CLOUD_RUN_AUDIENCE requires google-auth."
        ) from exc

    try:
        token = id_token.fetch_id_token(Request(), audience)
    except Exception as exc:
        raise CloudRunAuthError(
            "Could not obtain a Google ID token. Configure service-account "
            "ADC, use an authorized Google-hosted identity, or provide "
            "FITCHECK_CLOUD_RUN_ID_TOKEN."
        ) from exc
    if not token:
        raise CloudRunAuthError("Google authentication returned an empty ID token.")
    return token


def _explicit_id_token(value: str) -> str:
    if value.lower().startswith("bearer "):
        raise CloudRunAuthError(
            "FITCHECK_CLOUD_RUN_ID_TOKEN must omit the 'Bearer ' prefix."
        )
    if any(character.isspace() for character in value):
        raise CloudRunAuthError(
            "FITCHECK_CLOUD_RUN_ID_TOKEN must not contain whitespace."
        )
    return value


def remote_mcp_headers(
    *,
    api_key: str,
    api_key_header: str,
    endpoint_url: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build application and optional Cloud Run headers for an MCP client."""

    env = os.environ if environ is None else environ
    explicit_token = env.get("FITCHECK_CLOUD_RUN_ID_TOKEN", "")
    audience = env.get("FITCHECK_CLOUD_RUN_AUDIENCE", "")
    if explicit_token and audience:
        raise CloudRunAuthError(
            "Set only one of FITCHECK_CLOUD_RUN_ID_TOKEN and "
            "FITCHECK_CLOUD_RUN_AUDIENCE."
        )

    cloud_run_auth_enabled = bool(explicit_token or audience)
    if cloud_run_auth_enabled and api_key_header.casefold() == "authorization":
        raise CloudRunAuthError(
            "FITCHECK_API_KEY_HEADER cannot be Authorization when Cloud Run "
            "IAM authentication is enabled."
        )

    headers = {api_key_header: api_key}
    if not cloud_run_auth_enabled:
        return headers

    endpoint_origin = _https_endpoint_origin(endpoint_url)
    if explicit_token:
        token = _explicit_id_token(explicit_token)
    else:
        validated_audience = _cloud_run_audience(audience)
        if validated_audience != endpoint_origin:
            raise CloudRunAuthError(
                "FITCHECK_CLOUD_RUN_AUDIENCE must exactly match the origin of "
                "FITCHECK_MCP_URL; keep /mcp only on FITCHECK_MCP_URL."
            )
        token = _fetch_google_id_token(validated_audience)

    headers["Authorization"] = f"Bearer {token}"
    return headers
