#!/usr/bin/env python3
"""Verify a candidate against its canonical sources or immutable public lock."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Iterator, Sequence

# This checker must not mutate the tree it is about to attest. Set the runtime
# switch before importing the sibling module, which would otherwise create
# scripts/__pycache__ on a pristine public checkout.
sys.dont_write_bytecode = True

from build_public_export import (
    BoundaryError,
    HARD_MAX_FILE_BYTES,
    LOCK_FILENAME,
    _assert_destination_path_allowed,
    _assert_no_symlink_components,
    _read_rooted_regular_file,
    _scan_text,
    enforce_license_gate,
    parse_lockfile,
    prepare_export,
    validate_destination_path,
    IGNORED_RUNTIME_DIRECTORIES,
    IGNORED_RUNTIME_SUFFIXES,
)


def _walk_tree(
    root: Path, *, ignore_root_git: bool = False
) -> Iterator[tuple[PurePosixPath, Path]]:
    """Walk an output tree without following or reading ignored directories."""

    def visit(directory: Path, relative: PurePosixPath) -> Iterator[tuple[PurePosixPath, Path]]:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise BoundaryError(f"cannot enumerate export tree {relative}: {error}") from error
        for entry in entries:
            child = relative / entry.name
            if entry.name == ".git":
                if relative == PurePosixPath() and ignore_root_git:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        raise BoundaryError("root .git must be a real directory when ignored")
                    # A self-check may run in a checkout. Git history is never read.
                    continue
                raise BoundaryError(f"nested .git is forbidden in public output: {child}")
            if entry.is_symlink():
                raise BoundaryError(f"symlink is forbidden in public output: {child}")
            if entry.is_dir(follow_symlinks=False):
                if entry.name in IGNORED_RUNTIME_DIRECTORIES:
                    raise BoundaryError(f"runtime cache is forbidden in public output: {child}")
                _assert_destination_path_allowed(child)
                yield from visit(Path(entry.path), child)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise BoundaryError(f"non-regular public output: {child}")
            _assert_destination_path_allowed(child)
            if Path(entry.name).suffix in IGNORED_RUNTIME_SUFFIXES:
                raise BoundaryError(f"runtime bytecode is forbidden in public output: {child}")
            yield child, Path(entry.path)

    yield from visit(root, PurePosixPath())


def _read_output_tree(
    root: Path,
    max_file_bytes: int,
    *,
    ignore_root_git: bool = False,
) -> dict[str, bytes]:
    values: dict[str, bytes] = {}
    folded: dict[str, str] = {}
    for label, _ in _walk_tree(root, ignore_root_git=ignore_root_git):
        name = label.as_posix()
        lower = name.casefold()
        if lower in folded:
            raise BoundaryError(
                f"case-insensitive output collision: {folded[lower]} and {name}"
            )
        _assert_no_symlink_components(root, label, label=name)
        content = _read_rooted_regular_file(root, label, max_file_bytes, name)
        _scan_text(content, name)
        values[name] = content
        folded[lower] = name
    return values


def check_export(
    repo_root: Path,
    destination: Path,
    *,
    self_check: bool = False,
    allow_license_blocker: bool = False,
) -> int:
    repo_root = repo_root.resolve()
    if self_check:
        destination = destination.resolve()
        if destination != repo_root:
            raise BoundaryError("--self-check requires --destination to equal --repo-root")
        lock_label = PurePosixPath(LOCK_FILENAME)
        _assert_no_symlink_components(destination, lock_label, label=LOCK_FILENAME)
        lock_content = _read_rooted_regular_file(
            destination,
            lock_label,
            HARD_MAX_FILE_BYTES,
            LOCK_FILENAME,
        )
        _scan_text(lock_content, LOCK_FILENAME)
        max_file_bytes, expected_hashes = parse_lockfile(lock_content)
        expected_names = {*expected_hashes, LOCK_FILENAME}
        ignore_root_git = True
    else:
        destination = validate_destination_path(destination)
        manifest, files = prepare_export(
            repo_root,
            allow_license_blocker=allow_license_blocker,
        )
        max_file_bytes = manifest.max_file_bytes
        expected_names = {item.destination.as_posix() for item in files}
        expected_hashes = {item.destination.as_posix(): item.sha256 for item in files}
        ignore_root_git = False
    if not destination.is_dir() or destination.is_symlink():
        raise BoundaryError(f"export destination is not a regular directory: {destination}")

    actual = _read_output_tree(
        destination,
        max_file_bytes,
        ignore_root_git=ignore_root_git,
    )
    actual_names = set(actual)
    unexpected = sorted(actual_names - expected_names)
    missing = sorted(expected_names - actual_names)
    if unexpected or missing:
        raise BoundaryError(
            f"output differs from manifest; unexpected={unexpected}, missing={missing}"
        )
    enforce_license_gate(
        (PurePosixPath(name) for name in actual_names),
        allow_license_blocker=allow_license_blocker,
    )
    changed = sorted(
        name
        for name, digest in expected_hashes.items()
        if hashlib.sha256(actual[name]).hexdigest() != digest
    )
    if changed:
        raise BoundaryError(f"output content differs from immutable lock: {changed}")
    return len(actual)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="canonical root, or the checked root with --self-check",
    )
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--self-check",
        action="store_true",
        help=f"check a public checkout only against its {LOCK_FILENAME}",
    )
    parser.add_argument(
        "--allow-license-blocker",
        action="store_true",
        help="permit LICENSE_REQUIRED.md only for a private dry-run audit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        count = check_export(
            args.repo_root,
            args.destination,
            self_check=args.self_check,
            allow_license_blocker=args.allow_license_blocker,
        )
        print(f"OK: {count} files match the public boundary")
        return 0
    except BoundaryError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
