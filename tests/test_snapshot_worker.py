"""Snapshot validation, artifact publication, and fenced promotion."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from el.domain.tables import (
    ActiveMarketUniverse,
    Base,
    MarketUniverseSnapshot,
)
from el.jobs import JobStatus, JobStore
from el.retrieval.snapshot_contracts import (
    SnapshotKey,
    SnapshotRefreshPayload,
    SnapshotValidationPolicy,
    StagedSnapshot,
)
from el.retrieval.snapshot_store import (
    LocalSnapshotArtifactStore,
    SnapshotArtifactConflict,
    SnapshotArtifactError,
)
from el.retrieval.snapshot_validation import (
    build_manifest,
    stage_fixture_jsonl_rows,
    validate_staged_snapshot,
)
from el.retrieval.snapshot_worker import (
    SNAPSHOT_REFRESH_JOB_TYPE,
    SnapshotRefreshWorker,
    SnapshotRegistry,
    TransientSnapshotSourceFailure,
)

CUTOFF = datetime(2026, 7, 28, 22, 0, tzinfo=timezone.utc)


def test_snapshot_worker_boundary_has_no_live_polydata_implementation():
    root = Path(__file__).resolve().parents[1]
    sources = "\n".join(
        (root / "el" / "retrieval" / name).read_text(encoding="utf-8")
        for name in (
            "snapshot_contracts.py",
            "snapshot_validation.py",
            "snapshot_store.py",
            "snapshot_worker.py",
        )
    )
    assert "from poly_data_client import" not in sources
    assert ".markets_pit(" not in sources
    assert ".market_universe(" not in sources


def _row(market_id: str, **overrides):
    base = {
        "market_id": market_id,
        "title": f"Market {market_id}",
        "slug": f"market-{market_id}",
        "description": "metadata",
        "resolution_rules": f"Resolves from rule {market_id}.",
        "outcomes": ["Yes", "No"],
        "token_ids": [f"{market_id}-yes", f"{market_id}-no"],
        "close_date": "2026-12-31",
        "closed_time": None,
        "snapshot_ts": CUTOFF.isoformat(),
        "is_open": True,
        "volume_usd": 1000.0,
        "taxonomy_l1": "technology",
        "taxonomy_confidence": 0.9,
        "tags": [],
        "source_url": f"https://example.test/{market_id}",
    }
    base.update(overrides)
    return base


def _rows(**direct_overrides):
    return [
        _row(
            "687559",
            title="Sam Altman out as OpenAI CEO before 2027?",
            slug="sam-altman-out-as-openai-ceo-before-2027",
            **direct_overrides,
        ),
        _row("transport-2"),
        _row("unrelated-3"),
    ]


def _policy(version="polydata-sentinel-v1"):
    return SnapshotValidationPolicy(
        version=version,
        minimum_row_count=3,
        required_sentinel_ids=("687559", "transport-2"),
        direct_sentinel_id="687559",
        direct_expected_title="Sam Altman out as OpenAI CEO before 2027?",
        direct_expected_slug="sam-altman-out-as-openai-ceo-before-2027",
    )


def _sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot.db'}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class _FixtureSource:
    def __init__(self, rows, *, failures=None, source_versions=None):
        self.rows = rows
        self.failures = list(failures or [])
        self.source_versions = source_versions or {"fixture": "v1"}
        self.stage_contract = None

    def stage(
        self,
        payload: SnapshotRefreshPayload,
        destination,
        *,
        deadline_utc,
        cancellation_requested,
        max_silence_seconds,
        progress,
    ):
        self.stage_contract = {
            "deadline_utc": deadline_utc,
            "cancellation_requested": cancellation_requested,
            "max_silence_seconds": max_silence_seconds,
        }
        progress()
        if self.failures:
            raise self.failures.pop(0)
        stage_fixture_jsonl_rows(self.rows, destination)
        progress()
        return StagedSnapshot(
            path=destination,
            key=SnapshotKey(provider=payload.provider, venue=payload.venue),
            cutoff_utc=payload.cutoff_utc,
            normalization_policy_version=payload.normalization_policy_version,
            source_versions=self.source_versions,
        )


def _submit(
    jobs: JobStore,
    *,
    cutoff=CUTOFF,
    key="refresh-1",
    max_attempts=3,
    policy=None,
    deadline_at=None,
):
    policy = policy or _policy()
    return jobs.submit_or_get(
        job_type=SNAPSHOT_REFRESH_JOB_TYPE,
        owner_client_type="system",
        owner_actor_id="snapshot-scheduler",
        idempotency_key=key,
        payload={
            "provider": "polydata",
            "venue": "polymarket",
            "cutoff_utc": cutoff.isoformat(),
            "validation_policy_version": policy.version,
            "normalization_policy_version": "market-normalization-v1",
        },
        pinned_manifest={
            "normalization_policy_version": "market-normalization-v1"
        },
        max_attempts=max_attempts,
        deadline_at=deadline_at,
        now=cutoff,
    )


def _worker(
    tmp_path,
    sessions,
    jobs,
    source,
    *,
    policy=None,
    artifacts=None,
    lease_seconds=600,
):
    return SnapshotRefreshWorker(
        jobs=jobs,
        registry=SnapshotRegistry(sessions),
        source=source,
        artifacts=artifacts or LocalSnapshotArtifactStore(tmp_path / "artifacts"),
        validation_policy=policy or _policy(),
        worker_id="snapshot-worker-1",
        lease_seconds=lease_seconds,
    )


def _staged(tmp_path, rows):
    path = stage_fixture_jsonl_rows(rows, tmp_path / "staged.jsonl")
    return StagedSnapshot(
        path=path,
        key=SnapshotKey(provider="polydata", venue="polymarket"),
        cutoff_utc=CUTOFF,
        normalization_policy_version="market-normalization-v1",
        source_versions={"fixture": "v1"},
    )


def test_validation_is_order_independent_after_canonical_staging_and_rules_sensitive(
    tmp_path,
):
    first = _staged(tmp_path / "a", _rows())
    reversed_stage = _staged(tmp_path / "b", list(reversed(_rows())))
    changed = _staged(
        tmp_path / "c",
        [
            _row(
                "687559",
                title="Sam Altman out as OpenAI CEO before 2027?",
                slug="sam-altman-out-as-openai-ceo-before-2027",
                resolution_rules="One character changed!",
            ),
            _row("transport-2"),
            _row("unrelated-3"),
        ],
    )

    first_report = validate_staged_snapshot(first, _policy())
    reversed_report = validate_staged_snapshot(reversed_stage, _policy())
    changed_report = validate_staged_snapshot(changed, _policy())

    assert first_report.passed
    assert reversed_report.passed
    assert first_report.content_sha256 == reversed_report.content_sha256
    assert first_report.membership_sha256 == reversed_report.membership_sha256
    assert changed_report.passed
    assert changed_report.membership_sha256 == first_report.membership_sha256
    assert changed_report.content_sha256 != first_report.content_sha256


def test_content_identity_normalizes_utc_offsets_and_unordered_tags(tmp_path):
    canonical_rows = _rows()
    canonical_rows[2]["tags"] = ["alpha", "zeta"]
    equivalent_rows = _rows()
    for row in equivalent_rows:
        row["snapshot_ts"] = "2026-07-28T23:00:00+01:00"
    equivalent_rows[2]["tags"] = ["zeta", "alpha", "zeta"]

    canonical = validate_staged_snapshot(
        _staged(tmp_path / "canonical", canonical_rows), _policy()
    )
    equivalent = validate_staged_snapshot(
        _staged(tmp_path / "equivalent", equivalent_rows), _policy()
    )

    assert canonical.passed and equivalent.passed
    assert canonical.content_sha256 == equivalent.content_sha256
    assert canonical.membership_sha256 == equivalent.membership_sha256


def test_validation_rejects_noncanonical_jsonl_and_partial_direct_match(tmp_path):
    rows = _rows()
    rows[0]["title"] = "Sam Altman remains relevant but this is another market"
    rows[0]["slug"] = "another-openai-ceo-market"
    staged = _staged(tmp_path, rows)
    staged.path.write_text(
        " " + staged.path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    report = validate_staged_snapshot(staged, _policy())

    assert not report.passed
    assert "artifact_not_canonical_jsonl" in report.errors
    assert "direct_sentinel_mismatch" in report.errors


def test_validation_fails_closed_with_bounded_diagnostics(tmp_path):
    rows = _rows()
    rows[0]["title"] = "Unrelated title"
    rows[0]["slug"] = "unrelated-slug"
    rows[0]["outcomes"] = ["No", "Yes"]
    rows[1]["is_open"] = False
    rows[1]["closed_time"] = CUTOFF.isoformat()
    staged = _staged(tmp_path, rows)

    report = validate_staged_snapshot(staged, _policy())

    assert not report.passed
    assert report.closed_sentinel_ids == ("transport-2",)
    assert report.nonbinary_sentinel_ids == ("687559",)
    assert not report.direct_sentinel_match
    assert "required_sentinel_closed" in report.errors
    assert "required_sentinel_nonbinary" in report.errors
    assert "direct_sentinel_mismatch" in report.errors


def test_snapshot_contract_rejects_blank_market_identity_and_versions(tmp_path):
    with pytest.raises(ValidationError, match="market ID and title"):
        _staged(tmp_path / "blank-market", [_row("   ", title="   ")])

    with pytest.raises(ValidationError, match="version must be nonblank"):
        SnapshotValidationPolicy(version="   ")


def test_valid_snapshot_publishes_promotes_and_completes_atomically(tmp_path):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    submitted = _submit(jobs)
    worker = _worker(tmp_path, sessions, jobs, _FixtureSource(_rows()))

    result = worker.run_once(now=CUTOFF)

    assert result.status == "succeeded"
    assert result.snapshot_id
    assert result.active_snapshot_id == result.snapshot_id
    assert result.disposition == "promoted"
    job = jobs.get(submitted.job_id)
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.result["snapshot_id"] == result.snapshot_id
    assert job.result["disposition"] == "promoted"
    with sessions() as session:
        snapshot = session.get(MarketUniverseSnapshot, result.snapshot_id)
        pointer = session.get(
            ActiveMarketUniverse,
            {"provider": "polydata", "venue": "polymarket"},
        )
        assert snapshot is not None
        assert pointer.snapshot_id == snapshot.id
        assert pointer.generation == 1
        assert session.scalar(
            select(func.count()).select_from(MarketUniverseSnapshot)
        ) == 1


def test_snapshot_source_receives_deadline_cancellation_and_silence_contract(
    tmp_path,
):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    deadline = CUTOFF + timedelta(hours=1)
    _submit(jobs, deadline_at=deadline)
    source = _FixtureSource(_rows())
    worker = _worker(
        tmp_path,
        sessions,
        jobs,
        source,
        lease_seconds=90,
    )

    result = worker.run_once(now=CUTOFF)

    assert result.status == "succeeded"
    assert source.stage_contract["deadline_utc"] == deadline
    assert source.stage_contract["max_silence_seconds"] == 30
    assert source.stage_contract["cancellation_requested"]() is False


def test_invalid_refresh_preserves_previous_active_snapshot(tmp_path):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    first = _submit(jobs, key="first")
    valid_worker = _worker(tmp_path, sessions, jobs, _FixtureSource(_rows()))
    valid = valid_worker.run_once(now=CUTOFF)
    assert jobs.get(first.job_id).status == JobStatus.SUCCEEDED.value

    later = CUTOFF + timedelta(hours=1)
    _submit(jobs, cutoff=later, key="invalid-later")
    bad_rows = [_row("687559", snapshot_ts=later.isoformat())]
    bad_worker = _worker(tmp_path, sessions, jobs, _FixtureSource(bad_rows))
    failed = bad_worker.run_once(now=later)

    assert failed.status == "failed"
    with sessions() as session:
        pointer = session.get(
            ActiveMarketUniverse,
            {"provider": "polydata", "venue": "polymarket"},
        )
        assert pointer.snapshot_id == valid.snapshot_id
        assert session.scalar(
            select(func.count()).select_from(MarketUniverseSnapshot)
        ) == 1


def test_same_cutoff_changed_content_requires_operator(tmp_path):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    _submit(jobs, key="first")
    first = _worker(tmp_path, sessions, jobs, _FixtureSource(_rows())).run_once(
        now=CUTOFF
    )

    second_job = _submit(jobs, key="restated")
    changed = _rows()
    changed[2]["resolution_rules"] = "Provider restated these rules."
    second = _worker(
        tmp_path, sessions, jobs, _FixtureSource(changed)
    ).run_once(now=CUTOFF)

    assert second.status == "needs_operator"
    second_row = jobs.get(second_job.job_id)
    assert second_row.status == JobStatus.NEEDS_OPERATOR.value
    assert second_row.error_code == "same_cutoff_content_changed"
    assert second_row.error_details["candidate_snapshot_id"] == second.snapshot_id
    assert second_row.error_details["candidate_artifact_uri"].endswith(
        "/universe.jsonl"
    )
    assert second_row.error_details["candidate_artifact_sha256"]
    assert second_row.error_details["candidate_content_sha256"]
    assert second_row.error_details["active_snapshot_id"] == first.snapshot_id
    with sessions() as session:
        pointer = session.get(
            ActiveMarketUniverse,
            {"provider": "polydata", "venue": "polymarket"},
        )
        assert pointer.snapshot_id == first.snapshot_id


def test_same_content_revalidated_under_new_policy_is_already_active(tmp_path):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    policy_v1 = _policy("polydata-sentinel-v1")
    policy_v2 = _policy("polydata-sentinel-v2")
    _submit(jobs, key="v1", policy=policy_v1)
    first = _worker(
        tmp_path,
        sessions,
        jobs,
        _FixtureSource(_rows()),
        policy=policy_v1,
    ).run_once(now=CUTOFF)
    second_job = _submit(jobs, key="v2", policy=policy_v2)

    second = _worker(
        tmp_path,
        sessions,
        jobs,
        _FixtureSource(_rows()),
        policy=policy_v2,
    ).run_once(now=CUTOFF + timedelta(seconds=1))

    assert second.status == "succeeded"
    assert second.disposition == "already_active"
    assert second.snapshot_id == first.snapshot_id
    assert second.active_snapshot_id == first.snapshot_id
    second_job_row = jobs.get(second_job.job_id)
    assert second_job_row.result["validation_policy_version"] == policy_v2.version
    assert second_job_row.result["validation_report"]["passed"] is True


def test_existing_artifact_corruption_requires_operator(tmp_path):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    artifacts = LocalSnapshotArtifactStore(tmp_path / "artifacts")
    _submit(jobs, key="first")
    first = _worker(
        tmp_path,
        sessions,
        jobs,
        _FixtureSource(_rows()),
        artifacts=artifacts,
    ).run_once(now=CUTOFF)
    artifact_path = Path(
        artifacts.artifact_uri(first.snapshot_id).removeprefix("file://")
    )
    with artifact_path.open("a", encoding="utf-8") as handle:
        handle.write("corrupt\n")

    second_job = _submit(jobs, key="corrupt-rerun")
    result = _worker(
        tmp_path,
        sessions,
        jobs,
        _FixtureSource(_rows()),
        artifacts=artifacts,
    ).run_once(now=CUTOFF + timedelta(seconds=1))

    assert result.status == "needs_operator"
    assert (
        jobs.get(second_job.job_id).error_code
        == "snapshot_artifact_integrity_conflict"
    )


def test_registry_provenance_drift_is_not_misreported_as_cutoff_change(tmp_path):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    _submit(jobs, key="first")
    first = _worker(
        tmp_path, sessions, jobs, _FixtureSource(_rows())
    ).run_once(now=CUTOFF)
    with sessions() as session:
        snapshot = session.get(MarketUniverseSnapshot, first.snapshot_id)
        snapshot.source_versions = {"fixture": "tampered"}
        session.commit()

    second_job = _submit(jobs, key="registry-rerun")
    result = _worker(
        tmp_path, sessions, jobs, _FixtureSource(_rows())
    ).run_once(now=CUTOFF + timedelta(seconds=1))

    assert result.status == "needs_operator"
    assert (
        jobs.get(second_job.job_id).error_code
        == "snapshot_registry_identity_conflict"
    )


def test_validation_progress_callback_runs_during_stream(tmp_path):
    staged = _staged(tmp_path, _rows())
    progress_calls = 0

    def progress():
        nonlocal progress_calls
        progress_calls += 1

    report = validate_staged_snapshot(
        staged,
        _policy(),
        progress=progress,
        progress_every_rows=1,
    )

    assert report.passed
    assert progress_calls >= report.row_count


def test_retry_after_publish_reuses_canonical_manifest_when_time_advances(tmp_path):
    sessions = _sessions(tmp_path)
    durable_jobs = JobStore(sessions)
    submitted = _submit(durable_jobs, max_attempts=2)

    class CrashOnceAfterPublish:
        def __init__(self, delegate):
            self.delegate = delegate
            self.crash = True

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def succeed_with(self, *args, **kwargs):
            if self.crash:
                self.crash = False
                raise RuntimeError("simulated crash after artifact publication")
            return self.delegate.succeed_with(*args, **kwargs)

    jobs = CrashOnceAfterPublish(durable_jobs)
    worker = _worker(tmp_path, sessions, jobs, _FixtureSource(_rows()))

    first = worker.run_once(now=CUTOFF)
    assert first.status == "retry_wait"
    manifest_path = next((tmp_path / "artifacts").glob("mu_*/manifest.json"))
    manifest_before = manifest_path.read_text(encoding="utf-8")

    second = worker.run_once(now=CUTOFF + timedelta(seconds=1))

    assert second.status == "succeeded"
    assert second.disposition == "promoted"
    assert manifest_path.read_text(encoding="utf-8") == manifest_before
    assert durable_jobs.get(submitted.job_id).status == JobStatus.SUCCEEDED.value


def test_content_reuse_distinguishes_canonical_and_attempt_source_versions(
    tmp_path,
):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    _submit(jobs, key="source-v1")
    first = _worker(
        tmp_path,
        sessions,
        jobs,
        _FixtureSource(_rows(), source_versions={"fixture": "v1"}),
    ).run_once(now=CUTOFF)
    second_job = _submit(jobs, key="source-v2")

    second = _worker(
        tmp_path,
        sessions,
        jobs,
        _FixtureSource(_rows(), source_versions={"fixture": "v2"}),
    ).run_once(now=CUTOFF + timedelta(seconds=1))

    assert second.status == "succeeded"
    assert second.snapshot_id == first.snapshot_id
    result = jobs.get(second_job.job_id).result
    assert result["canonical_source_versions"] == {"fixture": "v1"}
    assert result["attempt_source_versions"] == {"fixture": "v2"}
    with sessions() as session:
        snapshot = session.get(MarketUniverseSnapshot, first.snapshot_id)
        assert snapshot.source_versions == {"fixture": "v1"}


def test_stale_attempt_cannot_fail_or_promote_after_artifact_publication(tmp_path):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    submitted = _submit(jobs)
    delegate = LocalSnapshotArtifactStore(tmp_path / "artifacts")

    class ReclaimThenFailStore:
        def __init__(self):
            self.reclaimed = None

        def artifact_uri(self, snapshot_id):
            return delegate.artifact_uri(snapshot_id)

        def verify(self, manifest, *, progress=None):
            return delegate.verify(manifest, progress=progress)

        def publish(self, staged, manifest, *, progress=None):
            delegate.publish(staged, manifest, progress=progress)
            self.reclaimed = jobs.claim_due(
                worker_id="snapshot-worker-2",
                lease_seconds=600,
                job_types=[SNAPSHOT_REFRESH_JOB_TYPE],
                now=CUTOFF + timedelta(seconds=2),
            )
            assert self.reclaimed is not None
            raise SnapshotArtifactError("simulated post-publish failure")

    artifacts = ReclaimThenFailStore()
    result = _worker(
        tmp_path,
        sessions,
        jobs,
        _FixtureSource(_rows()),
        artifacts=artifacts,
        lease_seconds=1,
    ).run_once(now=CUTOFF)

    assert result.status == "stale_attempt"
    assert artifacts.reclaimed.job_id == submitted.job_id
    with sessions() as session:
        assert session.scalar(
            select(func.count()).select_from(MarketUniverseSnapshot)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(ActiveMarketUniverse)
        ) == 0


def test_transient_source_failure_retries_without_leaking_raw_exception(tmp_path):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    submitted = _submit(jobs, max_attempts=2)
    source = _FixtureSource(
        _rows(),
        failures=[
            TransientSnapshotSourceFailure(
                "provider_timeout", "provider timed out"
            )
        ],
    )
    worker = _worker(tmp_path, sessions, jobs, source)

    first = worker.run_once(now=CUTOFF)
    assert first.status == "retry_wait"
    assert jobs.get(submitted.job_id).status == JobStatus.RETRY_WAIT.value

    second = worker.run_once(now=CUTOFF + timedelta(seconds=1))
    assert second.status == "succeeded"
    assert jobs.get(submitted.job_id).attempt_count == 2


def test_unknown_exception_is_sanitized(tmp_path):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    submitted = _submit(jobs, max_attempts=1)

    class SecretFailureSource:
        def stage(
            self,
            _payload,
            _destination,
            *,
            deadline_utc,
            cancellation_requested,
            max_silence_seconds,
            progress,
        ):
            assert deadline_utc is None
            assert not cancellation_requested()
            assert max_silence_seconds > 0
            progress()
            raise RuntimeError("signed_url=secret-value")

    result = _worker(tmp_path, sessions, jobs, SecretFailureSource()).run_once(
        now=CUTOFF
    )
    job = jobs.get(submitted.job_id)

    assert result.status == "failed"
    assert job.status == JobStatus.FAILED.value
    public_projection = json.dumps(
        {
            "error_code": job.error_code,
            "safe_error_message": job.safe_error_message,
            "error_details": job.error_details,
        }
    )
    assert "secret-value" not in public_projection
    assert job.error_code == "retry_budget_exhausted"


def test_malformed_job_payload_is_permanent(tmp_path):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    submitted = jobs.submit_or_get(
        job_type=SNAPSHOT_REFRESH_JOB_TYPE,
        owner_client_type="system",
        owner_actor_id="snapshot-scheduler",
        idempotency_key="malformed-payload",
        payload={
            "provider": "polydata",
            "venue": "polymarket",
            "cutoff_utc": "not-a-timestamp",
            "validation_policy_version": _policy().version,
            "normalization_policy_version": "market-normalization-v1",
        },
        pinned_manifest={
            "normalization_policy_version": "market-normalization-v1"
        },
        max_attempts=3,
        now=CUTOFF,
    )

    result = _worker(
        tmp_path, sessions, jobs, _FixtureSource(_rows())
    ).run_once(now=CUTOFF)
    job = jobs.get(submitted.job_id)

    assert result.status == "failed"
    assert job.attempt_count == 1
    assert job.error_kind == "permanent"
    assert job.error_code == "snapshot_contract_invalid"


def test_invalid_utf8_snapshot_is_permanent_validation_failure(tmp_path):
    sessions = _sessions(tmp_path)
    jobs = JobStore(sessions)
    submitted = _submit(jobs)

    class InvalidUtf8Source:
        def stage(
            self,
            payload,
            destination,
            *,
            deadline_utc,
            cancellation_requested,
            max_silence_seconds,
            progress,
        ):
            assert deadline_utc is None
            assert not cancellation_requested()
            assert max_silence_seconds > 0
            progress()
            destination.write_bytes(b"\xff\n")
            return StagedSnapshot(
                path=destination,
                key=SnapshotKey(provider=payload.provider, venue=payload.venue),
                cutoff_utc=payload.cutoff_utc,
                normalization_policy_version=payload.normalization_policy_version,
                source_versions={"fixture": "invalid-utf8"},
            )

    result = _worker(
        tmp_path, sessions, jobs, InvalidUtf8Source()
    ).run_once(now=CUTOFF)
    job = jobs.get(submitted.job_id)

    assert result.status == "failed"
    assert job.error_kind == "permanent"
    assert job.error_code == "snapshot_validation_failed"
    assert "artifact_not_utf8" in job.error_details["errors"]


def test_local_store_detects_mutation_and_refuses_governed_root(tmp_path):
    staged = _staged(tmp_path / "stage", _rows())
    report = validate_staged_snapshot(staged, _policy())
    store = LocalSnapshotArtifactStore(tmp_path / "artifacts")
    from el.retrieval.snapshot_validation import universe_snapshot_id

    snapshot_id = universe_snapshot_id(staged, report.content_sha256)
    manifest = build_manifest(
        staged,
        report,
        _policy(),
        artifact_uri=store.artifact_uri(snapshot_id),
        generated_at=CUTOFF,
    )
    published = store.publish(staged, manifest)
    store.verify(manifest)

    manifest_path = Path(published.manifest_uri.removeprefix("file://"))
    original_manifest = manifest_path.read_text(encoding="utf-8")
    tampered_manifest = json.loads(original_manifest)
    tampered_manifest["content_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(tampered_manifest), encoding="utf-8")
    with pytest.raises(SnapshotArtifactConflict, match="manifest identity"):
        store.verify(manifest)
    manifest_path.write_text(original_manifest, encoding="utf-8")

    artifact_path = published.artifact_uri.removeprefix("file://")
    with open(artifact_path, "a", encoding="utf-8") as handle:
        handle.write("tampered\n")
    with pytest.raises(SnapshotArtifactError, match="size mismatch"):
        store.verify(manifest)

    with pytest.raises(SnapshotArtifactError, match="governed data"):
        LocalSnapshotArtifactStore(tmp_path / "repo" / "data" / "review")
    with pytest.raises(SnapshotArtifactError, match="governed data"):
        LocalSnapshotArtifactStore(tmp_path / "repo" / "docs" / "archive")
    with pytest.raises(SnapshotArtifactError, match="invalid snapshot ID"):
        store.artifact_uri("../escape")
