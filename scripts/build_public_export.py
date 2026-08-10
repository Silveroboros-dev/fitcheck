#!/usr/bin/env python3
"""Build a deterministic, manifest-only candidate for FitCheck publication.

The builder is deliberately local and fail-closed.  It never consults Git,
never walks the repository root, and only reads sources named by
``public-export.toml``.  Publication remains a separate, human-authorized
operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import tomllib
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Literal, Sequence
from urllib.parse import urlsplit


MANIFEST_VERSION = 1
LOCK_VERSION = 1
LOCK_FILENAME = "PUBLIC_EXPORT_LOCK.json"
HARD_MAX_FILE_BYTES = 5 * 1024 * 1024
PRIVATE_TMP_ROOT = Path("/private/tmp")

# These paths are outside the public-source boundary even if a future manifest
# accidentally names them.  Prefix checks happen before any file is opened.
DENIED_PREFIXES = (
    PurePosixPath(".agents"),
    PurePosixPath(".claude"),
    PurePosixPath(".git"),
    PurePosixPath(".obsidian"),
    PurePosixPath("data"),
    PurePosixPath("docs/archive"),
    PurePosixPath("evals/governance"),
    PurePosixPath("outputs"),
    PurePosixPath("output"),
    PurePosixPath("review_batches"),
    PurePosixPath("vendor"),
)
IGNORED_RUNTIME_DIRECTORIES = frozenset(
    {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
IGNORED_RUNTIME_SUFFIXES = frozenset({".pyc", ".pyo"})
SECRET_BASENAMES = frozenset(
    {
        ".env",
        ".fitcheck-demo-key",
        ".netrc",
        "application_default_credentials.json",
        "credentials.json",
        "service-account.json",
    }
)
SECRET_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
DATABASE_SUFFIXES = frozenset({".db", ".duckdb", ".sqlite", ".sqlite3"})
RUNTIME_FILENAMES = frozenset({".coverage", ".DS_Store"})

_LOCAL_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])/(?:" + "Users|home" + r")/[^/\s]+/"),
    re.compile(r"(?<![A-Za-z0-9_])/[r]oot(?:/|\b)"),
    re.compile(r"(?i)\b[A-Z]:\\User" + r"s\\[^\\\s]+\\"),
    re.compile(r"(?i)\b[A-Z]:/User" + r"s/[^/\s]+/"),
    re.compile(r"/private/var/folders/[A-Za-z0-9_/.-]+"),
)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})"
)
_RUN_APP_URL = re.compile(
    r"(?i)https?://[^\s\"'<>]*\.run\.app(?:[/?#][^\s\"'<>]*)?"
)
_X_STATUS_URL = re.compile(
    r"(?i)https?://(?:www\.)?(?:x|twitter)\.com/[^\s\"'<>]+/status/\d+"
)
_GCP_RESOURCE = re.compile(r"\bprojects/([a-z][a-z0-9-]{4,28}[a-z0-9]|\d{6,})\b")
_GCP_PROJECT_ASSIGNMENT = re.compile(
    r"(?i)\b(?:gcp[_ -]?project(?:[_ -]?id)?|google[_ -]?cloud[_ -]?project|"
    r"project[_ -]?id|project[_ -]?number)\b\s*(?:=|:)\s*[\"']?"
    r"([a-z][a-z0-9-]{4,28}[a-z0-9]|\d{6,})"
)
_GCLOUD_PROJECT_FLAG = re.compile(
    r"(?i)--project(?:=|\s+)([a-z][a-z0-9-]{4,28}[a-z0-9]|\d{6,})"
)
_SERVICE_ACCOUNT_EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]+@([a-z][a-z0-9-]{4,28}[a-z0-9])\.iam\.gserviceaccount\.com"
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bghp_[0-9A-Za-z]{30,}\b"),
    re.compile(r"\bgithub_pat_[0-9A-Za-z_]{40,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{16}\b"),
)
_PLACEHOLDER_IDENTIFIERS = frozenset(
    {
        "different-project",
        "dummy-project",
        "example-project",
        "fake-project",
        "fitcheck-spike-project",
        "fitcheck-test",
        "placeholder-project",
        "project-test",
        "sample-project",
    }
)
_PLACEHOLDER_RUN_APP_HOSTS = frozenset(
    {
        "fitcheck-abc-uc.a.run.app",
        "other-abc-uc.a.run.app",
    }
)
_RESERVED_EMAIL_SUFFIXES = (
    ".example",
    ".invalid",
    ".local",
    ".localhost",
    ".test",
    "example.com",
    "example.net",
    "example.org",
)

# Binary publication is denied by default.  These are the only two labels a
# single reviewed screenshot has while it crosses the export boundary: its
# canonical overlay source and its candidate destination.
_PUBLIC_PNG_LABELS = frozenset(
    {
        "public/docs/assets/fitcheck-fixture-ui.png",
        "docs/assets/fitcheck-fixture-ui.png",
    }
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_ALLOWED_CHUNKS = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND"})
_PNG_MIN_WIDTH = 640
_PNG_MIN_HEIGHT = 360
_PNG_MAX_WIDTH = 1920
_PNG_MAX_HEIGHT = 1200


class BoundaryError(RuntimeError):
    """The requested export violates the public-repository boundary."""


@dataclass(frozen=True)
class IncludeRule:
    source: PurePosixPath
    destination: PurePosixPath
    extensions: tuple[str, ...] = ()
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class OverlayRule:
    source: PurePosixPath
    destination: PurePosixPath


@dataclass(frozen=True)
class ExportManifest:
    max_file_bytes: int
    includes: tuple[IncludeRule, ...]
    overlays: tuple[OverlayRule, ...]


@dataclass(frozen=True)
class ExportFile:
    source: Path
    source_label: PurePosixPath
    destination: PurePosixPath
    content: bytes
    sha256: str
    kind: Literal["include", "overlay", "lock"]


def _table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BoundaryError(f"{label} must be a TOML table")
    return value


def _array_of_tables(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise BoundaryError(f"{label} must be an array of TOML tables")
    return value


def _relative_path(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise BoundaryError(f"{label} must be a non-empty relative POSIX path")
    if "\\" in value:
        raise BoundaryError(f"{label} must use POSIX separators: {value!r}")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or value != candidate.as_posix():
        raise BoundaryError(f"{label} is not a normalized relative path: {value!r}")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise BoundaryError(f"{label} contains a forbidden path component: {value!r}")
    return candidate


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise BoundaryError(f"{label} must be an array of strings")
    if len(value) != len(set(value)):
        raise BoundaryError(f"{label} contains duplicate values")
    return tuple(value)


def load_manifest(repo_root: Path, manifest_path: Path | None = None) -> ExportManifest:
    repo_root = repo_root.resolve()
    raw_candidate = manifest_path or repo_root / "public-export.toml"
    candidate = Path(os.path.abspath(raw_candidate))
    expected = repo_root / "public-export.toml"
    if candidate != expected:
        raise BoundaryError("manifest must be <repo-root>/public-export.toml")
    manifest_label = PurePosixPath("public-export.toml")
    _assert_no_symlink_components(repo_root, manifest_label, label="public-export.toml")
    content = _read_rooted_regular_file(
        repo_root,
        manifest_label,
        HARD_MAX_FILE_BYTES,
        "public-export.toml",
    )
    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise BoundaryError(f"invalid UTF-8 TOML manifest: {error}") from error
    if set(raw) != {"manifest_version", "export", "include", "overlay"}:
        unknown = sorted(set(raw) - {"manifest_version", "export", "include", "overlay"})
        missing = sorted({"manifest_version", "export", "include", "overlay"} - set(raw))
        raise BoundaryError(
            f"manifest top-level keys differ from the contract; unknown={unknown}, missing={missing}"
        )
    if raw["manifest_version"] != MANIFEST_VERSION:
        raise BoundaryError(
            f"manifest_version must be {MANIFEST_VERSION}, got {raw['manifest_version']!r}"
        )
    export = _table(raw["export"], "export")
    if set(export) != {"max_file_bytes"}:
        raise BoundaryError("export must contain only max_file_bytes")
    max_file_bytes = export["max_file_bytes"]
    if (
        not isinstance(max_file_bytes, int)
        or isinstance(max_file_bytes, bool)
        or max_file_bytes <= 0
        or max_file_bytes > HARD_MAX_FILE_BYTES
    ):
        raise BoundaryError(
            f"max_file_bytes must be an integer from 1 to {HARD_MAX_FILE_BYTES}"
        )

    includes: list[IncludeRule] = []
    for index, row in enumerate(_array_of_tables(raw["include"], "include")):
        allowed = {"source", "destination", "extensions", "names"}
        if not {"source", "destination"} <= set(row) or not set(row) <= allowed:
            raise BoundaryError(f"include[{index}] has missing or unknown keys")
        extensions = _string_tuple(row.get("extensions"), f"include[{index}].extensions")
        if any(not item.startswith(".") or "/" in item for item in extensions):
            raise BoundaryError(f"include[{index}].extensions contains an invalid suffix")
        names = _string_tuple(row.get("names"), f"include[{index}].names")
        if any("/" in item or item in {"", ".", ".."} for item in names):
            raise BoundaryError(f"include[{index}].names contains an invalid basename")
        includes.append(
            IncludeRule(
                source=_relative_path(row["source"], f"include[{index}].source"),
                destination=_relative_path(
                    row["destination"], f"include[{index}].destination"
                ),
                extensions=extensions,
                names=names,
            )
        )

    overlays: list[OverlayRule] = []
    for index, row in enumerate(_array_of_tables(raw["overlay"], "overlay")):
        if set(row) != {"source", "destination"}:
            raise BoundaryError(f"overlay[{index}] must contain source and destination")
        source = _relative_path(row["source"], f"overlay[{index}].source")
        if not source.parts or source.parts[0] != "public" or len(source.parts) < 2:
            raise BoundaryError(f"overlay[{index}].source must be a file below public/")
        overlays.append(
            OverlayRule(
                source=source,
                destination=_relative_path(
                    row["destination"], f"overlay[{index}].destination"
                ),
            )
        )

    if not includes:
        raise BoundaryError("manifest must contain at least one include rule")
    if not overlays:
        raise BoundaryError("manifest must contain at least one explicit public/ overlay")
    return ExportManifest(
        max_file_bytes=max_file_bytes,
        includes=tuple(includes),
        overlays=tuple(overlays),
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_destination_path(destination: Path) -> Path:
    if not destination.is_absolute():
        raise BoundaryError("destination must be an absolute path below /private/tmp")
    root = PRIVATE_TMP_ROOT.resolve()
    resolved = destination.resolve(strict=False)
    if resolved == root or not _is_relative_to(resolved, root):
        raise BoundaryError("destination must be a child of /private/tmp")
    return resolved


def _matches_prefix(path: PurePosixPath, prefix: PurePosixPath) -> bool:
    return path == prefix or prefix in path.parents


def _assert_source_path_allowed(path: PurePosixPath, *, overlay: bool = False) -> None:
    if any(_matches_prefix(path, prefix) for prefix in DENIED_PREFIXES):
        raise BoundaryError(f"denied source path: {path}")
    if not overlay and path.parts and path.parts[0] == "public":
        raise BoundaryError(f"public/ sources require an explicit overlay mapping: {path}")
    if ".git" in path.parts:
        raise BoundaryError(f"nested .git is forbidden in public sources: {path}")
    if any(part in IGNORED_RUNTIME_DIRECTORIES for part in path.parts):
        raise BoundaryError(f"runtime artifact is not a public source: {path}")
    basename = path.name.lower()
    if (
        basename in SECRET_BASENAMES
        or basename.startswith(".env.")
        or basename in RUNTIME_FILENAMES
        or any(
            basename.endswith(suffix)
            for suffix in SECRET_SUFFIXES | DATABASE_SUFFIXES | IGNORED_RUNTIME_SUFFIXES
        )
    ):
        raise BoundaryError(f"secret or runtime source path is denied: {path}")


def _assert_destination_path_allowed(path: PurePosixPath) -> None:
    if any(_matches_prefix(path, prefix) for prefix in DENIED_PREFIXES):
        raise BoundaryError(f"denied export destination: {path}")
    if path.parts and path.parts[0] == "public":
        raise BoundaryError(f"public/ is an overlay source only, not an output path: {path}")
    if ".git" in path.parts:
        raise BoundaryError(f"nested .git is forbidden in public output: {path}")
    if any(part in IGNORED_RUNTIME_DIRECTORIES for part in path.parts):
        raise BoundaryError(f"runtime cache is forbidden in public output: {path}")
    basename = path.name.lower()
    if (
        basename in SECRET_BASENAMES
        or basename.startswith(".env.")
        or basename in RUNTIME_FILENAMES
        or any(
            basename.endswith(suffix)
            for suffix in SECRET_SUFFIXES | DATABASE_SUFFIXES | IGNORED_RUNTIME_SUFFIXES
        )
    ):
        raise BoundaryError(f"secret or runtime export destination is denied: {path}")


def _assert_no_symlink_components(
    root: Path, relative: PurePosixPath, *, label: str
) -> None:
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise BoundaryError(f"cannot inspect {label}: {error}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise BoundaryError(f"symlink component is forbidden in {label}: {current}")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise BoundaryError(f"non-directory parent component in {label}: {current}")


def _read_descriptor(descriptor: int, max_bytes: int, label: str) -> bytes:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise BoundaryError(f"source is not a regular file: {label}")
    if metadata.st_size > max_bytes:
        raise BoundaryError(
            f"oversized file {label}: {metadata.st_size} bytes exceeds {max_bytes}"
        )
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise BoundaryError(f"oversized file {label}: exceeds {max_bytes} bytes")
    return b"".join(chunks)


def _read_rooted_regular_file(
    root: Path,
    relative: PurePosixPath,
    max_bytes: int,
    label: str,
) -> bytes:
    """Read a file through no-follow directory descriptors rooted at ``root``."""

    if not relative.parts:
        raise BoundaryError(f"empty rooted file path: {label}")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(root, directory_flags | nofollow)
    except OSError as error:
        raise BoundaryError(f"cannot safely open root for {label}: {error}") from error
    try:
        for part in relative.parts[:-1]:
            try:
                next_fd = os.open(
                    part,
                    directory_flags | nofollow,
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise BoundaryError(
                    f"cannot safely traverse parent of {label}: {error}"
                ) from error
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            descriptor = os.open(
                relative.parts[-1],
                os.O_RDONLY | nofollow,
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise BoundaryError(f"cannot safely open {label}: {error}") from error
        try:
            return _read_descriptor(descriptor, max_bytes, label)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def _is_placeholder_identifier(value: str) -> bool:
    conventional = value.strip("<>{}[]$% \"'").upper()
    if conventional in {
        "PROJECT",
        "PROJECT_ID",
        "PROJECT_NUMBER",
        "REGION",
        "SERVICE",
        "SERVICE_URL",
    }:
        return True
    return value.lower() in _PLACEHOLDER_IDENTIFIERS


def _is_placeholder_run_app_url(value: str) -> bool:
    try:
        hostname = urlsplit(value).hostname
    except ValueError:
        return False
    return bool(hostname and hostname.lower() in _PLACEHOLDER_RUN_APP_HOSTS)


def _validate_public_png(content: bytes, label: str) -> None:
    """Validate the one deliberately permitted binary publication artifact."""

    if not content.startswith(_PNG_SIGNATURE):
        raise BoundaryError(f"invalid PNG signature in {label}")

    offset = len(_PNG_SIGNATURE)
    chunk_index = 0
    seen_ihdr = False
    seen_plte = False
    seen_idat = False
    seen_iend = False
    width = 0
    height = 0
    color_type = -1
    channels = 0
    compressed_rows: list[bytes] = []

    while offset < len(content):
        if len(content) - offset < 12:
            raise BoundaryError(f"truncated PNG chunk framing in {label}")
        length = struct.unpack_from(">I", content, offset)[0]
        chunk_type = content[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        chunk_end = data_end + 4
        if chunk_end > len(content):
            raise BoundaryError(f"truncated PNG chunk data in {label}")
        chunk_data = content[data_start:data_end]
        expected_crc = struct.unpack_from(">I", content, data_end)[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise BoundaryError(f"PNG chunk CRC mismatch in {label}")
        offset = chunk_end

        if chunk_type not in _PNG_ALLOWED_CHUNKS:
            raise BoundaryError(
                f"forbidden PNG chunk {chunk_type!r} in {label}; metadata is not allowed"
            )

        if chunk_type == b"IHDR":
            if chunk_index != 0 or seen_ihdr:
                raise BoundaryError(
                    f"PNG IHDR must be first and appear once in {label}"
                )
            if length != 13:
                raise BoundaryError(f"PNG IHDR has invalid length in {label}")
            (
                width,
                height,
                bit_depth,
                color_type,
                compression_method,
                filter_method,
                interlace_method,
            ) = struct.unpack(">IIBBBBB", chunk_data)
            if not (
                _PNG_MIN_WIDTH <= width <= _PNG_MAX_WIDTH
                and _PNG_MIN_HEIGHT <= height <= _PNG_MAX_HEIGHT
            ):
                raise BoundaryError(
                    f"PNG dimensions outside the public screenshot bounds in {label}: "
                    f"{width}x{height}"
                )
            if bit_depth != 8 or color_type not in {2, 3, 6}:
                raise BoundaryError(
                    f"PNG must be 8-bit RGB, indexed color, or RGBA in {label}"
                )
            if (
                compression_method != 0
                or filter_method != 0
                or interlace_method != 0
            ):
                raise BoundaryError(
                    "PNG must use standard compression/filtering and be "
                    f"non-interlaced in {label}"
                )
            channels = {2: 3, 3: 1, 6: 4}[color_type]
            seen_ihdr = True
        elif chunk_type == b"PLTE":
            if not seen_ihdr or seen_plte or seen_idat:
                raise BoundaryError(
                    f"PNG PLTE must appear at most once before IDAT in {label}"
                )
            if length == 0 or length % 3 != 0 or length > 3 * 256:
                raise BoundaryError(
                    f"PNG PLTE has an invalid RGB entry count in {label}"
                )
            seen_plte = True
        elif chunk_type == b"IDAT":
            if not seen_ihdr or seen_iend:
                raise BoundaryError(f"PNG IDAT is out of order in {label}")
            seen_idat = True
            compressed_rows.append(chunk_data)
        else:  # IEND
            if not seen_ihdr or not seen_idat or seen_iend or length != 0:
                raise BoundaryError(f"PNG IEND is invalid or out of order in {label}")
            seen_iend = True
            if offset != len(content):
                raise BoundaryError(f"trailing data after PNG IEND in {label}")
        chunk_index += 1

    if not seen_ihdr or not seen_idat or not seen_iend:
        raise BoundaryError(f"PNG is missing required IHDR, IDAT, or IEND in {label}")
    if color_type == 3 and not seen_plte:
        raise BoundaryError(f"indexed PNG is missing its required PLTE in {label}")

    # CRCs validate chunk integrity, while bounded decompression verifies that
    # IDAT actually contains exactly one non-interlaced image of the declared
    # dimensions instead of an opaque payload with PNG-shaped framing.
    row_bytes = width * channels
    expected_size = height * (row_bytes + 1)
    decompressor = zlib.decompressobj()
    try:
        rows = decompressor.decompress(b"".join(compressed_rows), expected_size + 1)
    except zlib.error as error:
        raise BoundaryError(
            f"invalid PNG IDAT compression in {label}: {error}"
        ) from error
    if (
        len(rows) != expected_size
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise BoundaryError(f"PNG IDAT size or stream framing is invalid in {label}")
    if any(rows[index * (row_bytes + 1)] > 4 for index in range(height)):
        raise BoundaryError(f"PNG contains an invalid scanline filter in {label}")


def _scan_text(content: bytes, label: str) -> None:
    if label in _PUBLIC_PNG_LABELS:
        _validate_public_png(content, label)
        return
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BoundaryError(f"non-UTF-8 public source {label}: {error}") from error

    for pattern in _LOCAL_PATH_PATTERNS:
        match = pattern.search(text)
        if match:
            raise BoundaryError(f"absolute local path in {label}: {match.group(0)!r}")

    match = _X_STATUS_URL.search(text)
    if match:
        raise BoundaryError(f"X status URL in {label}: {match.group(0)!r}")

    for match in _RUN_APP_URL.finditer(text):
        if not _is_placeholder_run_app_url(match.group(0)):
            raise BoundaryError(f"live Cloud Run URL in {label}: {match.group(0)!r}")

    for pattern in (_GCP_RESOURCE, _GCP_PROJECT_ASSIGNMENT, _GCLOUD_PROJECT_FLAG):
        for match in pattern.finditer(text):
            value = match.group(1)
            if not _is_placeholder_identifier(value):
                raise BoundaryError(f"live GCP identifier in {label}: {value!r}")

    for match in _SERVICE_ACCOUNT_EMAIL.finditer(text):
        if not _is_placeholder_identifier(match.group(1)):
            raise BoundaryError(
                f"live service-account identifier in {label}: {match.group(0)!r}"
            )

    for match in _EMAIL.finditer(text):
        domain = match.group(1).lower()
        if domain.endswith(_RESERVED_EMAIL_SUFFIXES):
            continue
        value = match.group(0)
        if domain.endswith(".run.app") and _is_placeholder_run_app_url(
            "https://" + domain
        ):
            continue
        raise BoundaryError(f"personal email in {label}: {value!r}")

    for pattern in _SECRET_PATTERNS:
        if match := pattern.search(text):
            raise BoundaryError(f"secret-like content in {label}: {match.group(0)[:24]!r}")


def _iter_directory_files(
    root: Path,
    source_label: PurePosixPath,
    *,
    extensions: Sequence[str],
    names: Sequence[str],
) -> Iterator[tuple[Path, PurePosixPath]]:
    if not extensions and not names:
        raise BoundaryError(
            f"directory include {source_label} must declare extensions and/or names"
        )

    def visit(directory: Path, relative: PurePosixPath) -> Iterator[tuple[Path, PurePosixPath]]:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise BoundaryError(f"cannot enumerate {source_label / relative}: {error}") from error
        for entry in entries:
            child_relative = relative / entry.name
            child_label = source_label / child_relative
            if entry.name == ".git":
                raise BoundaryError(f"nested .git is forbidden in public sources: {child_label}")
            if entry.is_symlink():
                raise BoundaryError(f"symlink is forbidden in public sources: {child_label}")
            if entry.is_dir(follow_symlinks=False):
                if entry.name in IGNORED_RUNTIME_DIRECTORIES:
                    continue
                _assert_source_path_allowed(child_label)
                yield from visit(Path(entry.path), child_relative)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise BoundaryError(f"non-regular public source: {child_label}")
            _assert_source_path_allowed(child_label)
            if Path(entry.name).suffix in IGNORED_RUNTIME_SUFFIXES:
                continue
            if entry.name not in names and Path(entry.name).suffix not in extensions:
                raise BoundaryError(
                    f"source file below {source_label} is outside its manifest rule: "
                    f"{child_label}"
                )
            yield Path(entry.path), child_relative

    yield from visit(root, PurePosixPath())


def _export_file(
    repo_root: Path,
    source_label: PurePosixPath,
    destination: PurePosixPath,
    *,
    max_file_bytes: int,
    kind: Literal["include", "overlay"],
) -> ExportFile:
    _assert_no_symlink_components(
        repo_root,
        source_label,
        label=source_label.as_posix(),
    )
    content = _read_rooted_regular_file(
        repo_root,
        source_label,
        max_file_bytes,
        source_label.as_posix(),
    )
    _scan_text(content, source_label.as_posix())
    return ExportFile(
        source=repo_root.joinpath(*source_label.parts),
        source_label=source_label,
        destination=destination,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        kind=kind,
    )


def collect_export_files(repo_root: Path, manifest: ExportManifest) -> tuple[ExportFile, ...]:
    repo_root = repo_root.resolve()
    files: list[ExportFile] = []
    for rule in manifest.includes:
        _assert_source_path_allowed(rule.source)
        _assert_destination_path_allowed(rule.destination)
        _assert_no_symlink_components(
            repo_root,
            rule.source,
            label=rule.source.as_posix(),
        )
        source = repo_root.joinpath(*rule.source.parts)
        try:
            source_metadata = source.lstat()
        except OSError as error:
            raise BoundaryError(f"missing include source {rule.source}: {error}") from error
        if stat.S_ISLNK(source_metadata.st_mode):
            raise BoundaryError(f"symlink is forbidden in public sources: {rule.source}")
        if stat.S_ISREG(source_metadata.st_mode):
            if rule.extensions or rule.names:
                raise BoundaryError(
                    f"file include {rule.source} must not declare extensions or names"
                )
            files.append(
                _export_file(
                    repo_root,
                    rule.source,
                    rule.destination,
                    max_file_bytes=manifest.max_file_bytes,
                    kind="include",
                )
            )
            continue
        if not stat.S_ISDIR(source_metadata.st_mode):
            raise BoundaryError(f"include source is not a file or directory: {rule.source}")
        for child, child_relative in _iter_directory_files(
            source,
            rule.source,
            extensions=rule.extensions,
            names=rule.names,
        ):
            destination = rule.destination / child_relative
            _assert_destination_path_allowed(destination)
            files.append(
                _export_file(
                    repo_root,
                    rule.source / child_relative,
                    destination,
                    max_file_bytes=manifest.max_file_bytes,
                    kind="include",
                )
            )

    for rule in manifest.overlays:
        _assert_source_path_allowed(rule.source, overlay=True)
        _assert_destination_path_allowed(rule.destination)
        _assert_no_symlink_components(
            repo_root,
            rule.source,
            label=rule.source.as_posix(),
        )
        source = repo_root.joinpath(*rule.source.parts)
        try:
            source_metadata = source.lstat()
        except OSError as error:
            raise BoundaryError(f"missing overlay source {rule.source}: {error}") from error
        if stat.S_ISLNK(source_metadata.st_mode):
            raise BoundaryError(f"symlink is forbidden in public overlays: {rule.source}")
        if not stat.S_ISREG(source_metadata.st_mode):
            raise BoundaryError(f"overlay source is not a regular file: {rule.source}")
        files.append(
            _export_file(
                repo_root,
                rule.source,
                rule.destination,
                max_file_bytes=manifest.max_file_bytes,
                kind="overlay",
            )
        )

    by_destination: dict[str, ExportFile] = {}
    casefolded: dict[str, str] = {}
    for item in files:
        destination = item.destination.as_posix()
        if destination in by_destination:
            previous = by_destination[destination]
            raise BoundaryError(
                f"duplicate export destination {destination}: "
                f"{previous.source_label} and {item.source_label}"
            )
        folded = destination.casefold()
        if folded in casefolded:
            raise BoundaryError(
                f"case-insensitive destination collision: {casefolded[folded]} and {destination}"
            )
        by_destination[destination] = item
        casefolded[folded] = destination
    return tuple(by_destination[name] for name in sorted(by_destination))


def enforce_license_gate(
    destinations: Iterable[PurePosixPath], *, allow_license_blocker: bool
) -> None:
    names = {path.as_posix() for path in destinations}
    blockers = {name for name in names if PurePosixPath(name).name == "LICENSE_REQUIRED.md"}
    licenses = {
        name
        for name in names
        if PurePosixPath(name).parent == PurePosixPath(".")
        and PurePosixPath(name).name.lower() in {"license", "license.md", "license.txt"}
    }
    if blockers and not allow_license_blocker:
        raise BoundaryError(
            "LICENSE_REQUIRED.md is a deliberate publication blocker; choose and supply a real LICENSE"
        )
    if not licenses and not allow_license_blocker:
        raise BoundaryError("public export has no root LICENSE")


def render_lockfile(manifest: ExportManifest, files: Sequence[ExportFile]) -> bytes:
    entries = []
    for item in sorted(files, key=lambda row: row.destination.as_posix()):
        destination = item.destination.as_posix()
        if destination == LOCK_FILENAME:
            raise BoundaryError(f"{LOCK_FILENAME} must not contain its own digest")
        entries.append({"path": destination, "sha256": item.sha256})
    payload = {
        "files": entries,
        "lock_version": LOCK_VERSION,
        "max_file_bytes": manifest.max_file_bytes,
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def parse_lockfile(content: bytes, *, label: str = LOCK_FILENAME) -> tuple[int, dict[str, str]]:
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BoundaryError(f"invalid UTF-8 JSON lockfile {label}: {error}") from error
    if not isinstance(raw, dict) or set(raw) != {
        "files",
        "lock_version",
        "max_file_bytes",
    }:
        raise BoundaryError(f"{label} has unknown or missing top-level keys")
    if raw["lock_version"] != LOCK_VERSION:
        raise BoundaryError(
            f"{label} lock_version must be {LOCK_VERSION}, got {raw['lock_version']!r}"
        )
    max_file_bytes = raw["max_file_bytes"]
    if (
        not isinstance(max_file_bytes, int)
        or isinstance(max_file_bytes, bool)
        or max_file_bytes <= 0
        or max_file_bytes > HARD_MAX_FILE_BYTES
    ):
        raise BoundaryError(f"{label} has invalid max_file_bytes")
    rows = raw["files"]
    if not isinstance(rows, list):
        raise BoundaryError(f"{label}.files must be an array")
    values: dict[str, str] = {}
    folded: dict[str, str] = {}
    previous: str | None = None
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise BoundaryError(f"{label}.files[{index}] must contain path and sha256")
        destination = _relative_path(row["path"], f"{label}.files[{index}].path")
        _assert_destination_path_allowed(destination)
        name = destination.as_posix()
        if name == LOCK_FILENAME:
            raise BoundaryError(f"{label} must exclude only its own digest entry")
        digest = row["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise BoundaryError(f"{label}.files[{index}].sha256 is invalid")
        if previous is not None and name <= previous:
            raise BoundaryError(f"{label}.files must be strictly path-sorted")
        folded_name = name.casefold()
        if folded_name in folded:
            raise BoundaryError(
                f"case-insensitive lockfile collision: {folded[folded_name]} and {name}"
            )
        values[name] = digest
        folded[folded_name] = name
        previous = name
    return max_file_bytes, values


def generate_lockfile(
    repo_root: Path,
    *,
    manifest_path: Path | None = None,
    allow_license_blocker: bool = False,
) -> tuple[ExportManifest, tuple[ExportFile, ...], bytes]:
    manifest = load_manifest(repo_root, manifest_path)
    files = collect_export_files(repo_root, manifest)
    if any(item.destination.as_posix() == LOCK_FILENAME for item in files):
        raise BoundaryError(
            f"{LOCK_FILENAME} is generated and must not be an include or overlay destination"
        )
    enforce_license_gate(
        (item.destination for item in files),
        allow_license_blocker=allow_license_blocker,
    )
    return manifest, files, render_lockfile(manifest, files)


def prepare_export(
    repo_root: Path,
    *,
    manifest_path: Path | None = None,
    allow_license_blocker: bool = False,
) -> tuple[ExportManifest, tuple[ExportFile, ...]]:
    repo_root = repo_root.resolve()
    manifest, files, expected_lock = generate_lockfile(
        repo_root,
        manifest_path=manifest_path,
        allow_license_blocker=allow_license_blocker,
    )
    lock_label = PurePosixPath(LOCK_FILENAME)
    _assert_no_symlink_components(repo_root, lock_label, label=LOCK_FILENAME)
    actual_lock = _read_rooted_regular_file(
        repo_root,
        lock_label,
        HARD_MAX_FILE_BYTES,
        LOCK_FILENAME,
    )
    _scan_text(actual_lock, LOCK_FILENAME)
    parse_lockfile(actual_lock)
    if actual_lock != expected_lock:
        raise BoundaryError(
            f"{LOCK_FILENAME} is stale; regenerate it from the reviewed manifest and sources"
        )
    lock_file = ExportFile(
        source=repo_root / LOCK_FILENAME,
        source_label=lock_label,
        destination=lock_label,
        content=actual_lock,
        sha256=hashlib.sha256(actual_lock).hexdigest(),
        kind="lock",
    )
    return manifest, (*files, lock_file)


def build_export(destination: Path, files: Sequence[ExportFile]) -> None:
    destination = validate_destination_path(destination)
    if destination.exists() or destination.is_symlink():
        raise BoundaryError(f"destination already exists; refusing to overwrite: {destination}")
    if not destination.parent.is_dir():
        raise BoundaryError(f"destination parent does not exist: {destination.parent}")
    staging = Path(
        tempfile.mkdtemp(prefix=".fitcheck-public-export-", dir=destination.parent)
    )
    try:
        for item in files:
            target = staging.joinpath(*item.destination.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(item.content)
            target.chmod(0o644)
            os.utime(target, (0, 0), follow_symlinks=False)
        directories = [staging, *(path for path in staging.rglob("*") if path.is_dir())]
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            directory.chmod(0o755)
            os.utime(directory, (0, 0), follow_symlinks=False)
        staging.rename(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (default: parent of scripts/)",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        help="new output directory below /private/tmp",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate the complete source set without writing the destination",
    )
    parser.add_argument(
        "--allow-license-blocker",
        action="store_true",
        help="permit LICENSE_REQUIRED.md only for a private dry-run audit",
    )
    parser.add_argument(
        "--print-lock",
        action="store_true",
        help=f"print the deterministic {LOCK_FILENAME} candidate without writing it",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.print_lock:
            if args.destination is not None or args.check_only:
                raise BoundaryError("--print-lock cannot be combined with destination/check-only")
            _, _, lock_content = generate_lockfile(
                args.repo_root,
                allow_license_blocker=args.allow_license_blocker,
            )
            sys.stdout.buffer.write(lock_content)
            return 0
        if args.destination is None:
            raise BoundaryError("--destination is required unless --print-lock is used")
        destination = validate_destination_path(args.destination)
        _, files = prepare_export(
            args.repo_root,
            allow_license_blocker=args.allow_license_blocker,
        )
        if args.check_only:
            print(f"OK: {len(files)} manifest-approved files; destination not written")
            return 0
        build_export(destination, files)
        print(f"OK: wrote {len(files)} files to {destination}")
        return 0
    except BoundaryError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
