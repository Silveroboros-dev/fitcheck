"""Bounded lexical candidate index over an immutable universe snapshot.

The index is a rebuildable derivative of a validated ``jsonl-v1`` snapshot.
Construction streams one market at a time into a temporary SQLite database;
publication uses a no-clobber hard link so a complete index becomes visible at
once. Queries read only term postings and hydrate at most the requested limit.

This module deliberately knows nothing about PolyData, object storage, model
adapters, jobs, or product APIs. A later worker may resolve a pinned snapshot
to a local artifact and use this provider-neutral boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from el.domain.structures import ExtractedStructure
from el.retrieval.candidate_contracts import (
    MAX_CANDIDATE_QUERY_LIMIT,
    CandidateIndexError,
    CandidateIndexIntegrityError,
    CandidateIndexPermanentError,
    CandidateQueryHit,
    CandidateQueryResult,
)
from el.retrieval.snapshot_contracts import (
    NormalizedUniverseMarket,
    SnapshotManifest,
)
from el.retrieval.snapshot_validation import (
    canonical_row_json,
    snapshot_id_for_identity,
)

CANDIDATE_INDEX_POLICY_VERSION = "lexical-postings-v1"
INDEX_SCHEMA_VERSION = 1
MAX_QUERY_LIMIT = MAX_CANDIDATE_QUERY_LIMIT
MAX_QUERY_TERMS = 64
MAX_TERMS_PER_MARKET = 4096
MAX_TERM_LENGTH = 64
MAX_POSTING_BUDGET = 5_000
MAX_ARTIFACT_ROW_BYTES = 1024 * 1024
DEFAULT_PROGRESS_EVERY_ROWS = 50_000

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_SHORT_TERMS = frozenset({"ai", "eu", "uk", "us"})
_STOPWORDS = frozenset(
    {
        "and",
        "are",
        "end",
        "for",
        "from",
        "has",
        "have",
        "into",
        "not",
        "of",
        "on",
        "or",
        "the",
        "this",
        "that",
        "their",
        "to",
        "will",
        "with",
        "unspecified",
    }
)
_REQUIRED_METADATA = frozenset(
    {
        "schema_version",
        "snapshot_id",
        "content_sha256",
        "artifact_sha256",
        "index_policy_version",
        "row_count",
        "open_market_count",
        "indexed_term_count",
        "max_posting_budget",
    }
)


class CandidateIndexArtifactError(CandidateIndexPermanentError):
    """The pinned snapshot artifact does not satisfy its manifest."""


class CandidateIndexIdentityConflict(CandidateIndexIntegrityError):
    """An existing index does not match the requested immutable identity."""


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CandidateIndexBuildResult(_Contract):
    index_path: Path
    snapshot_id: str
    content_sha256: str
    artifact_sha256: str
    index_policy_version: str
    row_count: int = Field(ge=1)
    open_market_count: int = Field(ge=0)
    indexed_term_count: int = Field(ge=0)
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reused: bool


class CandidateIndexHit(CandidateQueryHit):
    overlap_terms: int = Field(ge=1)


class CandidateIndexQueryResult(CandidateQueryResult):
    backend: str = "sqlite_lexical"
    query_terms: tuple[str, ...]
    selected_terms: tuple[str, ...]
    posting_count: int = Field(ge=0, le=MAX_POSTING_BUDGET)
    posting_budget: int = MAX_POSTING_BUDGET
    posting_budget_exhausted: bool
    hits: tuple[CandidateIndexHit, ...]


@dataclass(frozen=True)
class SqliteCandidateIndex:
    """Read-only handle to one pinned, provider-neutral candidate index."""

    index_path: Path
    snapshot_id: str
    content_sha256: str
    artifact_sha256: str
    index_sha256: str
    index_policy_version: str
    _file_fingerprint: tuple[int, int, int, int]

    @property
    def backend(self) -> str:
        return "sqlite_lexical"

    @classmethod
    def build(
        cls,
        *,
        manifest: SnapshotManifest,
        artifact_path: str | Path,
        index_path: str | Path,
        progress: Callable[[], None] | None = None,
        progress_every_rows: int = DEFAULT_PROGRESS_EVERY_ROWS,
        expected_index_sha256: str | None = None,
    ) -> CandidateIndexBuildResult:
        """Stream a verified snapshot into an atomically published index."""

        if progress_every_rows < 1:
            raise ValueError("progress_every_rows must be positive")
        _validate_manifest(manifest)

        artifact = Path(artifact_path).resolve()
        target = Path(index_path).resolve()
        _refuse_governed_target(target)
        if artifact == target:
            raise CandidateIndexArtifactError(
                "snapshot artifact and candidate index paths must differ"
            )
        if not artifact.is_file():
            raise CandidateIndexArtifactError("snapshot artifact is missing")
        target.parent.mkdir(parents=True, exist_ok=True)

        # A reuse request still verifies the source bytes. The existing index
        # is only a derivative; the pinned snapshot remains the authority.
        if target.exists():
            if expected_index_sha256 is None:
                raise CandidateIndexIdentityConflict(
                    "existing candidate index requires its pinned digest"
                )
            _verify_artifact_digest(artifact, manifest)
            existing = cls.open(
                target,
                expected_snapshot_id=manifest.snapshot_id,
                expected_content_sha256=manifest.content_sha256,
                expected_artifact_sha256=manifest.artifact_sha256,
                expected_index_sha256=expected_index_sha256,
            )
            metadata = _read_metadata(existing.index_path)
            return _build_result(
                existing,
                metadata,
                reused=True,
            )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(temporary)
            _configure_build_connection(connection)
            _create_schema(connection)
            observed = _populate_index(
                connection,
                manifest=manifest,
                artifact_path=artifact,
                progress=progress,
                progress_every_rows=progress_every_rows,
            )
            _write_metadata(connection, manifest, observed)
            connection.commit()
            check = connection.execute("PRAGMA quick_check").fetchone()
            if check != ("ok",):
                raise CandidateIndexArtifactError(
                    "candidate index integrity check failed"
                )
            connection.close()
            connection = None
            _fsync_file(temporary)
            built_sha256 = _file_sha256(temporary)
            if (
                expected_index_sha256 is not None
                and built_sha256 != expected_index_sha256
            ):
                raise CandidateIndexIdentityConflict(
                    "rebuilt candidate index differs from pinned digest"
                )

            published_here = False
            try:
                # A hard link fails instead of overwriting a concurrent winner.
                # Both paths are in the same directory/filesystem.
                os.link(temporary, target)
                published_here = True
                _fsync_directory(target.parent)
            except FileExistsError:
                pass

            index = cls.open(
                target,
                expected_snapshot_id=manifest.snapshot_id,
                expected_content_sha256=manifest.content_sha256,
                expected_artifact_sha256=manifest.artifact_sha256,
                expected_index_sha256=(
                    expected_index_sha256 or built_sha256
                ),
            )
            metadata = _read_metadata(index.index_path)
            return _build_result(
                index,
                metadata,
                reused=not published_here,
            )
        except sqlite3.Error as error:
            raise CandidateIndexArtifactError(
                "candidate index build failed"
            ) from error
        finally:
            if connection is not None:
                connection.close()
            temporary.unlink(missing_ok=True)

    @classmethod
    def open(
        cls,
        index_path: str | Path,
        *,
        expected_snapshot_id: str,
        expected_content_sha256: str,
        expected_artifact_sha256: str,
        expected_index_sha256: str,
        expected_policy_version: str = CANDIDATE_INDEX_POLICY_VERSION,
    ) -> "SqliteCandidateIndex":
        """Open an index read-only and fail closed on identity drift."""

        target = Path(index_path).resolve()
        if not target.is_file():
            raise CandidateIndexIdentityConflict("candidate index is missing")
        before = _file_fingerprint(target)
        if _file_sha256(target) != expected_index_sha256:
            raise CandidateIndexIdentityConflict(
                "candidate index digest does not match pinned identity"
            )
        try:
            with closing(_readonly_connection(target)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()
                check = connection.execute("PRAGMA quick_check").fetchone()
                metadata = _metadata_from_connection(connection)
        except sqlite3.Error as error:
            raise CandidateIndexIdentityConflict(
                "candidate index is invalid"
            ) from error
        after = _file_fingerprint(target)
        if after != before:
            raise CandidateIndexIdentityConflict(
                "candidate index changed while opening"
            )
        if version != (INDEX_SCHEMA_VERSION,) or check != ("ok",):
            raise CandidateIndexIdentityConflict(
                "candidate index integrity check failed"
            )
        _require_metadata_identity(
            metadata,
            snapshot_id=expected_snapshot_id,
            content_sha256=expected_content_sha256,
            artifact_sha256=expected_artifact_sha256,
            policy_version=expected_policy_version,
        )
        return cls(
            index_path=target,
            snapshot_id=expected_snapshot_id,
            content_sha256=expected_content_sha256,
            artifact_sha256=expected_artifact_sha256,
            index_sha256=expected_index_sha256,
            index_policy_version=expected_policy_version,
            _file_fingerprint=after,
        )

    def query(
        self,
        structure: ExtractedStructure,
        *,
        limit: int,
    ) -> CandidateIndexQueryResult:
        """Return at most ``limit`` open markets from indexed postings."""

        if not 1 <= limit <= MAX_QUERY_LIMIT:
            raise ValueError(
                f"candidate index limit must be between 1 and {MAX_QUERY_LIMIT}"
            )
        self._require_unchanged_file()
        terms = _query_terms(structure)
        if not terms:
            return self._query_result(
                query_terms=terms,
                selected_terms=(),
                posting_count=0,
                posting_budget_exhausted=False,
                limit=limit,
                hits=(),
            )

        try:
            with closing(_readonly_connection(self.index_path)) as connection:
                metadata = _metadata_from_connection(connection)
                _require_metadata_identity(
                    metadata,
                    snapshot_id=self.snapshot_id,
                    content_sha256=self.content_sha256,
                    artifact_sha256=self.artifact_sha256,
                    policy_version=self.index_policy_version,
                )
                selected_terms, posting_count, exhausted = _select_query_terms(
                    connection,
                    terms,
                    open_market_count=int(metadata["open_market_count"]),
                )
                if not selected_terms:
                    rows = []
                else:
                    placeholders = ",".join("?" for _ in selected_terms)
                    statement = f"""
                        WITH matched AS (
                            SELECT market_id, COUNT(*) AS overlap_terms
                            FROM terms
                            WHERE term IN ({placeholders})
                            GROUP BY market_id
                        )
                        SELECT markets.payload_json, matched.overlap_terms
                        FROM matched
                        JOIN markets
                          ON markets.market_id = matched.market_id
                        WHERE markets.is_open = 1
                        ORDER BY matched.overlap_terms DESC,
                                 markets.market_id ASC
                        LIMIT ?
                    """
                    rows = connection.execute(
                        statement,
                        (*selected_terms, limit),
                    ).fetchall()
        except sqlite3.Error as error:
            raise CandidateIndexIdentityConflict(
                "candidate index query failed"
            ) from error

        hits: list[CandidateIndexHit] = []
        for payload_json, overlap_terms in rows:
            try:
                market = NormalizedUniverseMarket.model_validate_json(payload_json)
            except ValidationError as error:
                raise CandidateIndexIdentityConflict(
                    "candidate index contains an invalid market"
                ) from error
            hits.append(
                CandidateIndexHit(
                    market=market,
                    overlap_terms=int(overlap_terms),
                )
            )
        self._require_unchanged_file()
        return self._query_result(
            query_terms=terms,
            selected_terms=selected_terms,
            posting_count=posting_count,
            posting_budget_exhausted=exhausted,
            limit=limit,
            hits=tuple(hits),
        )

    def _query_result(
        self,
        *,
        query_terms: tuple[str, ...],
        selected_terms: tuple[str, ...],
        posting_count: int,
        posting_budget_exhausted: bool,
        limit: int,
        hits: tuple[CandidateIndexHit, ...],
    ) -> CandidateIndexQueryResult:
        digest = _query_digest(
            snapshot_id=self.snapshot_id,
            content_sha256=self.content_sha256,
            index_sha256=self.index_sha256,
            policy_version=self.index_policy_version,
            query_terms=query_terms,
            selected_terms=selected_terms,
            posting_count=posting_count,
            limit=limit,
        )
        return CandidateIndexQueryResult(
            snapshot_id=self.snapshot_id,
            content_sha256=self.content_sha256,
            artifact_sha256=self.artifact_sha256,
            index_sha256=self.index_sha256,
            index_policy_version=self.index_policy_version,
            query_digest=digest,
            query_terms=query_terms,
            selected_terms=selected_terms,
            posting_count=posting_count,
            posting_budget_exhausted=posting_budget_exhausted,
            limit=limit,
            hits=hits,
        )

    def _require_unchanged_file(self) -> None:
        if _file_fingerprint(self.index_path) != self._file_fingerprint:
            raise CandidateIndexIdentityConflict(
                "candidate index changed after identity verification"
            )


@dataclass(frozen=True)
class _ObservedBuild:
    row_count: int
    open_market_count: int
    indexed_term_count: int


def _validate_manifest(manifest: SnapshotManifest) -> None:
    report = manifest.validation_report
    expected_snapshot_id = snapshot_id_for_identity(
        provider=manifest.provider,
        venue=manifest.venue,
        cutoff_utc=manifest.cutoff_utc,
        content_sha256=manifest.content_sha256,
        normalization_policy_version=manifest.normalization_policy_version,
    )
    consistent = (
        manifest.artifact_format == "jsonl-v1"
        and manifest.snapshot_id == expected_snapshot_id
        and report.passed
        and not report.errors
        and manifest.row_count > 0
        and manifest.unique_market_count == manifest.row_count
        and 0 <= manifest.open_market_count <= manifest.row_count
        and report.row_count == manifest.row_count
        and report.unique_market_count == manifest.unique_market_count
        and report.open_market_count == manifest.open_market_count
        and report.artifact_sha256 == manifest.artifact_sha256
        and report.artifact_bytes == manifest.artifact_bytes
        and report.content_sha256 == manifest.content_sha256
        and report.membership_sha256 == manifest.membership_sha256
    )
    if not consistent:
        raise CandidateIndexArtifactError("snapshot manifest identity is invalid")


def _configure_build_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE markets (
            market_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            is_open INTEGER NOT NULL CHECK (is_open IN (0, 1))
        ) WITHOUT ROWID;
        CREATE TABLE terms (
            term TEXT NOT NULL,
            market_id TEXT NOT NULL,
            PRIMARY KEY (term, market_id),
            FOREIGN KEY (market_id) REFERENCES markets(market_id)
        ) WITHOUT ROWID;
        CREATE TABLE term_stats (
            term TEXT PRIMARY KEY,
            document_count INTEGER NOT NULL CHECK (document_count > 0)
        ) WITHOUT ROWID;
        """
    )


def _populate_index(
    connection: sqlite3.Connection,
    *,
    manifest: SnapshotManifest,
    artifact_path: Path,
    progress: Callable[[], None] | None,
    progress_every_rows: int,
) -> _ObservedBuild:
    artifact_sha = hashlib.sha256()
    content_sha = hashlib.sha256()
    membership_sha = hashlib.sha256()
    artifact_bytes = 0
    row_count = 0
    open_market_count = 0
    indexed_term_count = 0
    last_market_id: str | None = None

    with artifact_path.open("rb") as handle:
        line_number = 0
        while True:
            raw_line = handle.readline(MAX_ARTIFACT_ROW_BYTES + 1)
            if not raw_line:
                break
            line_number += 1
            if len(raw_line) > MAX_ARTIFACT_ROW_BYTES:
                raise CandidateIndexArtifactError(
                    f"snapshot artifact line {line_number} exceeds byte ceiling"
                )
            artifact_sha.update(raw_line)
            artifact_bytes += len(raw_line)
            if not raw_line.endswith(b"\n"):
                raise CandidateIndexArtifactError(
                    f"snapshot artifact line {line_number} is not canonical"
                )
            try:
                line = raw_line[:-1].decode("utf-8")
                market = NormalizedUniverseMarket.model_validate_json(line)
            except (UnicodeDecodeError, ValidationError) as error:
                raise CandidateIndexArtifactError(
                    f"snapshot artifact line {line_number} is invalid"
                ) from error
            canonical = canonical_row_json(market)
            if not line or line != canonical:
                raise CandidateIndexArtifactError(
                    f"snapshot artifact line {line_number} is not canonical"
                )
            if last_market_id is not None and market.market_id <= last_market_id:
                raise CandidateIndexArtifactError(
                    "snapshot market IDs are not strictly sorted and unique"
                )
            if market.snapshot_ts > manifest.cutoff_utc:
                raise CandidateIndexArtifactError(
                    "snapshot contains data after its pinned cutoff"
                )

            content_sha.update(canonical.encode("utf-8"))
            content_sha.update(b"\n")
            membership = json.dumps(
                {
                    "market_id": market.market_id,
                    "snapshot_ts": market.snapshot_ts.isoformat(),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            membership_sha.update(membership.encode("utf-8"))
            membership_sha.update(b"\n")

            connection.execute(
                "INSERT INTO markets(market_id, payload_json, is_open) "
                "VALUES (?, ?, ?)",
                (market.market_id, canonical, int(market.is_open)),
            )
            if market.is_open:
                terms = _market_terms(market)
                connection.executemany(
                    "INSERT INTO terms(term, market_id) VALUES (?, ?)",
                    ((term, market.market_id) for term in terms),
                )
                connection.executemany(
                    """
                    INSERT INTO term_stats(term, document_count) VALUES (?, 1)
                    ON CONFLICT(term) DO UPDATE SET
                        document_count = document_count + 1
                    """,
                    ((term,) for term in terms),
                )
                open_market_count += 1
                indexed_term_count += len(terms)

            row_count += 1
            last_market_id = market.market_id
            if row_count % progress_every_rows == 0:
                connection.commit()
                if progress is not None:
                    progress()

    if progress is not None:
        progress()
    observed_sha = artifact_sha.hexdigest()
    failures = (
        artifact_bytes != manifest.artifact_bytes
        or observed_sha != manifest.artifact_sha256
        or content_sha.hexdigest() != manifest.content_sha256
        or membership_sha.hexdigest() != manifest.membership_sha256
        or row_count != manifest.row_count
        or row_count != manifest.unique_market_count
        or open_market_count != manifest.open_market_count
    )
    if failures:
        raise CandidateIndexArtifactError(
            "snapshot artifact does not match its pinned manifest"
        )
    return _ObservedBuild(
        row_count=row_count,
        open_market_count=open_market_count,
        indexed_term_count=indexed_term_count,
    )


def _write_metadata(
    connection: sqlite3.Connection,
    manifest: SnapshotManifest,
    observed: _ObservedBuild,
) -> None:
    values = {
        "schema_version": str(INDEX_SCHEMA_VERSION),
        "snapshot_id": manifest.snapshot_id,
        "content_sha256": manifest.content_sha256,
        "artifact_sha256": manifest.artifact_sha256,
        "index_policy_version": CANDIDATE_INDEX_POLICY_VERSION,
        "row_count": str(observed.row_count),
        "open_market_count": str(observed.open_market_count),
        "indexed_term_count": str(observed.indexed_term_count),
        "max_posting_budget": str(MAX_POSTING_BUDGET),
    }
    connection.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        sorted(values.items()),
    )


def _query_terms(structure: ExtractedStructure) -> tuple[str, ...]:
    texts: Iterable[str] = (
        *(entity.name for entity in structure.entities),
        structure.claim_summary,
        structure.contractible_version,
        structure.metric.what,
        structure.metric.measured_by,
    )
    return _terms(texts, limit=MAX_QUERY_TERMS)


def _market_terms(market: NormalizedUniverseMarket) -> tuple[str, ...]:
    texts = (
        market.title,
        market.description,
        market.resolution_rules,
        market.taxonomy_l1 or "",
        *market.tags,
    )
    return _terms(texts, limit=MAX_TERMS_PER_MARKET)


def _terms(texts: Iterable[str], *, limit: int) -> tuple[str, ...]:
    terms: set[str] = set()
    for text in texts:
        for match in _TOKEN_PATTERN.finditer(text.casefold()):
            term = match.group()
            if len(term) > MAX_TERM_LENGTH or term in _STOPWORDS:
                continue
            if len(term) < 3 and term not in _SHORT_TERMS:
                continue
            terms.add(term)
            if len(terms) == limit:
                return tuple(sorted(terms))
    return tuple(sorted(terms))


def _query_digest(
    *,
    snapshot_id: str,
    content_sha256: str,
    index_sha256: str,
    policy_version: str,
    query_terms: tuple[str, ...],
    selected_terms: tuple[str, ...],
    posting_count: int,
    limit: int,
) -> str:
    payload = json.dumps(
        {
            "snapshot_id": snapshot_id,
            "content_sha256": content_sha256,
            "index_sha256": index_sha256,
            "index_policy_version": policy_version,
            "query_terms": query_terms,
            "selected_terms": selected_terms,
            "posting_count": posting_count,
            "posting_budget": MAX_POSTING_BUDGET,
            "limit": limit,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _verify_artifact_digest(path: Path, manifest: SnapshotManifest) -> None:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
    if (
        byte_count != manifest.artifact_bytes
        or digest.hexdigest() != manifest.artifact_sha256
    ):
        raise CandidateIndexArtifactError(
            "snapshot artifact does not match its pinned manifest"
        )


def _readonly_connection(path: Path) -> sqlite3.Connection:
    encoded = quote(str(path), safe="/")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _read_metadata(path: Path) -> dict[str, str]:
    try:
        with closing(_readonly_connection(path)) as connection:
            return _metadata_from_connection(connection)
    except sqlite3.Error as error:
        raise CandidateIndexIdentityConflict(
            "candidate index metadata is invalid"
        ) from error


def _metadata_from_connection(
    connection: sqlite3.Connection,
) -> dict[str, str]:
    rows = connection.execute("SELECT key, value FROM metadata").fetchall()
    metadata = {str(key): str(value) for key, value in rows}
    if set(metadata) != _REQUIRED_METADATA or len(rows) != len(metadata):
        raise CandidateIndexIdentityConflict(
            "candidate index metadata is incomplete"
        )
    return metadata


def _require_metadata_identity(
    metadata: dict[str, str],
    *,
    snapshot_id: str,
    content_sha256: str,
    artifact_sha256: str,
    policy_version: str,
) -> None:
    expected = {
        "schema_version": str(INDEX_SCHEMA_VERSION),
        "snapshot_id": snapshot_id,
        "content_sha256": content_sha256,
        "artifact_sha256": artifact_sha256,
        "index_policy_version": policy_version,
        "max_posting_budget": str(MAX_POSTING_BUDGET),
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise CandidateIndexIdentityConflict(
            "candidate index identity does not match pinned snapshot"
        )
    for key in ("row_count", "open_market_count", "indexed_term_count"):
        try:
            value = int(metadata[key])
        except (KeyError, ValueError) as error:
            raise CandidateIndexIdentityConflict(
                "candidate index counts are invalid"
            ) from error
        if value < 0 or (key == "row_count" and value < 1):
            raise CandidateIndexIdentityConflict(
                "candidate index counts are invalid"
            )
    if int(metadata["open_market_count"]) > int(metadata["row_count"]):
        raise CandidateIndexIdentityConflict(
            "candidate index counts are inconsistent"
        )


def _build_result(
    index: SqliteCandidateIndex,
    metadata: dict[str, str],
    *,
    reused: bool,
) -> CandidateIndexBuildResult:
    return CandidateIndexBuildResult(
        index_path=index.index_path,
        snapshot_id=index.snapshot_id,
        content_sha256=index.content_sha256,
        artifact_sha256=index.artifact_sha256,
        index_policy_version=index.index_policy_version,
        row_count=int(metadata["row_count"]),
        open_market_count=int(metadata["open_market_count"]),
        indexed_term_count=int(metadata["indexed_term_count"]),
        index_sha256=index.index_sha256,
        reused=reused,
    )


def _select_query_terms(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    *,
    open_market_count: int,
) -> tuple[tuple[str, ...], int, bool]:
    placeholders = ",".join("?" for _ in terms)
    rows = connection.execute(
        "SELECT term, document_count FROM term_stats "
        f"WHERE term IN ({placeholders})",
        terms,
    ).fetchall()
    frequencies: list[tuple[str, int]] = []
    for term, raw_count in rows:
        count = int(raw_count)
        if count < 1 or count > open_market_count:
            raise CandidateIndexIdentityConflict(
                "candidate index term statistics are invalid"
            )
        frequencies.append((str(term), count))
    frequencies.sort(key=lambda item: (item[1], item[0]))

    selected: list[str] = []
    posting_count = 0
    for term, count in frequencies:
        if posting_count + count > MAX_POSTING_BUDGET:
            break
        selected.append(term)
        posting_count += count
    exhausted = len(selected) < len(frequencies)
    return tuple(selected), posting_count, exhausted


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_fingerprint(path: Path) -> tuple[int, int, int, int]:
    try:
        stat = path.stat()
    except OSError as error:
        raise CandidateIndexIdentityConflict(
            "candidate index disappeared after identity verification"
        ) from error
    # Do not include ctime: atomic no-clobber publication hard-links the fully
    # fsynced temporary inode and then removes the private name. That harmless
    # link-count change updates ctime while a concurrent loser verifies the
    # winner. Device/inode catch replacement; size/mtime catch ordinary writes,
    # while the required SHA-256 remains the semantic identity authority.
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _refuse_governed_target(path: Path) -> None:
    normalized = path.as_posix().rstrip("/")
    forbidden = (
        "/data/review",
        "/data/clean_goldens",
        "/data/eval_sets",
        "/data/review_batches",
        "/docs/archive",
    )
    if any(
        normalized == suffix
        or normalized.startswith(f"{suffix}/")
        or suffix + "/" in normalized + "/"
        for suffix in forbidden
    ):
        raise CandidateIndexArtifactError(
            "candidate indexes cannot be written under governed data roots"
        )
