"""The default telemetry path must work in an environment without the SDK."""

from __future__ import annotations

import importlib.util

import pytest

from el.observability import configure_otlp_from_environment


def test_disabled_telemetry_neither_requires_sdk_nor_initializes_exporter(monkeypatch):
    if importlib.util.find_spec("opentelemetry.sdk") is not None:
        pytest.skip("the dedicated no-SDK CI environment runs this proof")
    monkeypatch.delenv("FITCHECK_TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("FITCHECK_TELEMETRY_EXPORTER", raising=False)
    monkeypatch.delenv("FITCHECK_TELEMETRY_OTLP_ENDPOINT", raising=False)

    telemetry = configure_otlp_from_environment()

    assert telemetry.enabled is False
    assert telemetry.shutdown(timeout_seconds=0.01)
