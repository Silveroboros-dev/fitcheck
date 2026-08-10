"""Artifact-store contract and atomic local implementation for dev/tests."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from el.retrieval.snapshot_contracts import (
    PublishedSnapshot,
    SnapshotManifest,
    StagedSnapshot,
)
from el.retrieval.snapshot_validation import snapshot_id_for_identity


class SnapshotArtifactError(RuntimeError):
    pass


class SnapshotArtifactConflict(SnapshotArtifactError):
    """An existing content address resolves to different artifact identity."""


class SnapshotArtifactStore(Protocol):
    def artifact_uri(self, snapshot_id: str) -> str: ...

    def publish(
        self,
        staged: StagedSnapshot,
        manifest: SnapshotManifest,
        *,
        progress: Callable[[], None] | None = None,
    ) -> PublishedSnapshot: ...

    def verify(
        self,
        manifest: SnapshotManifest,
        *,
        progress: Callable[[], None] | None = None,
    ) -> None: ...


class LocalSnapshotArtifactStore:
    """Content-addressed local store; not a production Cloud Run volume.

    A complete directory becomes visible in one rename. The live deployment
    will provide a GCS implementation of the same protocol.
    """

    ARTIFACT_NAME = "universe.jsonl"
    MANIFEST_NAME = "manifest.json"
    SNAPSHOT_ID_PATTERN = re.compile(r"^mu_[0-9a-f]{64}$")

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self._refuse_governed_root(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def artifact_uri(self, snapshot_id: str) -> str:
        self._validate_snapshot_id(snapshot_id)
        return (self.root / snapshot_id / self.ARTIFACT_NAME).as_uri()

    def publish(
        self,
        staged: StagedSnapshot,
        manifest: SnapshotManifest,
        *,
        progress: Callable[[], None] | None = None,
    ) -> PublishedSnapshot:
        expected_uri = self.artifact_uri(manifest.snapshot_id)
        if manifest.artifact_uri != expected_uri:
            raise SnapshotArtifactConflict(
                "manifest artifact URI does not match store"
            )
        target = self.root / manifest.snapshot_id
        if target.exists():
            persisted = self._load_manifest(target)
            self.verify(persisted, progress=progress)
            self._require_same_artifact(persisted, manifest)
            return self._published(persisted)

        staging_dir = Path(tempfile.mkdtemp(prefix=".publish-", dir=self.root))
        try:
            artifact = staging_dir / self.ARTIFACT_NAME
            copied_sha, copied_bytes = _copy_and_sha256(
                staged.path,
                artifact,
                progress=progress,
            )
            if (
                copied_sha != manifest.artifact_sha256
                or copied_bytes != manifest.artifact_bytes
            ):
                raise SnapshotArtifactError("staged artifact digest changed")
            manifest_path = staging_dir / self.MANIFEST_NAME
            manifest_path.write_text(
                json.dumps(
                    manifest.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            _fsync_file(artifact)
            _fsync_file(manifest_path)
            _fsync_directory(staging_dir)
            try:
                staging_dir.rename(target)
            except OSError as error:
                # Another identical publisher won the race. Verification
                # distinguishes idempotency from a content-address collision.
                if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
            else:
                _fsync_directory(self.root)
            persisted = self._load_manifest(target)
            self.verify(persisted, progress=progress)
            self._require_same_artifact(persisted, manifest)
            return self._published(persisted)
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir)

    def verify(
        self,
        manifest: SnapshotManifest,
        *,
        progress: Callable[[], None] | None = None,
    ) -> None:
        self._validate_snapshot_id(manifest.snapshot_id)
        directory = self.root / manifest.snapshot_id
        artifact = directory / self.ARTIFACT_NAME
        manifest_path = directory / self.MANIFEST_NAME
        if not artifact.is_file() or not manifest_path.is_file():
            raise SnapshotArtifactConflict("snapshot artifact is incomplete")
        persisted = self._load_manifest(directory)
        self._validate_manifest_consistency(persisted)
        if persisted.artifact_uri != self.artifact_uri(persisted.snapshot_id):
            raise SnapshotArtifactConflict("snapshot manifest URI mismatch")
        if artifact.stat().st_size != persisted.artifact_bytes:
            raise SnapshotArtifactConflict("snapshot artifact size mismatch")
        if _file_sha256(artifact, progress=progress) != persisted.artifact_sha256:
            raise SnapshotArtifactConflict("snapshot artifact digest mismatch")
        self._require_same_artifact(persisted, manifest)

    def _load_manifest(self, directory: Path) -> SnapshotManifest:
        manifest_path = directory / self.MANIFEST_NAME
        try:
            return SnapshotManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except Exception as error:
            raise SnapshotArtifactConflict("snapshot manifest is invalid") from error

    @staticmethod
    def _validate_manifest_consistency(manifest: SnapshotManifest) -> None:
        report = manifest.validation_report
        expected_snapshot_id = snapshot_id_for_identity(
            provider=manifest.provider,
            venue=manifest.venue,
            cutoff_utc=manifest.cutoff_utc,
            content_sha256=manifest.content_sha256,
            normalization_policy_version=manifest.normalization_policy_version,
        )
        report_fields_match = (
            report.passed
            and report.content_sha256 == manifest.content_sha256
            and report.membership_sha256 == manifest.membership_sha256
            and report.artifact_sha256 == manifest.artifact_sha256
            and report.artifact_bytes == manifest.artifact_bytes
            and report.row_count == manifest.row_count
            and report.unique_market_count == manifest.unique_market_count
            and report.open_market_count == manifest.open_market_count
        )
        if manifest.snapshot_id != expected_snapshot_id or not report_fields_match:
            raise SnapshotArtifactConflict("snapshot manifest identity mismatch")

    @staticmethod
    def _require_same_artifact(
        persisted: SnapshotManifest, requested: SnapshotManifest
    ) -> None:
        if _artifact_identity(persisted) != _artifact_identity(requested):
            raise SnapshotArtifactConflict("snapshot artifact identity mismatch")

    def _published(self, manifest: SnapshotManifest) -> PublishedSnapshot:
        directory = self.root / manifest.snapshot_id
        return PublishedSnapshot(
            snapshot_id=manifest.snapshot_id,
            artifact_uri=(directory / self.ARTIFACT_NAME).as_uri(),
            manifest_uri=(directory / self.MANIFEST_NAME).as_uri(),
            manifest=manifest,
        )

    @classmethod
    def _validate_snapshot_id(cls, snapshot_id: str) -> None:
        if cls.SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None:
            raise SnapshotArtifactConflict("invalid snapshot ID")

    @staticmethod
    def _refuse_governed_root(root: Path) -> None:
        normalized = root.as_posix().rstrip("/")
        forbidden = (
            "/data/review",
            "/data/clean_goldens",
            "/data/eval_sets",
            "/data/review_batches",
            "/docs/archive",
        )
        if any(
            normalized == suffix or normalized.startswith(f"{suffix}/")
            or suffix + "/" in normalized + "/"
            for suffix in forbidden
        ):
            raise SnapshotArtifactError(
                "snapshot artifacts cannot be written under governed data roots"
            )


def _file_sha256(
    path: Path,
    *,
    progress: Callable[[], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    processed = 0
    next_progress = 64 * 1024 * 1024
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            processed += len(chunk)
            if progress is not None and processed >= next_progress:
                progress()
                next_progress = processed + 64 * 1024 * 1024
    if progress is not None:
        progress()
    return digest.hexdigest()


def _copy_and_sha256(
    source: Path,
    target: Path,
    *,
    progress: Callable[[], None] | None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    copied = 0
    next_progress = 64 * 1024 * 1024
    with source.open("rb") as input_handle, target.open("wb") as output_handle:
        for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
            output_handle.write(chunk)
            digest.update(chunk)
            copied += len(chunk)
            if progress is not None and copied >= next_progress:
                progress()
                next_progress = copied + 64 * 1024 * 1024
    if progress is not None:
        progress()
    return digest.hexdigest(), copied


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _artifact_identity(manifest: SnapshotManifest) -> tuple:
    """Stable artifact identity, excluding build/validation attempt metadata."""

    return (
        manifest.schema_version,
        manifest.snapshot_id,
        manifest.provider,
        manifest.venue,
        manifest.cutoff_utc,
        manifest.artifact_format,
        manifest.artifact_uri,
        manifest.artifact_sha256,
        manifest.artifact_bytes,
        manifest.content_sha256,
        manifest.membership_sha256,
        manifest.row_count,
        manifest.unique_market_count,
        manifest.open_market_count,
        manifest.normalization_policy_version,
    )
