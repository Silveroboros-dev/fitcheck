"""Assurance checks for the standalone ADK contract runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


RUNNER_PATH = Path(__file__).resolve().parents[1] / "run_contracts.py"
SPEC = importlib.util.spec_from_file_location("fitcheck_adk_contract_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _distribution(site_packages: Path, module_name: str):
    return SimpleNamespace(
        version="1.0.0",
        files=(Path(f"{module_name}.py"),),
        locate_file=lambda path: site_packages / path,
    )


def test_local_shadow_is_rejected_before_its_import_side_effect(monkeypatch, tmp_path):
    shadow = tmp_path / "shadow_sdk.py"
    marker = tmp_path / "shadow-imported"
    shadow.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n",
        encoding="utf-8",
    )
    site_packages = tmp_path / ".venv" / "site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "shadow_sdk.py").write_text("VALUE = 'installed'\n")
    monkeypatch.syspath_prepend(tmp_path)

    with pytest.raises(RuntimeError, match="local shadow SDK package"):
        runner._verify_sdk_import(
            distribution_name="shadow-distribution",
            expected="1.0.0",
            module_name="shadow_sdk",
            distribution=_distribution(site_packages, "shadow_sdk"),
            source_root=tmp_path,
        )

    assert not marker.exists()


def test_distribution_owned_module_origin_is_accepted(monkeypatch, tmp_path):
    site_packages = tmp_path / ".venv" / "site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "owned_sdk.py").write_text("VALUE = 'installed'\n")
    monkeypatch.syspath_prepend(site_packages)

    module = runner._verify_sdk_import(
        distribution_name="owned-distribution",
        expected="1.0.0",
        module_name="owned_sdk",
        distribution=_distribution(site_packages, "owned_sdk"),
        source_root=tmp_path,
    )

    assert module.VALUE == "installed"


def test_source_namespace_location_is_rejected_before_target_import(
    monkeypatch, tmp_path
):
    source_namespace = tmp_path / "shadow_namespace"
    source_namespace.mkdir()
    marker = tmp_path / "namespace-helper-imported"
    (source_namespace / "helper.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n",
        encoding="utf-8",
    )
    site_packages = tmp_path / ".venv" / "site-packages"
    installed_namespace = site_packages / "shadow_namespace"
    installed_namespace.mkdir(parents=True)
    target = installed_namespace / "target.py"
    target.write_text("from shadow_namespace import helper\n", encoding="utf-8")
    monkeypatch.syspath_prepend(site_packages)
    monkeypatch.syspath_prepend(tmp_path)
    distribution = SimpleNamespace(
        version="1.0.0",
        files=(Path("shadow_namespace/target.py"),),
        locate_file=lambda path: site_packages / path,
    )

    with pytest.raises(RuntimeError, match="namespace search location"):
        runner._verify_sdk_import(
            distribution_name="shadow-distribution",
            expected="1.0.0",
            module_name="shadow_namespace.target",
            distribution=distribution,
            source_root=tmp_path,
        )

    assert not marker.exists()


def test_wheel_client_origin_must_be_distribution_owned_and_outside_checkout(
    monkeypatch, tmp_path
):
    checkout = tmp_path / "checkout"
    expected_source = checkout / "clients" / "adk" / "src"
    site_packages = tmp_path / "wheel-venv" / "site-packages"
    installed = site_packages / "fitcheck_adk_client" / "__init__.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("", encoding="utf-8")
    distribution = SimpleNamespace(
        files=(Path("fitcheck_adk_client/__init__.py"),),
        locate_file=lambda path: site_packages / path,
    )
    monkeypatch.setitem(
        sys.modules,
        "fitcheck_adk_client",
        SimpleNamespace(__file__=str(installed)),
    )

    runner._verify_client_origin(
        expected_source=expected_source,
        source_root=checkout,
        distribution=distribution,
        require_wheel_install=True,
    )


def test_wheel_client_origin_rejects_checkout_source(monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    expected_source = checkout / "clients" / "adk" / "src"
    source_module = expected_source / "fitcheck_adk_client" / "__init__.py"
    source_module.parent.mkdir(parents=True)
    source_module.write_text("", encoding="utf-8")
    distribution = SimpleNamespace(
        files=(Path("fitcheck_adk_client/__init__.py"),),
        locate_file=lambda path: expected_source / path,
    )
    monkeypatch.setitem(
        sys.modules,
        "fitcheck_adk_client",
        SimpleNamespace(__file__=str(source_module)),
    )

    with pytest.raises(RuntimeError, match="resolved inside the checkout"):
        runner._verify_client_origin(
            expected_source=expected_source,
            source_root=checkout,
            distribution=distribution,
            require_wheel_install=True,
        )
