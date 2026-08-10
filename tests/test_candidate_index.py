"""Provider-neutral, bounded candidate-index experiment."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Barrier
from urllib.parse import quote

import pytest

from el.domain.structures import (
    ClaimHorizon,
    Entity,
    ExtractedStructure,
    Mechanism,
    Metric,
)
from el.evals.recall import load_golden_cases
from el.retrieval.candidate_index import (
    CANDIDATE_INDEX_POLICY_VERSION,
    MAX_ARTIFACT_ROW_BYTES,
    MAX_POSTING_BUDGET,
    MAX_QUERY_LIMIT,
    MAX_QUERY_TERMS,
    MAX_TERMS_PER_MARKET,
    CandidateIndexArtifactError,
    CandidateIndexIdentityConflict,
    SqliteCandidateIndex,
)
from el.retrieval.gate import evaluate_eligibility
from el.retrieval.provider import CandidateMarketRecord
from el.retrieval.ranking import rank_candidates
from el.retrieval.snapshot_contracts import (
    SnapshotKey,
    SnapshotManifest,
    SnapshotValidationPolicy,
    StagedSnapshot,
)
from el.retrieval.snapshot_validation import (
    build_manifest,
    stage_fixture_jsonl_rows,
    validate_staged_snapshot,
)

CUTOFF = datetime(2026, 7, 28, 22, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "retrieval"


def _row(market_id: str, **overrides):
    values = {
        "market_id": market_id,
        "title": f"Market {market_id}",
        "slug": f"market-{market_id}",
        "description": "Candidate metadata.",
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
    values.update(overrides)
    return values


def _manifest(tmp_path: Path, rows, *, name: str = "snapshot"):
    artifact = stage_fixture_jsonl_rows(
        rows,
        tmp_path / name / "universe.jsonl",
    )
    staged = StagedSnapshot(
        path=artifact,
        key=SnapshotKey(provider="fixture", venue="test"),
        cutoff_utc=CUTOFF,
        normalization_policy_version="market-normalization-v1",
        source_versions={"fixture": "v1"},
    )
    policy = SnapshotValidationPolicy(
        version="candidate-index-fixture-v1",
        minimum_row_count=1,
    )
    report = validate_staged_snapshot(staged, policy)
    assert report.passed, report.model_dump_json(indent=2)
    manifest = build_manifest(
        staged,
        report,
        policy,
        artifact_uri=artifact.resolve().as_uri(),
        generated_at=CUTOFF,
    )
    return artifact, manifest


def _structure(**overrides) -> ExtractedStructure:
    values = {
        "claim_summary": (
            "Gemini ranks #1 on LMSYS Chatbot Arena by the end of 2026."
        ),
        "entities": [
            Entity(name="Google Gemini", role="subject"),
            Entity(name="LMSYS Chatbot Arena", role="venue"),
        ],
        "event_stage": "measured",
        "metric": Metric(
            what="LMSYS Chatbot Arena #1 rank",
            measured_by="LMSYS leaderboard",
            objective=True,
        ),
        "horizon": ClaimHorizon(
            window_end=date(2026, 12, 31),
            precision="day",
        ),
        "mechanism": Mechanism(),
        "stance": "yes",
        "resolution_source_class": "leaderboard",
        "contractible_version": (
            "Will a Google Gemini model rank #1 on LMSYS?"
        ),
    }
    values.update(overrides)
    return ExtractedStructure(**values)


def _open_index(
    path: Path,
    manifest: SnapshotManifest,
    index_sha256: str,
):
    return SqliteCandidateIndex.open(
        path,
        expected_snapshot_id=manifest.snapshot_id,
        expected_content_sha256=manifest.content_sha256,
        expected_artifact_sha256=manifest.artifact_sha256,
        expected_index_sha256=index_sha256,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_build_streams_verified_snapshot_and_reuses_identity(tmp_path):
    artifact, manifest = _manifest(
        tmp_path,
        [
            _row(
                "direct",
                title="Google Gemini ranks first on LMSYS Chatbot Arena",
            ),
            _row("other"),
        ],
    )
    index_path = tmp_path / "indexes" / "candidate.sqlite"
    progress_calls = 0

    def progress():
        nonlocal progress_calls
        progress_calls += 1

    built = SqliteCandidateIndex.build(
        manifest=manifest,
        artifact_path=artifact,
        index_path=index_path,
        progress=progress,
        progress_every_rows=1,
    )
    reused = SqliteCandidateIndex.build(
        manifest=manifest,
        artifact_path=artifact,
        index_path=index_path,
        expected_index_sha256=built.index_sha256,
    )

    assert built.index_path == index_path.resolve()
    assert built.row_count == 2
    assert built.open_market_count == 2
    assert built.indexed_term_count > 0
    assert built.index_policy_version == CANDIDATE_INDEX_POLICY_VERSION
    assert len(built.index_sha256) == 64
    assert not built.reused
    assert reused.reused
    assert reused.index_sha256 == built.index_sha256
    assert progress_calls >= built.row_count
    assert (
        _open_index(index_path, manifest, built.index_sha256).snapshot_id
        == manifest.snapshot_id
    )


def test_query_is_deterministic_hard_bounded_and_read_only(tmp_path):
    rows = [
        _row(
            "target",
            title=(
                "Google Gemini ranks first on LMSYS Chatbot Arena leaderboard"
            ),
            description="Gemini leaderboard contract.",
        )
    ]
    rows.extend(
        _row(
            f"distractor-{number:04d}",
            title=f"Gemini unrelated contract {number}",
        )
        for number in range(500)
    )
    artifact, manifest = _manifest(tmp_path, rows)
    index_path = tmp_path / "candidate.sqlite"
    built = SqliteCandidateIndex.build(
        manifest=manifest,
        artifact_path=artifact,
        index_path=index_path,
    )
    index = _open_index(index_path, manifest, built.index_sha256)
    before = _sha256(index_path)

    first = index.query(_structure(), limit=5)
    second = index.query(_structure(), limit=5)

    assert first == second
    assert first.returned_count == 5
    assert first.returned_count <= first.limit
    assert first.posting_count <= MAX_POSTING_BUDGET
    assert first.posting_budget == MAX_POSTING_BUDGET
    assert first.hits[0].market.market_id == "target"
    assert first.hits[0].overlap_terms > first.hits[-1].overlap_terms
    assert _sha256(index_path) == before

    encoded = quote(str(index_path.resolve()), safe="/")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('write', 'no')"
            )
    finally:
        connection.close()


def test_query_excludes_closed_markets_and_empty_terms_return_empty(tmp_path):
    artifact, manifest = _manifest(
        tmp_path,
        [
            _row(
                "closed-target",
                title="Google Gemini LMSYS leaderboard",
                is_open=False,
                closed_time=CUTOFF.isoformat(),
            ),
            _row("open-unrelated", title="Weather in Zurich"),
        ],
    )
    index_path = tmp_path / "candidate.sqlite"
    built = SqliteCandidateIndex.build(
        manifest=manifest,
        artifact_path=artifact,
        index_path=index_path,
    )
    index = _open_index(index_path, manifest, built.index_sha256)

    closed = index.query(_structure(), limit=10)
    empty = index.query(
        _structure(
            claim_summary="the and",
            entities=[Entity(name="the", role="subject")],
            metric=Metric(what="the", measured_by="and", objective=False),
            contractible_version="the or",
        ),
        limit=10,
    )

    assert closed.hits == ()
    assert empty.query_terms == ()
    assert empty.hits == ()


def test_build_and_query_term_budgets_are_hard_bounds(tmp_path):
    market_terms = " ".join(
        f"marketterm{number}" for number in range(MAX_TERMS_PER_MARKET + 50)
    )
    artifact, manifest = _manifest(
        tmp_path,
        [_row("many-terms", description=market_terms)],
    )
    index_path = tmp_path / "candidate.sqlite"
    built = SqliteCandidateIndex.build(
        manifest=manifest,
        artifact_path=artifact,
        index_path=index_path,
    )
    index = _open_index(index_path, manifest, built.index_sha256)
    query_terms = " ".join(
        f"queryterm{number}" for number in range(MAX_QUERY_TERMS + 50)
    )
    result = index.query(
        _structure(claim_summary=query_terms),
        limit=10,
    )

    assert built.indexed_term_count == MAX_TERMS_PER_MARKET
    assert len(result.query_terms) == MAX_QUERY_TERMS


def test_query_selects_rare_terms_without_exceeding_posting_budget(tmp_path):
    rows = [
        _row(
            "target",
            title="commonterm rareterm",
            description="commonterm rareterm",
        )
    ]
    rows.extend(
        _row(
            f"common-{number:05d}",
            title="commonterm",
            description="commonterm",
        )
        for number in range(MAX_POSTING_BUDGET)
    )
    artifact, manifest = _manifest(tmp_path, rows)
    index_path = tmp_path / "candidate.sqlite"
    built = SqliteCandidateIndex.build(
        manifest=manifest,
        artifact_path=artifact,
        index_path=index_path,
    )
    index = _open_index(index_path, manifest, built.index_sha256)

    rare = index.query(
        _structure(
            claim_summary="commonterm rareterm",
            entities=[Entity(name="commonterm rareterm", role="subject")],
            metric=Metric(
                what="commonterm rareterm",
                measured_by="commonterm rareterm",
                objective=True,
            ),
            contractible_version="commonterm rareterm",
        ),
        limit=10,
    )
    common_only = index.query(
        _structure(
            claim_summary="commonterm",
            entities=[Entity(name="commonterm", role="subject")],
            metric=Metric(
                what="commonterm",
                measured_by="commonterm",
                objective=True,
            ),
            contractible_version="commonterm",
        ),
        limit=10,
    )

    assert rare.query_terms == ("commonterm", "rareterm")
    assert rare.selected_terms == ("rareterm",)
    assert rare.posting_count == 1
    assert rare.posting_budget_exhausted
    assert [hit.market.market_id for hit in rare.hits] == ["target"]
    assert common_only.selected_terms == ()
    assert common_only.posting_count == 0
    assert common_only.posting_budget_exhausted
    assert common_only.hits == ()


def test_build_fails_when_artifact_or_count_differs_from_manifest(tmp_path):
    artifact, manifest = _manifest(
        tmp_path,
        [_row("direct", title="Google Gemini LMSYS market")],
    )
    # Same byte length and still canonical JSON, but no longer the pinned bytes.
    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace("Gemini", "Geminx"),
        encoding="utf-8",
    )
    with pytest.raises(CandidateIndexArtifactError, match="pinned manifest"):
        SqliteCandidateIndex.build(
            manifest=manifest,
            artifact_path=artifact,
            index_path=tmp_path / "digest.sqlite",
        )

    artifact, manifest = _manifest(tmp_path, [_row("one")], name="count")
    raw = manifest.model_dump(mode="json")
    raw["row_count"] = 2
    raw["unique_market_count"] = 2
    raw["validation_report"]["row_count"] = 2
    raw["validation_report"]["unique_market_count"] = 2
    inconsistent_count = SnapshotManifest.model_validate(raw)
    with pytest.raises(CandidateIndexArtifactError, match="pinned manifest"):
        SqliteCandidateIndex.build(
            manifest=inconsistent_count,
            artifact_path=artifact,
            index_path=tmp_path / "count.sqlite",
        )


def test_build_rejects_artifact_rows_above_byte_ceiling(tmp_path):
    artifact, manifest = _manifest(
        tmp_path,
        [_row("oversized", description="x" * MAX_ARTIFACT_ROW_BYTES)],
    )
    index_path = tmp_path / "candidate.sqlite"

    with pytest.raises(CandidateIndexArtifactError, match="byte ceiling"):
        SqliteCandidateIndex.build(
            manifest=manifest,
            artifact_path=artifact,
            index_path=index_path,
        )

    assert not index_path.exists()


def test_open_and_query_fail_closed_on_identity_or_policy_drift(tmp_path):
    artifact, manifest = _manifest(tmp_path, [_row("one")])
    index_path = tmp_path / "candidate.sqlite"
    built = SqliteCandidateIndex.build(
        manifest=manifest,
        artifact_path=artifact,
        index_path=index_path,
    )

    with pytest.raises(CandidateIndexIdentityConflict, match="identity"):
        SqliteCandidateIndex.open(
            index_path,
            expected_snapshot_id=f"mu_{'0' * 64}",
            expected_content_sha256=manifest.content_sha256,
            expected_artifact_sha256=manifest.artifact_sha256,
            expected_index_sha256=built.index_sha256,
        )
    with pytest.raises(CandidateIndexIdentityConflict, match="identity"):
        SqliteCandidateIndex.open(
            index_path,
            expected_snapshot_id=manifest.snapshot_id,
            expected_content_sha256="0" * 64,
            expected_artifact_sha256=manifest.artifact_sha256,
            expected_index_sha256=built.index_sha256,
        )
    with pytest.raises(CandidateIndexIdentityConflict, match="identity"):
        SqliteCandidateIndex.open(
            index_path,
            expected_snapshot_id=manifest.snapshot_id,
            expected_content_sha256=manifest.content_sha256,
            expected_artifact_sha256=manifest.artifact_sha256,
            expected_index_sha256=built.index_sha256,
            expected_policy_version="future-policy",
        )

    index = _open_index(index_path, manifest, built.index_sha256)
    for bad_limit in (0, MAX_QUERY_LIMIT + 1):
        with pytest.raises(ValueError, match="limit"):
            index.query(_structure(), limit=bad_limit)


def test_open_handle_rejects_valid_sqlite_mutation(tmp_path):
    artifact, manifest = _manifest(
        tmp_path,
        [_row("target", title="Google Gemini LMSYS leaderboard")],
    )
    index_path = tmp_path / "candidate.sqlite"
    built = SqliteCandidateIndex.build(
        manifest=manifest,
        artifact_path=artifact,
        index_path=index_path,
    )
    index = _open_index(index_path, manifest, built.index_sha256)

    connection = sqlite3.connect(index_path)
    try:
        connection.execute(
            "UPDATE markets SET is_open = 0 WHERE market_id = 'target'"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(CandidateIndexIdentityConflict, match="changed"):
        index.query(_structure(), limit=10)
    with pytest.raises(CandidateIndexIdentityConflict, match="digest"):
        SqliteCandidateIndex.open(
            index_path,
            expected_snapshot_id=manifest.snapshot_id,
            expected_content_sha256=manifest.content_sha256,
            expected_artifact_sha256=manifest.artifact_sha256,
            expected_index_sha256=built.index_sha256,
        )


def test_existing_different_identity_is_never_overwritten(tmp_path):
    first_artifact, first_manifest = _manifest(
        tmp_path,
        [_row("first")],
        name="first",
    )
    index_path = tmp_path / "candidate.sqlite"
    first = SqliteCandidateIndex.build(
        manifest=first_manifest,
        artifact_path=first_artifact,
        index_path=index_path,
    )

    second_artifact, second_manifest = _manifest(
        tmp_path,
        [_row("second")],
        name="second",
    )
    with pytest.raises(CandidateIndexIdentityConflict, match="identity"):
        SqliteCandidateIndex.build(
            manifest=second_manifest,
            artifact_path=second_artifact,
            index_path=index_path,
            expected_index_sha256=first.index_sha256,
        )

    assert _sha256(index_path) == first.index_sha256
    assert _open_index(
        index_path,
        first_manifest,
        first.index_sha256,
    ).snapshot_id == (
        first_manifest.snapshot_id
    )


def test_concurrent_same_identity_builds_publish_one_verified_index(
    tmp_path, monkeypatch
):
    import el.retrieval.candidate_index as candidate_index_module

    artifact, manifest = _manifest(
        tmp_path,
        [_row(f"market-{number:04d}") for number in range(200)],
    )
    target = tmp_path / "candidate.sqlite"
    original_link = candidate_index_module.os.link
    publication_barrier = Barrier(2)

    def synchronized_link(source, destination):
        publication_barrier.wait(timeout=10)
        return original_link(source, destination)

    monkeypatch.setattr(candidate_index_module.os, "link", synchronized_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: SqliteCandidateIndex.build(
                    manifest=manifest,
                    artifact_path=artifact,
                    index_path=target,
                ),
                range(2),
            )
        )

    assert sorted(result.reused for result in results) == [False, True]
    assert len({result.index_sha256 for result in results}) == 1
    assert _sha256(target) == results[0].index_sha256
    assert not list(tmp_path.glob(".candidate.sqlite.*.tmp"))


def test_concurrent_different_identity_build_never_clobbers_winner(
    tmp_path, monkeypatch
):
    import el.retrieval.candidate_index as candidate_index_module

    first_artifact, first_manifest = _manifest(
        tmp_path, [_row("first")], name="first-race"
    )
    second_artifact, second_manifest = _manifest(
        tmp_path, [_row("second")], name="second-race"
    )
    target = tmp_path / "candidate.sqlite"
    original_link = candidate_index_module.os.link
    publication_barrier = Barrier(2)

    def synchronized_link(source, destination):
        publication_barrier.wait(timeout=10)
        return original_link(source, destination)

    monkeypatch.setattr(candidate_index_module.os, "link", synchronized_link)

    def build(args):
        artifact, manifest = args
        try:
            return SqliteCandidateIndex.build(
                manifest=manifest,
                artifact_path=artifact,
                index_path=target,
            )
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                build,
                (
                    (first_artifact, first_manifest),
                    (second_artifact, second_manifest),
                ),
            )
        )

    winners = [
        outcome for outcome in outcomes if not isinstance(outcome, Exception)
    ]
    losers = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], CandidateIndexIdentityConflict)
    assert _sha256(target) == winners[0].index_sha256
    assert not list(tmp_path.glob(".candidate.sqlite.*.tmp"))


def test_failed_publication_cleans_temporary_index(tmp_path, monkeypatch):
    import el.retrieval.candidate_index as candidate_index_module

    artifact, manifest = _manifest(tmp_path, [_row("one")])
    target = tmp_path / "candidate.sqlite"

    def fail_publication(_source, _destination):
        raise OSError("injected publication failure")

    monkeypatch.setattr(candidate_index_module.os, "link", fail_publication)
    with pytest.raises(OSError, match="injected publication failure"):
        SqliteCandidateIndex.build(
            manifest=manifest,
            artifact_path=artifact,
            index_path=target,
        )

    assert not target.exists()
    assert not list(tmp_path.glob(".candidate.sqlite.*.tmp"))


@pytest.mark.parametrize(
    "governed_suffix",
    [
        ("data", "review"),
        ("data", "clean_goldens"),
        ("data", "eval_sets"),
        ("data", "review_batches"),
        ("docs", "archive"),
    ],
)
def test_build_refuses_governed_index_targets(tmp_path, governed_suffix):
    artifact, manifest = _manifest(tmp_path, [_row("one")])
    target = tmp_path / "repo"
    for part in governed_suffix:
        target /= part
    target /= "candidate.sqlite"

    with pytest.raises(CandidateIndexArtifactError, match="governed"):
        SqliteCandidateIndex.build(
            manifest=manifest,
            artifact_path=artifact,
            index_path=target,
        )
    assert not target.exists()


def test_phase0_indexed_recommended_recall_remains_one(tmp_path):
    snapshot = json.loads(
        (FIXTURES / "frozen_snapshot_phase0.json").read_text(encoding="utf-8")
    )
    rows = []
    for raw in snapshot["markets"]:
        rows.append(
            _row(
                raw["market_id"],
                title=raw["title"],
                slug=raw["market_id"],
                description=raw["description"],
                resolution_rules=raw["resolution_rules"],
                close_date=raw["close_date"],
                volume_usd=None,
                taxonomy_l1=raw["taxonomy_l1"],
                taxonomy_confidence=raw["taxonomy_confidence"],
                tags=raw["tags"],
                source_url=raw["source_url"],
            )
        )
    artifact, manifest = _manifest(tmp_path, rows)
    index_path = tmp_path / "candidate.sqlite"
    built = SqliteCandidateIndex.build(
        manifest=manifest,
        artifact_path=artifact,
        index_path=index_path,
    )
    index = _open_index(index_path, manifest, built.index_sha256)

    hits = total = 0
    cases = load_golden_cases(FIXTURES / "recall_golden_phase0.json")
    for case in cases:
        if case.recommended_market_id is None:
            continue
        total += 1
        indexed = index.query(case.structure, limit=10)
        records = [
            CandidateMarketRecord(
                market_id=hit.market.market_id,
                title=hit.market.title,
                venue="fixture",
                description=hit.market.description,
                resolution_rules=hit.market.resolution_rules,
                close_date=hit.market.close_date,
                outcomes=hit.market.outcomes,
                current_probability=None,
                # Volume is not liquidity; leave the gate's field unknown.
                liquidity_usd=None,
                taxonomy_l1=hit.market.taxonomy_l1,
                taxonomy_confidence=hit.market.taxonomy_confidence,
                taxonomy_low_confidence=None,
                tags=hit.market.tags,
                source_url=hit.market.source_url,
            )
            for hit in indexed.hits
        ]
        ranked = rank_candidates(case.structure, records)
        eligible = [
            record.market_id
            for record, _score in ranked
            if evaluate_eligibility(record, case.structure).eligible
        ][:5]
        hits += int(case.recommended_market_id in eligible)

    assert total == 4
    assert hits / total == 1.0


def test_candidate_index_boundary_has_no_live_provider_or_model_imports():
    import el.retrieval.candidate_index as candidate_index

    source = Path(candidate_index.__file__).read_text(encoding="utf-8")
    assert "poly_data_client" not in source
    assert "google.genai" not in source
    assert ".market_universe(" not in source
    assert ".markets_pit(" not in source
