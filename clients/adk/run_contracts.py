#!/usr/bin/env python3
"""Run isolated public ADK contracts offline; missing SDKs and skips fail."""

from __future__ import annotations

import argparse
import importlib
from importlib.machinery import PathFinder
from importlib import metadata
from pathlib import Path
import socket
import sys
from types import ModuleType
from typing import Any


CLIENT_PIN = ("fitcheck-adk-client", "0.1.0")
SDK_PINS = {
    "pydantic": ("2.13.5", "pydantic"),
    "google-auth": ("2.62.0", "google.auth"),
    "google-genai": ("2.20.0", "google.genai"),
    "mcp": ("1.30.0", "mcp"),
    "pytest": ("9.1.1", "pytest"),
    "google-adk": ("2.2.0", "google.adk"),
}


class OfflineFence:
    def __init__(self) -> None:
        self._originals: list[tuple[object, str, object]] = []

    @staticmethod
    def _deny(*_args, **_kwargs):
        raise AssertionError("ADK client contracts must not access the network")

    def install(self) -> None:
        for owner, name in (
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
        ):
            self._originals.append((owner, name, getattr(owner, name)))
            setattr(owner, name, self._deny)

    def restore(self) -> None:
        for owner, name, original in reversed(self._originals):
            setattr(owner, name, original)
        self._originals.clear()


class NoSkips:
    def __init__(self) -> None:
        self.skips = 0

    def pytest_runtest_logreport(self, report) -> None:
        if report.skipped:
            self.skips += 1

    def pytest_collectreport(self, report) -> None:
        if report.skipped:
            self.skips += 1

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        del exitstatus
        if self.skips or not session.testscollected:
            session.exitstatus = 1
            print(
                f"ADK client contracts failed: {self.skips} skips; "
                f"{session.testscollected} collected tests"
            )


def _installed_distribution(distribution: str, expected: str):
    try:
        installed = metadata.distribution(distribution)
    except metadata.PackageNotFoundError:
        raise RuntimeError(
            f"requires {distribution}=={expected}; dependency is absent"
        ) from None
    if installed.version != expected:
        raise RuntimeError(
            f"requires {distribution}=={expected}; found {installed.version}"
        )
    return installed


def _owned_files(distribution: Any, *, name: str) -> frozenset[Path]:
    if not distribution.files:
        raise RuntimeError(f"cannot verify files owned by {name}")
    return frozenset(
        Path(distribution.locate_file(item)).resolve()
        for item in distribution.files
    )


def _unimported_spec(module: str, *, source_root: Path, distribution_root: Path):
    search_path = None
    fullname = ""
    for index, part in enumerate(module.split(".")):
        fullname = part if not fullname else f"{fullname}.{part}"
        spec = PathFinder.find_spec(fullname, search_path)
        if spec is None:
            raise RuntimeError(f"cannot locate required SDK module {module}")
        origin = getattr(spec, "origin", None)
        if origin not in (None, "built-in", "frozen"):
            resolved = Path(origin).resolve()
            if resolved.is_relative_to(source_root) and not resolved.is_relative_to(
                distribution_root
            ):
                raise RuntimeError(
                    f"rejected local shadow SDK package {fullname} from {resolved}"
                )
        if spec.submodule_search_locations is not None:
            for location in spec.submodule_search_locations:
                resolved = Path(location).resolve()
                if resolved.is_relative_to(
                    source_root
                ) and not resolved.is_relative_to(distribution_root):
                    raise RuntimeError(
                        "rejected local SDK namespace search location "
                        f"{resolved} for {fullname}"
                    )
        if index < len(module.split(".")) - 1:
            locations = spec.submodule_search_locations
            if locations is None:
                raise RuntimeError(f"cannot locate required SDK module {module}")
            search_path = list(locations)
    return spec


def _verify_sdk_import(
    *,
    distribution_name: str,
    expected: str,
    module_name: str,
    distribution: Any,
    source_root: Path,
) -> ModuleType:
    distribution_root = Path(distribution.locate_file("")).resolve()
    owned = _owned_files(distribution, name=distribution_name)
    spec = _unimported_spec(
        module_name,
        source_root=source_root,
        distribution_root=distribution_root,
    )
    origin = getattr(spec, "origin", None)
    if not origin or Path(origin).resolve() not in owned:
        raise RuntimeError(
            f"rejected SDK module {module_name} from {origin}; not owned by "
            f"{distribution_name}=={expected}"
        )
    module = importlib.import_module(module_name)
    imported_origin = getattr(module, "__file__", None)
    if not imported_origin or Path(imported_origin).resolve() not in owned:
        raise RuntimeError(
            f"rejected imported SDK module {module_name} from "
            f"{imported_origin}; not owned by {distribution_name}=={expected}"
        )
    return module


def _require_pins(*, source_root: Path) -> Any:
    client_name, client_version = CLIENT_PIN
    client_distribution = _installed_distribution(client_name, client_version)
    print(f"ADK client contract: {client_name}=={client_version}")
    for distribution_name, (expected, module_name) in SDK_PINS.items():
        distribution = _installed_distribution(distribution_name, expected)
        _verify_sdk_import(
            distribution_name=distribution_name,
            expected=expected,
            module_name=module_name,
            distribution=distribution,
            source_root=source_root,
        )
        print(
            f"ADK client contract: {distribution_name}=={expected} "
            f"({module_name})"
        )
    return client_distribution


def _verify_client_origin(
    *,
    expected_source: Path,
    source_root: Path,
    distribution: Any,
    require_wheel_install: bool,
) -> None:
    package = importlib.import_module("fitcheck_adk_client")
    origin = Path(package.__file__).resolve()
    if require_wheel_install:
        owned = _owned_files(distribution, name=CLIENT_PIN[0])
        if origin not in owned:
            raise RuntimeError(
                "fitcheck_adk_client import is not owned by the installed "
                f"{CLIENT_PIN[0]} distribution"
            )
        if origin.is_relative_to(source_root):
            raise RuntimeError(
                "fitcheck_adk_client wheel smoke resolved inside the checkout"
            )
    elif not origin.is_relative_to(expected_source):
        raise RuntimeError(
            "fitcheck_adk_client import does not originate from clients/adk/src"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="public checkout root",
    )
    parser.add_argument(
        "--require-wheel-install",
        action="store_true",
        help="require the client import to be owned by an installed wheel outside root",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    client_root = root / "clients" / "adk"
    tests = client_root / "tests"
    expected_source = (client_root / "src").resolve()

    fence = OfflineFence()
    fence.install()
    try:
        client_distribution = _require_pins(source_root=root)
        _verify_client_origin(
            expected_source=expected_source,
            source_root=root,
            distribution=client_distribution,
            require_wheel_install=args.require_wheel_install,
        )
        import pytest

        return int(pytest.main([str(tests), "-q", "-rs"], plugins=[NoSkips()]))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        fence.restore()


if __name__ == "__main__":
    raise SystemExit(main())
