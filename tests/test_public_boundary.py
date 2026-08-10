from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_public_export as public_export  # noqa: E402
import check_public_boundary as public_boundary  # noqa: E402

from test_public_export import _fixture_png, _write_fixture_repo  # noqa: E402


def _candidate(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_fixture_repo(repo)
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir()
    monkeypatch.setattr(public_export, "PRIVATE_TMP_ROOT", private_tmp)
    monkeypatch.setattr(public_boundary, "validate_destination_path", lambda path: path)
    _, files = public_export.prepare_export(repo)
    destination = private_tmp / "candidate"
    public_export.build_export(destination, files)
    return repo, destination


def test_checker_matches_sources_and_supports_public_self_check(
    tmp_path: Path, monkeypatch
) -> None:
    repo, destination = _candidate(tmp_path, monkeypatch)

    assert public_boundary.check_export(repo, destination) == 5
    assert (
        public_boundary.check_export(
            destination,
            destination,
            self_check=True,
        )
        == 5
    )


def test_checker_accepts_strict_screenshot_in_source_and_candidate_scans(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_fixture_repo(repo)
    asset = repo / "public" / "docs" / "assets" / "fitcheck-fixture-ui.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(_fixture_png())
    with (repo / "public-export.toml").open("a", encoding="utf-8") as manifest:
        manifest.write(
            """
[[overlay]]
source = "public/docs/assets/fitcheck-fixture-ui.png"
destination = "docs/assets/fitcheck-fixture-ui.png"
"""
        )
    _, _, lock_content = public_export.generate_lockfile(repo)
    (repo / public_export.LOCK_FILENAME).write_bytes(lock_content)

    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir()
    monkeypatch.setattr(public_export, "PRIVATE_TMP_ROOT", private_tmp)
    monkeypatch.setattr(public_boundary, "validate_destination_path", lambda path: path)
    _, files = public_export.prepare_export(repo)
    destination = private_tmp / "candidate"
    public_export.build_export(destination, files)

    assert public_boundary.check_export(repo, destination) == 6
    assert public_boundary.check_export(destination, destination, self_check=True) == 6


def test_checker_rejects_unmanifested_and_changed_files(tmp_path: Path, monkeypatch) -> None:
    repo, destination = _candidate(tmp_path, monkeypatch)
    (destination / "extra.txt").write_text("not approved\n")
    with pytest.raises(public_export.BoundaryError, match="unexpected"):
        public_boundary.check_export(repo, destination)
    (destination / "extra.txt").unlink()
    (destination / "src" / "app.py").write_text("VALUE = 2\n")
    with pytest.raises(public_export.BoundaryError, match="content differs"):
        public_boundary.check_export(repo, destination)


def test_self_check_uses_lock_not_checkout_manifest(tmp_path: Path, monkeypatch) -> None:
    _, destination = _candidate(tmp_path, monkeypatch)
    with (destination / "public-export.toml").open("a", encoding="utf-8") as handle:
        handle.write(
            "\n[[include]]\nsource = \"rogue.py\"\ndestination = \"rogue.py\"\n"
        )
    (destination / "rogue.py").write_text("ROGUE = True\n")
    with pytest.raises(public_export.BoundaryError, match="unexpected|immutable lock"):
        public_boundary.check_export(
            destination,
            destination,
            self_check=True,
        )


def test_self_check_requires_and_enforces_immutable_lock(tmp_path: Path, monkeypatch) -> None:
    _, destination = _candidate(tmp_path, monkeypatch)
    lock_path = destination / public_export.LOCK_FILENAME
    raw = json.loads(lock_path.read_text())
    for row in raw["files"]:
        if row["path"] == "src/app.py":
            row["sha256"] = "0" * 64
    lock_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    with pytest.raises(public_export.BoundaryError, match="immutable lock"):
        public_boundary.check_export(destination, destination, self_check=True)

    lock_path.unlink()
    with pytest.raises(public_export.BoundaryError, match="PUBLIC_EXPORT_LOCK"):
        public_boundary.check_export(destination, destination, self_check=True)


def test_self_check_ignores_only_real_root_git(tmp_path: Path, monkeypatch) -> None:
    _, destination = _candidate(tmp_path, monkeypatch)
    root_git = destination / ".git"
    root_git.mkdir()
    (root_git / "objects").mkdir()
    (root_git / "objects" / "private").write_text(
        "/" + "Users/private/history\n"
    )
    assert (
        public_boundary.check_export(destination, destination, self_check=True)
        == 5
    )

    nested = destination / "src" / ".git"
    nested.mkdir()
    with pytest.raises(public_export.BoundaryError, match="nested .git"):
        public_boundary.check_export(destination, destination, self_check=True)


@pytest.mark.parametrize(
    ("relative", "is_directory"),
    [
        ("src/__pycache__", True),
        ("src/app.pyc", False),
        ("src/state.db", False),
        ("src/.env.production", False),
    ],
)
def test_self_check_rejects_runtime_artifacts(
    tmp_path: Path,
    monkeypatch,
    relative: str,
    is_directory: bool,
) -> None:
    _, destination = _candidate(tmp_path, monkeypatch)
    artifact = destination / relative
    if is_directory:
        artifact.mkdir(parents=True)
    else:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("runtime\n")
    with pytest.raises(public_export.BoundaryError, match="runtime|denied"):
        public_boundary.check_export(destination, destination, self_check=True)


def test_checker_rejects_output_symlink(tmp_path: Path, monkeypatch) -> None:
    repo, destination = _candidate(tmp_path, monkeypatch)
    (destination / "src" / "app.py").unlink()
    (destination / "src" / "app.py").symlink_to(destination / "README.md")
    with pytest.raises(public_export.BoundaryError, match="symlink"):
        public_boundary.check_export(repo, destination)


def test_candidate_native_self_check_does_not_create_bytecode_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "public").mkdir()
    for name in ("build_public_export.py", "check_public_boundary.py"):
        shutil.copyfile(ROOT / "scripts" / name, repo / "scripts" / name)
    (repo / "public" / "README.md").write_text("# Public\n")
    (repo / "public" / "LICENSE").write_text("license\n")
    (repo / "public-export.toml").write_text(
        """manifest_version = 1
[export]
max_file_bytes = 262144
[[include]]
source = "public-export.toml"
destination = "public-export.toml"
[[include]]
source = "scripts"
destination = "scripts"
extensions = [".py"]
[[overlay]]
source = "public/README.md"
destination = "README.md"
[[overlay]]
source = "public/LICENSE"
destination = "LICENSE"
"""
    )
    _, _, lock_content = public_export.generate_lockfile(repo)
    (repo / public_export.LOCK_FILENAME).write_bytes(lock_content)

    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir()
    monkeypatch.setattr(public_export, "PRIVATE_TMP_ROOT", private_tmp)
    _, files = public_export.prepare_export(repo)
    destination = private_tmp / "candidate"
    public_export.build_export(destination, files)

    environment = os.environ.copy()
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/check_public_boundary.py",
            "--repo-root",
            str(destination),
            "--destination",
            ".",
            "--self-check",
        ],
        cwd=destination,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "files match the public boundary" in completed.stdout
    assert not list(destination.rglob("__pycache__"))
    assert not list(destination.rglob("*.pyc"))
