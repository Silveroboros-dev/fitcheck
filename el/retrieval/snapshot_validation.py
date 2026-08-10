"""Deterministic validation and content identity for universe artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from el.retrieval.snapshot_contracts import (
    NormalizedUniverseMarket,
    SnapshotManifest,
    SnapshotValidationPolicy,
    SnapshotValidationReport,
    StagedSnapshot,
)


class SnapshotValidationError(RuntimeError):
    def __init__(self, report: SnapshotValidationReport):
        super().__init__("snapshot validation failed")
        self.report = report


def canonical_row_json(row: NormalizedUniverseMarket) -> str:
    return json.dumps(
        row.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def stage_fixture_jsonl_rows(
    rows: Iterable[NormalizedUniverseMarket | dict], path: str | Path
) -> Path:
    """Canonical dev/test staging helper.

    This helper sorts in memory and is therefore not the live 1.9M-row path.
    A production PolyData source must perform an external/columnar sort and
    stream the same canonical JSONL contract without ``frame.to_dicts()``.
    """

    normalized = [
        row
        if isinstance(row, NormalizedUniverseMarket)
        else NormalizedUniverseMarket.model_validate(row)
        for row in rows
    ]
    normalized.sort(key=lambda row: row.market_id)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for row in normalized:
            handle.write(canonical_row_json(row))
            handle.write("\n")
    return target


def validate_staged_snapshot(
    staged: StagedSnapshot,
    policy: SnapshotValidationPolicy,
    *,
    progress: Callable[[], None] | None = None,
    progress_every_rows: int = 50_000,
) -> SnapshotValidationReport:
    if progress_every_rows < 1:
        raise ValueError("progress_every_rows must be positive")
    artifact_path = staged.path
    artifact_sha = hashlib.sha256()
    artifact_bytes = 0
    content_sha = hashlib.sha256()
    membership_sha = hashlib.sha256()
    row_count = 0
    unique_count = 0
    open_count = 0
    last_market_id: str | None = None
    sentinels: dict[str, NormalizedUniverseMarket] = {}
    required = set(policy.required_sentinel_ids)
    future_ids: list[str] = []
    duplicate_ids: list[str] = []
    invalid_rows: list[str] = []
    ordering_invalid = False
    noncanonical = False
    invalid_utf8 = False

    def record(bucket: list[str], value: str) -> None:
        if len(bucket) < policy.diagnostics_limit:
            bucket.append(value)

    # Hash and validate the same bytes in one pass. Publication checks this
    # digest again, closing the validation/copy race without loading the file.
    with artifact_path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if progress is not None and line_number % progress_every_rows == 0:
                progress()
            artifact_sha.update(raw_line)
            artifact_bytes += len(raw_line)
            try:
                decoded = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                row_count += 1
                invalid_utf8 = True
                record(invalid_rows, f"line:{line_number}:invalid_utf8")
                content_sha.update(raw_line)
                continue

            has_lf = decoded.endswith("\n")
            line = decoded[:-1] if has_lf else decoded
            if not line:
                record(invalid_rows, f"line:{line_number}:blank")
                if raw_line != b"\n":
                    noncanonical = True
                continue
            row_count += 1
            try:
                row = NormalizedUniverseMarket.model_validate_json(line)
            except ValidationError:
                record(invalid_rows, f"line:{line_number}:schema_invalid")
                content_sha.update(raw_line)
                continue

            canonical = canonical_row_json(row)
            if not has_lf or line != canonical:
                noncanonical = True
                record(invalid_rows, f"line:{line_number}:noncanonical")
            content_sha.update(canonical.encode())
            content_sha.update(b"\n")
            membership = json.dumps(
                {
                    "market_id": row.market_id,
                    "snapshot_ts": row.snapshot_ts.isoformat(),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            membership_sha.update(membership.encode())
            membership_sha.update(b"\n")

            if last_market_id is not None and row.market_id <= last_market_id:
                ordering_invalid = True
                if row.market_id == last_market_id:
                    record(duplicate_ids, row.market_id)
            else:
                unique_count += 1
            last_market_id = row.market_id
            if row.closed_time is None:
                open_count += 1
            if row.snapshot_ts > staged.cutoff_utc:
                record(future_ids, row.market_id)
            if row.market_id in required:
                sentinels[row.market_id] = row

    if progress is not None:
        progress()

    missing = sorted(required - set(sentinels))
    closed = sorted(
        market_id for market_id, row in sentinels.items()
        if row.closed_time is not None
    )
    nonbinary = sorted(
        market_id
        for market_id, row in sentinels.items()
        if row.outcomes != ["Yes", "No"] or len(row.token_ids) != 2
    )
    timestamp_mismatch = sorted(
        market_id
        for market_id, row in sentinels.items()
        if policy.require_sentinel_cutoff_timestamp
        and row.snapshot_ts != staged.cutoff_utc
    )

    direct_match = True
    if policy.direct_sentinel_id:
        direct = sentinels.get(policy.direct_sentinel_id)
        direct_match = bool(direct) and (
            (
                policy.direct_expected_title is not None
                and direct.title == policy.direct_expected_title
            )
            or (
                policy.direct_expected_slug is not None
                and direct.slug == policy.direct_expected_slug
            )
        )

    errors: list[str] = []
    if row_count < policy.minimum_row_count:
        errors.append("row_count_below_minimum")
    if invalid_rows:
        errors.append("schema_invalid")
    if invalid_utf8:
        errors.append("artifact_not_utf8")
    if noncanonical:
        errors.append("artifact_not_canonical_jsonl")
    if ordering_invalid:
        errors.append("market_ids_not_strictly_sorted_unique")
    if future_ids:
        errors.append("snapshot_after_cutoff")
    if missing:
        errors.append("required_sentinel_missing")
    if closed:
        errors.append("required_sentinel_closed")
    if nonbinary:
        errors.append("required_sentinel_nonbinary")
    if timestamp_mismatch:
        errors.append("required_sentinel_timestamp_mismatch")
    if not direct_match:
        errors.append("direct_sentinel_mismatch")
    if unique_count != row_count:
        errors.append("market_id_count_mismatch")

    return SnapshotValidationReport(
        passed=not errors,
        row_count=row_count,
        unique_market_count=unique_count,
        open_market_count=open_count,
        content_sha256=content_sha.hexdigest(),
        membership_sha256=membership_sha.hexdigest(),
        artifact_sha256=artifact_sha.hexdigest(),
        artifact_bytes=artifact_bytes,
        missing_sentinel_ids=tuple(missing[: policy.diagnostics_limit]),
        closed_sentinel_ids=tuple(closed[: policy.diagnostics_limit]),
        nonbinary_sentinel_ids=tuple(nonbinary[: policy.diagnostics_limit]),
        sentinel_timestamp_mismatch_ids=tuple(
            timestamp_mismatch[: policy.diagnostics_limit]
        ),
        future_market_ids=tuple(future_ids),
        duplicate_market_ids=tuple(duplicate_ids),
        invalid_rows=tuple(invalid_rows),
        direct_sentinel_match=direct_match,
        errors=tuple(errors),
    )


def universe_snapshot_id(
    staged: StagedSnapshot,
    content_sha256: str,
) -> str:
    return snapshot_id_for_identity(
        provider=staged.key.provider,
        venue=staged.key.venue,
        cutoff_utc=staged.cutoff_utc,
        content_sha256=content_sha256,
        normalization_policy_version=staged.normalization_policy_version,
    )


def snapshot_id_for_identity(
    *,
    provider: str,
    venue: str,
    cutoff_utc: datetime,
    content_sha256: str,
    normalization_policy_version: str,
) -> str:
    identity = json.dumps(
        {
            "provider": provider,
            "venue": venue,
            "cutoff_utc": cutoff_utc.astimezone(timezone.utc).isoformat(),
            "content_sha256": content_sha256,
            "normalization_policy_version": normalization_policy_version,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"mu_{hashlib.sha256(identity.encode()).hexdigest()}"


def build_manifest(
    staged: StagedSnapshot,
    report: SnapshotValidationReport,
    policy: SnapshotValidationPolicy,
    *,
    artifact_uri: str,
    generated_at: datetime | None = None,
) -> SnapshotManifest:
    if not report.passed:
        raise SnapshotValidationError(report)
    return SnapshotManifest(
        snapshot_id=universe_snapshot_id(staged, report.content_sha256),
        provider=staged.key.provider,
        venue=staged.key.venue,
        cutoff_utc=staged.cutoff_utc,
        generated_at_utc=generated_at or datetime.now(timezone.utc),
        artifact_format=staged.artifact_format,
        artifact_uri=artifact_uri,
        artifact_sha256=report.artifact_sha256,
        artifact_bytes=report.artifact_bytes,
        content_sha256=report.content_sha256,
        membership_sha256=report.membership_sha256,
        row_count=report.row_count,
        unique_market_count=report.unique_market_count,
        open_market_count=report.open_market_count,
        normalization_policy_version=staged.normalization_policy_version,
        validation_policy_version=policy.version,
        source_versions=staged.source_versions,
        validation_report=report,
    )
