from __future__ import annotations

import hashlib
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
            for name in ("LICENSE", "pyproject.toml", "Dockerfile", "DATA_CARD.md")
        }

    assert "LICENSE_REQUIRED.md" not in by_destination
    assert hashlib.sha256(by_destination["LICENSE"]).hexdigest() == (
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
    )
    assert b'license = "Apache-2.0"' in by_destination["pyproject.toml"]
    assert b"COPY pyproject.toml README.md LICENSE ./" in by_destination["Dockerfile"]
    assert b"project-authored synthetic material" in by_destination["DATA_CARD.md"]


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
