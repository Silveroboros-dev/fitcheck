from __future__ import annotations

import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path, PurePosixPath

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_public_export as public_export  # noqa: E402


def _png_chunk(chunk_type: bytes, data: bytes, *, corrupt_crc: bool = False) -> bytes:
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    if corrupt_crc:
        crc ^= 1
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _fixture_png(
    *,
    width: int = 640,
    height: int = 360,
    bit_depth: int = 8,
    color_type: int = 2,
    interlace: int = 0,
    before_idat: tuple[tuple[bytes, bytes], ...] = (),
    after_idat: tuple[tuple[bytes, bytes], ...] = (),
    corrupt_idat_crc: bool = False,
    trailing: bytes = b"",
) -> bytes:
    channels = {2: 3, 3: 1, 6: 4}.get(color_type, 4)
    ihdr = struct.pack(
        ">IIBBBBB",
        width,
        height,
        bit_depth,
        color_type,
        0,
        0,
        interlace,
    )
    rows = (b"\x00" + bytes(width * channels)) * height
    chunks = [_png_chunk(b"IHDR", ihdr)]
    chunks.extend(_png_chunk(kind, data) for kind, data in before_idat)
    chunks.append(
        _png_chunk(b"IDAT", zlib.compress(rows), corrupt_crc=corrupt_idat_crc)
    )
    chunks.extend(_png_chunk(kind, data) for kind, data in after_idat)
    chunks.append(_png_chunk(b"IEND", b""))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks) + trailing


def _write_fixture_repo(root: Path, *, blocker: bool = False) -> None:
    (root / "src").mkdir(parents=True)
    (root / "public").mkdir()
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "public" / "README.md").write_text("# Public\n", encoding="utf-8")
    license_name = "LICENSE_REQUIRED.md" if blocker else "LICENSE"
    (root / "public" / license_name).write_text("license decision\n", encoding="utf-8")
    (root / "public-export.toml").write_text(
        f"""manifest_version = 1
[export]
max_file_bytes = 1024
[[include]]
source = "public-export.toml"
destination = "public-export.toml"
[[include]]
source = "src"
destination = "src"
extensions = [".py"]
[[overlay]]
source = "public/README.md"
destination = "README.md"
[[overlay]]
source = "public/{license_name}"
destination = "{license_name}"
""",
        encoding="utf-8",
    )
    _, _, lock_content = public_export.generate_lockfile(
        root,
        allow_license_blocker=blocker,
    )
    (root / public_export.LOCK_FILENAME).write_bytes(lock_content)


def test_build_is_manifest_only_and_deterministic(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_fixture_repo(repo)
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir()
    monkeypatch.setattr(public_export, "PRIVATE_TMP_ROOT", private_tmp)

    _, files = public_export.prepare_export(repo)
    destination = private_tmp / "candidate"
    public_export.build_export(destination, files)

    assert sorted(
        item.relative_to(destination).as_posix()
        for item in destination.rglob("*")
        if item.is_file()
    ) == [
        "LICENSE",
        "PUBLIC_EXPORT_LOCK.json",
        "README.md",
        "public-export.toml",
        "src/app.py",
    ]
    assert (destination / public_export.LOCK_FILENAME).is_file()
    assert (destination / "src" / "app.py").read_text() == "VALUE = 1\n"
    assert int((destination / "src" / "app.py").stat().st_mtime) == 0
    with pytest.raises(public_export.BoundaryError, match="refusing to overwrite"):
        public_export.build_export(destination, files)


def test_check_only_does_not_create_destination(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_fixture_repo(repo)
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir()
    monkeypatch.setattr(public_export, "PRIVATE_TMP_ROOT", private_tmp)
    destination = private_tmp / "candidate"

    assert (
        public_export.main(
            [
                "--repo-root",
                str(repo),
                "--destination",
                str(destination),
                "--check-only",
            ]
        )
        == 0
    )
    assert not destination.exists()


@pytest.mark.parametrize(
    "unsafe",
    [
        "/" + "Users/alice/project/file.txt",
        "person" + "@gmail.com",
        "https://service-realhash-uc.a." + "run.app/mcp",
        "projects/" + "real-production-123/locations/us-central1",
        "https://" + "x.com/example/status/123",
    ],
)
def test_content_boundary_rejects_publication_identifiers(unsafe: str) -> None:
    with pytest.raises(public_export.BoundaryError):
        public_export._scan_text(unsafe.encode(), "unsafe.txt")


@pytest.mark.parametrize(
    "label",
    [
        "public/docs/assets/fitcheck-fixture-ui.png",
        "docs/assets/fitcheck-fixture-ui.png",
    ],
)
def test_content_boundary_accepts_only_the_strict_fixture_screenshot_png(
    label: str,
) -> None:
    public_export._scan_text(_fixture_png(), label)
    public_export._scan_text(
        _fixture_png(before_idat=((b"PLTE", b"\x00\x00\x00"),)),
        label,
    )
    public_export._scan_text(
        _fixture_png(
            color_type=3,
            before_idat=((b"PLTE", b"\x00\x00\x00"),),
        ),
        label,
    )


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (_fixture_png(corrupt_idat_crc=True), "CRC mismatch"),
        (
            _fixture_png(before_idat=((b"tEXt", b"Comment\x00private"),)),
            "forbidden PNG chunk",
        ),
        (
            _fixture_png(before_idat=((b"tRNS", b"\xff"),)),
            "forbidden PNG chunk",
        ),
        (_fixture_png(color_type=3), "required PLTE"),
        (_fixture_png(width=639), "dimensions"),
        (_fixture_png(height=359), "dimensions"),
        (_fixture_png(width=1921), "dimensions"),
        (_fixture_png(height=1201), "dimensions"),
        (_fixture_png(trailing=b"not-a-png-chunk"), "trailing data"),
    ],
)
def test_content_boundary_rejects_unsafe_fixture_screenshot_png(
    content: bytes,
    match: str,
) -> None:
    with pytest.raises(public_export.BoundaryError, match=match):
        public_export._scan_text(
            content,
            "public/docs/assets/fitcheck-fixture-ui.png",
        )


@pytest.mark.parametrize(
    "label",
    [
        "public/docs/assets/other.png",
        "docs/assets/other.png",
        "fitcheck-fixture-ui.png",
    ],
)
def test_content_boundary_rejects_png_binary_at_every_other_path(label: str) -> None:
    with pytest.raises(public_export.BoundaryError, match="non-UTF-8"):
        public_export._scan_text(_fixture_png(), label)


def test_placeholder_bypass_is_exact_not_a_substring() -> None:
    public_export._scan_text(
        b"projects/fitcheck-test/locations/us-central1",
        "synthetic.txt",
    )
    disguised_live = "projects/con" + "test-production-123/locations/us-central1"
    with pytest.raises(public_export.BoundaryError, match="live GCP identifier"):
        public_export._scan_text(disguised_live.encode(), "unsafe.txt")


@pytest.mark.parametrize(
    "unsafe",
    [
        "/" + "root/project/file.txt",
        "C:" + "/User" + "s/alice/project/file.txt",
        "C:" + "\\User" + "s\\alice\\project\\file.txt",
        "AKIA" + "A" * 16,
    ],
)
def test_expanded_content_detectors(unsafe: str) -> None:
    with pytest.raises(public_export.BoundaryError):
        public_export._scan_text(unsafe.encode(), "unsafe.txt")


@pytest.mark.parametrize(
    "unsafe_path",
    [".env.production", "state.db", "cache.sqlite3", "index.duckdb"],
)
def test_secret_and_database_paths_are_denied(unsafe_path: str) -> None:
    with pytest.raises(public_export.BoundaryError):
        public_export._assert_source_path_allowed(PurePosixPath(unsafe_path))
    with pytest.raises(public_export.BoundaryError):
        public_export._assert_destination_path_allowed(PurePosixPath(unsafe_path))


def test_denied_source_is_rejected_before_file_read(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "public").mkdir()
    (repo / "data" / "review.json").write_text("private\n")
    (repo / "public" / "README.md").write_text("# Public\n")
    (repo / "public-export.toml").write_text(
        """manifest_version = 1
[export]
max_file_bytes = 1024
[[include]]
source = "data/review.json"
destination = "review.json"
[[overlay]]
source = "public/README.md"
destination = "README.md"
"""
    )
    reads: list[str] = []
    original = public_export._read_rooted_regular_file

    def recording_read(
        root: Path,
        relative: PurePosixPath,
        limit: int,
        label: str,
    ) -> bytes:
        reads.append(label)
        return original(root, relative, limit, label)

    monkeypatch.setattr(public_export, "_read_rooted_regular_file", recording_read)
    with pytest.raises(public_export.BoundaryError, match="denied source path"):
        public_export.prepare_export(repo, allow_license_blocker=True)
    assert reads == ["public-export.toml"]


def test_license_blocker_requires_explicit_dry_run_override(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_fixture_repo(repo, blocker=True)

    with pytest.raises(public_export.BoundaryError, match="publication blocker"):
        public_export.prepare_export(repo)
    _, files = public_export.prepare_export(repo, allow_license_blocker=True)
    assert any(item.destination.name == "LICENSE_REQUIRED.md" for item in files)


def test_release_export_uses_verified_apache_license() -> None:
    if (ROOT / "public").is_dir():
        _, files = public_export.prepare_export(ROOT)
        by_destination = {
            item.destination.as_posix(): item.content for item in files
        }
    else:
        by_destination = {
            name: (ROOT / name).read_bytes()
            for name in (
                "LICENSE",
                "pyproject.toml",
                "Dockerfile",
                "DATA_CARD.md",
                "THIRD_PARTY_NOTICES.md",
                "el/product/demo_fixtures/hormuz_impact_copy.json",
                "el/product/demo_fixtures/hormuz_rules.json",
                "clients/adk/LICENSE",
                "clients/adk/pyproject.toml",
            )
        }

    assert "LICENSE_REQUIRED.md" not in by_destination
    assert hashlib.sha256(by_destination["LICENSE"]).hexdigest() == (
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
    )
    assert b'license = "Apache-2.0"' in by_destination["pyproject.toml"]
    assert by_destination["clients/adk/LICENSE"] == by_destination["LICENSE"]
    assert b'license = "Apache-2.0"' in by_destination[
        "clients/adk/pyproject.toml"
    ]
    assert b'license-files = ["LICENSE"]' in by_destination[
        "clients/adk/pyproject.toml"
    ]
    assert b'license-files = ["LICENSE", "THIRD_PARTY_NOTICES.md"]' in by_destination[
        "pyproject.toml"
    ]
    assert (
        b"COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./"
        in by_destination["Dockerfile"]
    )
    assert b"project-authored synthetic material" in by_destination["DATA_CARD.md"]

    notice = by_destination["THIRD_PARTY_NOTICES.md"]
    rules_bytes = by_destination["el/product/demo_fixtures/hormuz_rules.json"]
    impact_bytes = by_destination[
        "el/product/demo_fixtures/hormuz_impact_copy.json"
    ]
    assert b"el/product/demo_fixtures/hormuz_rules.json" in notice
    for digest in (
        b"56c45b0e41fdb0d1dd94654aff12fde49a8fc18fc71b168da148c4d84678da2f",
        b"967727828ff22a05e4807ef3728b95349fb007ce0d332804e3af45e27163da8b",
        b"cb371ec8e828421f47cbb688988125d12401981984ee047e317fc25d02bc0b07",
    ):
        assert digest in notice
    assert hashlib.sha256(rules_bytes).hexdigest() == (
        "56c45b0e41fdb0d1dd94654aff12fde49a8fc18fc71b168da148c4d84678da2f"
    )
    assert hashlib.sha256(impact_bytes).hexdigest() == (
        "0eab36b3ecbda1268d0565210d5275e4a54a9a0973cef8d1d040e7c29b34456e"
    )
    rules = json.loads(rules_bytes)
    assert hashlib.sha256(rules["old_rules"].encode()).hexdigest() == (
        "967727828ff22a05e4807ef3728b95349fb007ce0d332804e3af45e27163da8b"
    )
    assert hashlib.sha256(rules["new_rules"].encode()).hexdigest() == (
        "cb371ec8e828421f47cbb688988125d12401981984ee047e317fc25d02bc0b07"
    )


def test_release_docs_bound_historical_deployment_and_hormuz_exception() -> None:
    if (ROOT / "public").is_dir():
        _, files = public_export.prepare_export(ROOT)
        by_destination = {
            item.destination.as_posix(): item.content for item in files
        }
    else:
        by_destination = {
            name: (ROOT / name).read_bytes()
            for name in (
                "DATA_CARD.md",
                "PUBLIC_REPO_BOUNDARY.md",
                "docs/engineering-walkthrough.md",
            )
        }

    data_card = by_destination["DATA_CARD.md"]
    boundary = by_destination["PUBLIC_REPO_BOUNDARY.md"]
    walkthrough = by_destination["docs/engineering-walkthrough.md"]

    assert b"2026-10-16" in data_card
    assert b"sole external-content exception" in data_card
    assert b"el/product/demo_fixtures/hormuz_rules.json" in boundary
    assert b"one owner-directed external-content exception" in boundary
    for document in (boundary, data_card):
        assert b"hormuz-publication-risk-exception-v1" in document
        assert b"rights_verified=false" in document
        assert b"third_party_permission=unproven" in document
        assert b"owner_risk_accepted=true" in document
    approval_digest = (
        "f1f2031005287a70554541beb7457cb6bec39b955971b903aa6380a0087256cb"
    )
    assert approval_digest.encode() in boundary
    if (ROOT / "public").is_dir():
        approval_path = (
            "docs/release-decisions/hormuz-publication-risk-exception-v1.json"
        )
        assert approval_path not in by_destination
        assert hashlib.sha256((ROOT / approval_path).read_bytes()).hexdigest() == (
            approval_digest
        )
    assert (
        b"If release materials claim or link a live reference service" in boundary
    )
    assert b"gate is recorded as not applicable" in boundary
    assert b"June 2026 predecessor FitCheck MCP" in walkthrough
    assert b"deployment on Cloud Run" in walkthrough
    assert (
        b"does not provide or claim a current live reference service" in walkthrough
    )
    assert b"A reference service is operated separately" not in walkthrough


def test_release_export_replaces_external_market_bodies_with_synthetic_fixtures() -> None:
    if not (ROOT / "public").is_dir():
        pytest.skip("source-to-candidate boundary is verified in the private checkout")

    _, files = public_export.prepare_export(ROOT)
    by_destination = {
        item.destination.as_posix(): item.content for item in files
    }
    expected_fixtures = {
        "el/product/demo_fixtures/markets/synthetic-market-stablecoin-total-cap.json",
        "el/product/demo_fixtures/markets/synthetic-market-stablecoin-unit-cap.json",
        "el/product/demo_fixtures/markets/synthetic-market-treasury-ledger-payment.json",
        "el/product/demo_fixtures/markets/synthetic-market-broker-tokenized-asset.json",
        "el/product/demo_fixtures/markets/synthetic-market-broker-first-tokenized.json",
        "el/product/demo_fixtures/markets/synthetic-market-broker-chain-token.json",
        "el/product/demo_fixtures/markets/synthetic-event-stablecoin-cap.json",
        "el/product/demo_fixtures/markets/synthetic-event-tokenized-listing.json",
    }
    exported_fixtures = {
        path
        for path in by_destination
        if path.startswith("el/product/demo_fixtures/markets/synthetic-")
    }
    assert exported_fixtures == expected_fixtures


def test_public_ci_fences_offline_tests_and_smokes_built_wheels() -> None:
    if (ROOT / "public").is_dir():
        _, files = public_export.prepare_export(ROOT)
        shipped_workflow = next(
            item.content
            for item in files
            if item.destination.as_posix() == ".github/workflows/ci.yml"
        )
        workflows_with_postgres_gates = (
            (ROOT / ".github/workflows/public-export-verify.yml").read_bytes(),
            shipped_workflow,
        )
    else:
        shipped_workflow = (ROOT / ".github/workflows/ci.yml").read_bytes()
        workflows_with_postgres_gates = (shipped_workflow,)

    for workflow in workflows_with_postgres_gates:
        assert b"sudo unshare --net" in workflow
        assert b"--ignore=tests/test_job_store_postgres.py" in workflow
        assert b"--ignore=tests/test_mcp_schema_migration_postgres.py" in workflow
        assert (
            b"python -m pytest\n"
            b"          tests/test_job_store_postgres.py\n"
            b"          tests/test_mcp_schema_migration_postgres.py\n"
            b"          -rs"
        ) in workflow
        assert b"resource_names == expected_market_resources" in workflow
        assert b"retired_provider_fields.isdisjoint(body)" in workflow
        assert b"demo_frozen_markets.public_market(market_id)" in workflow
        assert b"demo_frozen_markets.event_association(" in workflow
        assert b"registered_market_ids == expected_market_ids" in workflow
        assert b".dist-info/licenses/THIRD_PARTY_NOTICES.md" in workflow
        assert (
            b"56c45b0e41fdb0d1dd94654aff12fde49a8fc18fc71b168da148c4d84678da2f"
            in workflow
        )
        for resource_name in (
            b"golden_market_structures.json",
            b"synthetic-market-stablecoin-total-cap.json",
            b"synthetic-market-stablecoin-unit-cap.json",
            b"synthetic-market-treasury-ledger-payment.json",
            b"synthetic-market-broker-tokenized-asset.json",
            b"synthetic-market-broker-first-tokenized.json",
            b"synthetic-market-broker-chain-token.json",
            b"synthetic-event-stablecoin-cap.json",
            b"synthetic-event-tokenized-listing.json",
        ):
            assert resource_name in workflow

    assert b"python -m build clients/adk" in shipped_workflow
    assert b"--require-wheel-install" in shipped_workflow
    assert b"fitcheck-public-wheel-smoke" in shipped_workflow


def test_private_account_admission_stays_outside_public_runtime_manifest() -> None:
    if not (ROOT / "public").is_dir():
        pytest.skip("source manifest is verified in the private source checkout")

    manifest = public_export.load_manifest(ROOT)
    exported_files = public_export.collect_export_files(ROOT, manifest)
    sources_by_destination = {
        exported.destination.as_posix(): exported.source_label.as_posix()
        for exported in exported_files
    }
    exported_sources = {
        exported.source_label.as_posix() for exported in exported_files
    }
    exported_destinations = {
        exported.destination.as_posix() for exported in exported_files
    }
    forbidden_private_paths = {
        "el/sourceinterpretation/account_admission.py",
        "el/product/account_sessions.py",
        "migrations/versions/c8e1f4a2b6d9_account_browser_sessions.py",
    }
    assert forbidden_private_paths.isdisjoint(exported_sources)
    assert forbidden_private_paths.isdisjoint(exported_destinations)
    assert sources_by_destination["el/sourceinterpretation/__init__.py"] == (
        "public/el/sourceinterpretation/__init__.py"
    )

    forbidden_concrete_dependencies = (
        "el.budget",
        "BudgetReservation",
        "AccountSourceAdmission",
    )
    for destination in (
        "el/sourceinterpretation/jobs.py",
        "el/product/api.py",
        "el/product/app.py",
    ):
        source = ROOT / sources_by_destination[destination]
        body = source.read_text(encoding="utf-8")
        assert "SourceJobAdmission" in body
        assert not any(
            dependency in body
            for dependency in forbidden_concrete_dependencies
        ), destination


def test_fixture_projection_runs_without_private_account_adapter(tmp_path, monkeypatch):
    """Import and exercise the actual projection, not the private checkout."""
    if not (ROOT / "public").is_dir():
        pytest.skip("source projection is verified in the private source checkout")
    import os
    import subprocess

    monkeypatch.setattr(public_export, "PRIVATE_TMP_ROOT", tmp_path)
    _, files = public_export.prepare_export(ROOT)
    destination = tmp_path / "fixture-projection"
    public_export.build_export(destination, files)
    assert not (destination / "el/product/accounts.py").exists()
    assert not (destination / "el/product/account_sessions.py").exists()
    assert not (
        destination
        / "migrations/versions/c8e1f4a2b6d9_account_browser_sessions.py"
    ).exists()
    assert not (destination / "el/budget").exists()
    assert not (
        destination / "el/sourceinterpretation/account_admission.py"
    ).exists()
    code = r'''import importlib.abc
import pathlib
import socket
import sys

class RejectPrivateAdapter(importlib.abc.MetaPathFinder):
    missing_name = "el.product.accounts"

    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"el.product.accounts", "el.product.account_sessions"}:
            raise ModuleNotFoundError(
                "private account adapter must not load in fixture projection",
                name=(
                    self.missing_name
                    if fullname == "el.product.accounts"
                    else fullname
                ),
            )
        return None
private_adapter_rejector = RejectPrivateAdapter()
sys.meta_path.insert(0, private_adapter_rejector)
original_connect = socket.socket.connect
def offline_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError("projection smoke is offline")
    return original_connect(self, address)
socket.socket.connect = offline_connect

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from el.domain.tables import Base
from el.product.api import ProductApi
from el.product.app import build_app
from el.product.wiring import build_services, local_actor
from el.sourceinterpretation import jobs as projected_source_jobs
import el.product.app as projected_app
assert pathlib.Path(projected_app.__file__).resolve().is_relative_to(pathlib.Path.cwd())
assert not hasattr(projected_source_jobs, "AccountSourceAdmission")
assert "el.sourceinterpretation.account_admission" not in sys.modules
assert not any(name == "el.budget" or name.startswith("el.budget.") for name in sys.modules)
assert "el.product.accounts" not in sys.modules
assert "el.product.account_sessions" not in sys.modules
engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
assert "account_browser_sessions" not in Base.metadata.tables
sessions = sessionmaker(bind=engine, expire_on_commit=False)
services = build_services(mode="fixture", session_factory=sessions)

class UnavailableAccountResolver:
    require_https = True
    async def resolve(self, request):
        raise AssertionError("public account resolver must never run")

private_adapter_rejector.missing_name = "synthetic.transitive_dependency"
try:
    build_app(services, account_resolver=UnavailableAccountResolver())
except ModuleNotFoundError as error:
    assert error.name == "synthetic.transitive_dependency"
else:
    raise AssertionError("transitive import failures must remain visible")

private_adapter_rejector.missing_name = "el.product.accounts"
try:
    build_app(services, account_resolver=UnavailableAccountResolver())
except ValueError as error:
    assert str(error) == "account mode is unavailable in this public distribution"
else:
    raise AssertionError("public account mode must fail closed")

client = TestClient(build_app(ProductApi(services, local_actor(sessions))))
assert client.get("/api/health").status_code == 200
proposal = client.post("/api/v3/normalization-attempts", json={"input_text": "Gemini is going to be ranked #1 chatbot on LMSYS Chatbot Arena by the end of 2026."})
assert proposal.status_code == 200, proposal.text
p = proposal.json()
accepted = client.post("/api/v3/normalization-attempts/" + p["normalization_attempt_id"] + "/accept", json={"expected_input_digest": p["input_digest"]})
assert accepted.status_code == 200, accepted.text
thesis_id = accepted.json()["thesis_analysis_id"]
assert client.get("/api/v3/theses/" + thesis_id + "/accepted-state").status_code == 200
pool = client.post("/api/v3/theses/" + thesis_id + "/market-pool")
assert pool.status_code == 200, pool.text
pool_id = pool.json()["market_display_set_id"]
assert client.get("/api/v3/market-pools/" + pool_id).json() == pool.json()
choice = client.post("/api/v3/market-pools/" + pool_id + "/choice", json={"selection_kind": "none"})
assert choice.status_code == 200, choice.text
assert client.get("/api/v3/market-choices/" + choice.json()["market_choice_id"]).json() == choice.json()
assert "el.product.accounts" not in sys.modules
assert "el.product.account_sessions" not in sys.modules

import pytest
raise SystemExit(pytest.main([
    "-q",
    "-p", "no:cacheprovider",
    "tests/test_anonymous_demo.py",
    "tests/test_product_resume.py",
]))
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(destination)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=destination, env=environment,
        text=True, capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_lockfile_is_exact_and_stale_source_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_fixture_repo(repo)

    _, files = public_export.prepare_export(repo)
    lock = next(item for item in files if item.destination.name == public_export.LOCK_FILENAME)
    _, entries = public_export.parse_lockfile(lock.content)
    assert public_export.LOCK_FILENAME not in entries
    assert set(entries) == {"LICENSE", "README.md", "public-export.toml", "src/app.py"}

    (repo / "src" / "app.py").write_text("VALUE = 2\n")
    with pytest.raises(public_export.BoundaryError, match="stale"):
        public_export.prepare_export(repo)


def test_symlink_and_oversized_sources_fail(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_fixture_repo(repo)
    (repo / "src" / "link.py").symlink_to(repo / "src" / "app.py")
    with pytest.raises(public_export.BoundaryError, match="symlink"):
        public_export.prepare_export(repo)
    (repo / "src" / "link.py").unlink()
    (repo / "src" / "app.py").write_text("x" * 1025)
    with pytest.raises(public_export.BoundaryError, match="oversized"):
        public_export.prepare_export(repo)


def test_explicit_include_and_overlay_parent_symlinks_fail(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.py").write_text("VALUE = 1\n")

    include_repo = tmp_path / "include-repo"
    include_repo.mkdir()
    (include_repo / "public").mkdir()
    (include_repo / "public" / "LICENSE").write_text("license\n")
    (include_repo / "linked").symlink_to(outside, target_is_directory=True)
    (include_repo / "public-export.toml").write_text(
        """manifest_version = 1
[export]
max_file_bytes = 1024
[[include]]
source = "linked/app.py"
destination = "app.py"
[[overlay]]
source = "public/LICENSE"
destination = "LICENSE"
"""
    )
    with pytest.raises(public_export.BoundaryError, match="symlink component"):
        public_export.generate_lockfile(include_repo)

    overlay_repo = tmp_path / "overlay-repo"
    overlay_repo.mkdir()
    (overlay_repo / "app.py").write_text("VALUE = 1\n")
    overlay_source = tmp_path / "overlay-source"
    overlay_source.mkdir()
    (overlay_source / "LICENSE").write_text("license\n")
    (overlay_repo / "public").symlink_to(overlay_source, target_is_directory=True)
    (overlay_repo / "public-export.toml").write_text(
        """manifest_version = 1
[export]
max_file_bytes = 1024
[[include]]
source = "app.py"
destination = "app.py"
[[overlay]]
source = "public/LICENSE"
destination = "LICENSE"
"""
    )
    with pytest.raises(public_export.BoundaryError, match="symlink component"):
        public_export.generate_lockfile(overlay_repo)


def test_nested_git_in_included_directory_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_fixture_repo(repo)
    (repo / "src" / ".git").mkdir()
    with pytest.raises(public_export.BoundaryError, match="nested .git"):
        public_export.prepare_export(repo)
