"""Offline-testable market-universe refresh worker boundary.

The live PolyData source is intentionally not implemented here. It must stage
the normalized artifact without ``frame.to_dicts()`` before it can satisfy the
``SnapshotSource`` protocol.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.tables import ActiveMarketUniverse, Job, MarketUniverseSnapshot
from el.jobs import (
    FailureKind,
    FencedWriteSession,
    JobClaim,
    JobStatus,
    JobStore,
)
from el.retrieval.snapshot_contracts import (
    SnapshotKey,
    SnapshotManifest,
    SnapshotPromotionResult,
    SnapshotRefreshPayload,
    SnapshotValidationPolicy,
    StagedSnapshot,
)
from el.retrieval.snapshot_store import (
    SnapshotArtifactConflict,
    SnapshotArtifactError,
    SnapshotArtifactStore,
)
from el.retrieval.snapshot_validation import (
    SnapshotValidationError,
    build_manifest,
    validate_staged_snapshot,
)

SNAPSHOT_REFRESH_JOB_TYPE = "market_universe_refresh"


class SnapshotRestatementRequiresOperator(RuntimeError):
    def __init__(
        self,
        message: str,
        manifest: SnapshotManifest,
        active_snapshot_id: str | None = None,
        *,
        code: str = "snapshot_registry_identity_conflict",
        safe_message: str = "stored snapshot conflicts with content identity",
    ):
        super().__init__(message)
        self.manifest = manifest
        self.active_snapshot_id = active_snapshot_id
        self.code = code
        self.safe_message = safe_message


class SnapshotLeaseLost(RuntimeError):
    pass


class SnapshotSourceFailure(RuntimeError):
    def __init__(self, code: str, safe_message: str):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class TransientSnapshotSourceFailure(SnapshotSourceFailure):
    pass


class PermanentSnapshotSourceFailure(SnapshotSourceFailure):
    pass


class SnapshotSource(Protocol):
    def stage(
        self,
        payload: SnapshotRefreshPayload,
        destination: Path,
        *,
        deadline_utc: datetime | None,
        cancellation_requested: Callable[[], bool],
        max_silence_seconds: float,
        progress: Callable[[], None],
    ) -> StagedSnapshot: ...


class SnapshotWorkerResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claimed: bool
    job_id: str | None = None
    attempt_id: str | None = None
    status: str
    snapshot_id: str | None = None
    active_snapshot_id: str | None = None
    disposition: Literal[
        "promoted",
        "already_active",
        "superseded",
        "needs_operator",
    ] | None = None


class SnapshotRegistry:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._sessions = session_factory

    def active(self, key: SnapshotKey) -> MarketUniverseSnapshot | None:
        with self._sessions() as session:
            pointer = session.get(
                ActiveMarketUniverse,
                {"provider": key.provider, "venue": key.venue},
            )
            if pointer is None:
                return None
            snapshot = session.get(MarketUniverseSnapshot, pointer.snapshot_id)
            if snapshot is not None:
                session.expunge(snapshot)
            return snapshot

    @staticmethod
    def promote_in_session(
        session: Session,
        job: Job,
        manifest: SnapshotManifest,
        *,
        now: datetime,
    ) -> SnapshotPromotionResult:
        manifest_json = manifest.model_dump(mode="json")
        snapshot = session.get(MarketUniverseSnapshot, manifest.snapshot_id)
        if snapshot is None:
            snapshot = MarketUniverseSnapshot(
                id=manifest.snapshot_id,
                provider=manifest.provider,
                venue=manifest.venue,
                cutoff_utc=manifest.cutoff_utc,
                content_sha256=manifest.content_sha256,
                membership_sha256=manifest.membership_sha256,
                artifact_uri=manifest.artifact_uri,
                artifact_format=manifest.artifact_format,
                artifact_sha256=manifest.artifact_sha256,
                artifact_bytes=manifest.artifact_bytes,
                row_count=manifest.row_count,
                unique_market_count=manifest.unique_market_count,
                open_market_count=manifest.open_market_count,
                normalization_policy_version=(
                    manifest.normalization_policy_version
                ),
                validation_policy_version=manifest.validation_policy_version,
                source_versions=manifest.source_versions,
                manifest=manifest_json,
                created_by_job_id=job.id,
                created_at=now,
            )
            session.add(snapshot)
            session.flush()
        elif not _snapshot_matches_manifest(snapshot, manifest):
            raise SnapshotRestatementRequiresOperator(
                "content-addressed snapshot manifest mismatch",
                manifest,
            )

        pointer = session.scalar(
            select(ActiveMarketUniverse)
            .where(
                ActiveMarketUniverse.provider == manifest.provider,
                ActiveMarketUniverse.venue == manifest.venue,
            )
            .with_for_update()
        )
        if pointer is None:
            pointer = ActiveMarketUniverse(
                provider=manifest.provider,
                venue=manifest.venue,
                snapshot_id=manifest.snapshot_id,
                generation=1,
                promoted_by_job_id=job.id,
                promoted_at=now,
            )
            session.add(pointer)
            return SnapshotPromotionResult(
                snapshot_id=manifest.snapshot_id,
                active_snapshot_id=manifest.snapshot_id,
                generation=1,
                disposition="promoted",
            )

        active = session.get(MarketUniverseSnapshot, pointer.snapshot_id)
        if active is None:
            raise RuntimeError("active market-universe pointer is broken")
        if pointer.snapshot_id == manifest.snapshot_id:
            return SnapshotPromotionResult(
                snapshot_id=manifest.snapshot_id,
                active_snapshot_id=pointer.snapshot_id,
                generation=pointer.generation,
                disposition="already_active",
            )

        active_cutoff = _aware(active.cutoff_utc)
        candidate_cutoff = _aware(manifest.cutoff_utc)
        if candidate_cutoff < active_cutoff:
            return SnapshotPromotionResult(
                snapshot_id=manifest.snapshot_id,
                active_snapshot_id=pointer.snapshot_id,
                generation=pointer.generation,
                disposition="superseded",
            )
        if candidate_cutoff == active_cutoff:
            if active.content_sha256 == manifest.content_sha256:
                return SnapshotPromotionResult(
                    snapshot_id=manifest.snapshot_id,
                    active_snapshot_id=pointer.snapshot_id,
                    generation=pointer.generation,
                    disposition="already_active",
                )
            raise SnapshotRestatementRequiresOperator(
                "same cutoff produced different universe content",
                manifest,
                pointer.snapshot_id,
                code="same_cutoff_content_changed",
                safe_message="same cutoff produced different snapshot content",
            )

        pointer.snapshot_id = manifest.snapshot_id
        pointer.generation += 1
        pointer.promoted_by_job_id = job.id
        pointer.promoted_at = now
        return SnapshotPromotionResult(
            snapshot_id=manifest.snapshot_id,
            active_snapshot_id=manifest.snapshot_id,
            generation=pointer.generation,
            disposition="promoted",
        )


class SnapshotRefreshWorker:
    def __init__(
        self,
        *,
        jobs: JobStore,
        registry: SnapshotRegistry,
        source: SnapshotSource,
        artifacts: SnapshotArtifactStore,
        validation_policy: SnapshotValidationPolicy,
        worker_id: str,
        lease_seconds: int = 600,
    ):
        self._jobs = jobs
        self._registry = registry
        self._source = source
        self._artifacts = artifacts
        self._policy = validation_policy
        self._worker_id = worker_id
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._lease_seconds = lease_seconds

    def run_once(self, *, now: datetime | None = None) -> SnapshotWorkerResult:
        fixed_now = _aware(now) if now else None
        claim = self._jobs.claim_due(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            job_types=[SNAPSHOT_REFRESH_JOB_TYPE],
            now=fixed_now,
        )
        if claim is None:
            return SnapshotWorkerResult(claimed=False, status="idle")

        try:
            payload = SnapshotRefreshPayload.model_validate(claim.payload)
            self._validate_pins(payload, claim)
            with tempfile.TemporaryDirectory(prefix="fitcheck-snapshot-") as raw:
                destination = Path(raw) / "universe.jsonl"
                self._heartbeat_or_raise(claim, stage="staging", now=fixed_now)
                staged = self._source.stage(
                    payload,
                    destination,
                    deadline_utc=claim.deadline_at,
                    cancellation_requested=lambda: (
                        self._jobs.get(claim.job_id).status
                        == JobStatus.CANCELLED.value
                    ),
                    max_silence_seconds=self._lease_seconds / 3,
                    progress=lambda: self._heartbeat_or_raise(
                        claim, stage="staging", now=fixed_now
                    ),
                )
                self._validate_source_echo(payload, staged)
                self._heartbeat_or_raise(claim, stage="validating", now=fixed_now)
                report = validate_staged_snapshot(
                    staged,
                    self._policy,
                    progress=lambda: self._heartbeat_or_raise(
                        claim, stage="validating", now=fixed_now
                    ),
                )
                if not report.passed:
                    raise SnapshotValidationError(report)
                self._heartbeat_or_raise(claim, stage="publishing", now=fixed_now)
                manifest = build_manifest(
                    staged,
                    report,
                    self._policy,
                    artifact_uri=self._artifacts.artifact_uri(
                        _manifest_snapshot_id(staged, report)
                    ),
                    generated_at=fixed_now or datetime.now(timezone.utc),
                )
                published = self._artifacts.publish(
                    staged,
                    manifest,
                    progress=lambda: self._heartbeat_or_raise(
                        claim, stage="publishing", now=fixed_now
                    ),
                )
                manifest = published.manifest
                self._artifacts.verify(
                    manifest,
                    progress=lambda: self._heartbeat_or_raise(
                        claim, stage="verifying", now=fixed_now
                    ),
                )
                self._heartbeat_or_raise(claim, stage="promoting", now=fixed_now)
                promotion: SnapshotPromotionResult | None = None

                def commit_action(session: FencedWriteSession, job: Job) -> dict:
                    nonlocal promotion
                    promotion = self._registry.promote_in_session(
                        session,
                        job,
                        manifest,
                        now=fixed_now or datetime.now(timezone.utc),
                    )
                    return {
                        **promotion.model_dump(mode="json"),
                        "validation_policy_version": self._policy.version,
                        "validation_report": report.model_dump(mode="json"),
                        "canonical_source_versions": manifest.source_versions,
                        "attempt_source_versions": staged.source_versions,
                    }

                if not self._jobs.succeed_with(
                    claim, commit_action, now=fixed_now
                ):
                    return self._stale(claim)
                assert promotion is not None
                return SnapshotWorkerResult(
                    claimed=True,
                    job_id=str(claim.job_id),
                    attempt_id=str(claim.attempt_id),
                    status="succeeded",
                    snapshot_id=promotion.snapshot_id,
                    active_snapshot_id=promotion.active_snapshot_id,
                    disposition=promotion.disposition,
                )
        except SnapshotLeaseLost:
            return self._stale(claim)
        except ValidationError:
            return self._record_failure(
                claim,
                kind=FailureKind.PERMANENT,
                code="snapshot_contract_invalid",
                safe_message="snapshot job or source contract is invalid",
                now=fixed_now,
            )
        except SnapshotValidationError as error:
            return self._record_failure(
                claim,
                kind=FailureKind.PERMANENT,
                code="snapshot_validation_failed",
                safe_message="snapshot validation failed",
                details=error.report.model_dump(mode="json"),
                now=fixed_now,
            )
        except SnapshotRestatementRequiresOperator as error:
            failed = self._record_failure(
                claim,
                kind=FailureKind.NEEDS_OPERATOR,
                code=error.code,
                safe_message=error.safe_message,
                details={
                    "candidate_snapshot_id": error.manifest.snapshot_id,
                    "candidate_artifact_uri": error.manifest.artifact_uri,
                    "candidate_artifact_sha256": error.manifest.artifact_sha256,
                    "candidate_content_sha256": error.manifest.content_sha256,
                    "candidate_membership_sha256": error.manifest.membership_sha256,
                    "candidate_cutoff_utc": error.manifest.cutoff_utc.isoformat(),
                    "active_snapshot_id": error.active_snapshot_id,
                },
                now=fixed_now,
            )
            if failed.status == "stale_attempt":
                return failed
            return failed.model_copy(
                update={
                    "snapshot_id": error.manifest.snapshot_id,
                    "active_snapshot_id": error.active_snapshot_id,
                    "disposition": "needs_operator",
                }
            )
        except PermanentSnapshotSourceFailure as error:
            return self._record_failure(
                claim,
                kind=FailureKind.PERMANENT,
                code=error.code,
                safe_message=error.safe_message,
                now=fixed_now,
            )
        except TransientSnapshotSourceFailure as error:
            return self._record_failure(
                claim,
                kind=FailureKind.TRANSIENT,
                code=error.code,
                safe_message=error.safe_message,
                now=fixed_now,
            )
        except SnapshotArtifactConflict:
            return self._record_failure(
                claim,
                kind=FailureKind.NEEDS_OPERATOR,
                code="snapshot_artifact_integrity_conflict",
                safe_message="snapshot artifact conflicts with stored data",
                now=fixed_now,
            )
        except SnapshotArtifactError:
            return self._record_failure(
                claim,
                kind=FailureKind.TRANSIENT,
                code="snapshot_artifact_error",
                safe_message="snapshot artifact publication failed",
                now=fixed_now,
            )
        except Exception:
            # Never persist or return raw exception text: provider errors may
            # contain signed URLs, credentials, or internal implementation.
            return self._record_failure(
                claim,
                kind=FailureKind.TRANSIENT,
                code="snapshot_worker_internal_error",
                safe_message="snapshot worker failed",
                now=fixed_now,
            )

    def _validate_pins(
        self, payload: SnapshotRefreshPayload, claim: JobClaim
    ) -> None:
        if payload.validation_policy_version != self._policy.version:
            raise PermanentSnapshotSourceFailure(
                "unsupported_validation_policy",
                "snapshot validation policy is unavailable",
            )
        pinned_normalization = claim.pinned_manifest.get(
            "normalization_policy_version"
        )
        if pinned_normalization != payload.normalization_policy_version:
            raise PermanentSnapshotSourceFailure(
                "normalization_policy_not_pinned",
                "snapshot normalization policy is not pinned",
            )

    @staticmethod
    def _validate_source_echo(
        payload: SnapshotRefreshPayload, staged: StagedSnapshot
    ) -> None:
        if (
            staged.key.provider != payload.provider
            or staged.key.venue != payload.venue
            or staged.cutoff_utc != payload.cutoff_utc
            or staged.normalization_policy_version
            != payload.normalization_policy_version
        ):
            raise PermanentSnapshotSourceFailure(
                "snapshot_source_identity_mismatch",
                "snapshot source returned different pinned identity",
            )

    @staticmethod
    def _stale(claim: JobClaim) -> SnapshotWorkerResult:
        return SnapshotWorkerResult(
            claimed=True,
            job_id=str(claim.job_id),
            attempt_id=str(claim.attempt_id),
            status="stale_attempt",
        )

    def _failed(self, claim: JobClaim) -> SnapshotWorkerResult:
        status = self._jobs.get(claim.job_id).status
        return SnapshotWorkerResult(
            claimed=True,
            job_id=str(claim.job_id),
            attempt_id=str(claim.attempt_id),
            status=status,
        )

    def _heartbeat_or_raise(
        self,
        claim: JobClaim,
        *,
        stage: str,
        now: datetime | None,
    ) -> None:
        if not self._jobs.heartbeat(
            claim,
            lease_seconds=self._lease_seconds,
            stage=stage,
            now=now,
        ):
            raise SnapshotLeaseLost

    def _record_failure(
        self,
        claim: JobClaim,
        *,
        kind: FailureKind,
        code: str,
        safe_message: str,
        now: datetime | None,
        details: dict | None = None,
    ) -> SnapshotWorkerResult:
        if not self._jobs.fail(
            claim,
            kind=kind,
            code=code,
            safe_message=safe_message,
            details=details,
            now=now,
        ):
            return self._stale(claim)
        return self._failed(claim)


def _manifest_snapshot_id(staged, report) -> str:
    # ``build_manifest`` is the authority. This small call avoids making the
    # store derive identity from mutable paths.
    from el.retrieval.snapshot_validation import universe_snapshot_id

    return universe_snapshot_id(staged, report.content_sha256)


def _aware(value: datetime) -> datetime:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc)


def _snapshot_matches_manifest(
    snapshot: MarketUniverseSnapshot, manifest: SnapshotManifest
) -> bool:
    return (
        snapshot.provider == manifest.provider
        and snapshot.venue == manifest.venue
        and _aware(snapshot.cutoff_utc) == manifest.cutoff_utc
        and snapshot.content_sha256 == manifest.content_sha256
        and snapshot.membership_sha256 == manifest.membership_sha256
        and snapshot.artifact_uri == manifest.artifact_uri
        and snapshot.artifact_format == manifest.artifact_format
        and snapshot.artifact_sha256 == manifest.artifact_sha256
        and snapshot.artifact_bytes == manifest.artifact_bytes
        and snapshot.row_count == manifest.row_count
        and snapshot.unique_market_count == manifest.unique_market_count
        and snapshot.open_market_count == manifest.open_market_count
        and snapshot.normalization_policy_version
        == manifest.normalization_policy_version
        and snapshot.validation_policy_version
        == manifest.validation_policy_version
        and snapshot.source_versions == manifest.source_versions
        and snapshot.manifest == manifest.model_dump(mode="json")
    )
