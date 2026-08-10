"""Learning Loop v0 — review-candidate generation + persistence."""

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from el.domain.enums import FitClass
from el.domain.tables import Base, ReviewCandidate
from el.evals.fit import ClassMetric, FitEvalReport, FitMetrics, PoolSweepReport
from el.fitgate.aliases import ALIAS_RULES_EMPTY, ALIAS_RULES_VERSION
from el.review.candidates import (
    FAMILY_OFF_LABEL_DIRECT,
    FAMILY_UNDERCALL_KNOWN,
    collect_review_candidates,
)
from el.review.store import persist_candidates


def _metrics() -> FitMetrics:
    classes = [fit_class.value for fit_class in FitClass]
    return FitMetrics(
        case_count=0,
        confusion_matrix={
            expected: {actual: 0 for actual in classes}
            for expected in classes
        },
        per_class={
            fit_class: ClassMetric(
                precision=None,
                recall=None,
                support=0,
                predicted=0,
                true_positive=0,
            )
            for fit_class in classes
        },
        undercall_count=0,
        overcall_count=0,
    )


def _fit(**kw) -> FitEvalReport:
    base = dict(
        gated_cases=[],
        cases=[],
        gated_passed=True,
        ceiling_direct_fp=[],
        undercall_known_active=[],
        undercall_marker_stale=[],
        undercall_pending=[],
        metrics_all=_metrics(),
        metrics_gated=_metrics(),
        passed=True,
    )
    base.update(kw)
    return FitEvalReport(**base)


def _sweep(**kw) -> PoolSweepReport:
    base = dict(pairs_total=0, off_label_direct=[], off_label_direct_rate=0.0)
    base.update(kw)
    return PoolSweepReport(**base)


def _sessions():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_undercall_becomes_a_candidate():
    fit = _fit(undercall_known_active=["rt_003_synonym_undercall"])
    specs = collect_review_candidates(fit, _sweep(), source="eval_failure")
    assert len(specs) == 1
    assert specs[0].object_ref == "rt_003_synonym_undercall"
    assert specs[0].failure_family == FAMILY_UNDERCALL_KNOWN
    assert specs[0].object_type == "fit_case"


def test_off_label_becomes_a_candidate():
    sweep = _sweep(pairs_total=10, off_label_direct=["eval_x::mkt_y"])
    specs = collect_review_candidates(_fit(), sweep, source="eval_failure")
    assert specs[0].failure_family == FAMILY_OFF_LABEL_DIRECT
    assert specs[0].object_type == "claim_market_pair"


def test_collect_is_run_invariant_and_deduped():
    fit = _fit(undercall_known_active=["a", "b"])
    sweep = _sweep(off_label_direct=["c::d"])
    first = collect_review_candidates(fit, sweep, source="eval_failure")
    second = collect_review_candidates(fit, sweep, source="eval_failure")
    assert [s.fingerprint for s in first] == [s.fingerprint for s in second]
    assert len({s.fingerprint for s in first}) == len(first)


def test_fingerprint_binds_alias_version():
    # A candidate from one policy generation must not dedupe against another.
    empty = collect_review_candidates(
        _fit(undercall_known_active=["x"], alias_rules_version=ALIAS_RULES_EMPTY),
        _sweep(),
        source="eval_failure",
    )
    v1 = collect_review_candidates(
        _fit(undercall_known_active=["x"], alias_rules_version=ALIAS_RULES_VERSION),
        _sweep(),
        source="eval_failure",
    )
    assert empty[0].fingerprint != v1[0].fingerprint


def test_persist_is_idempotent_and_notes_human_readable():
    fit = _fit(undercall_known_active=["rt_003_synonym_undercall"])
    specs = collect_review_candidates(fit, _sweep(), source="eval_failure")
    sessions = _sessions()
    first = persist_candidates(sessions, specs)
    second = persist_candidates(sessions, specs)
    assert len(first) == 1 and second == []  # idempotent

    with sessions() as session:
        rows = session.scalars(select(ReviewCandidate)).all()
        assert len(rows) == 1
        assert rows[0].status == "pending"
        # human-readable prose, not structured evidence dumped in
        assert "rt_003" in rows[0].reviewer_notes
        assert "sha256" not in rows[0].reviewer_notes
        assert "fingerprint" not in rows[0].reviewer_notes


def test_different_policy_generation_does_not_dedupe():
    # Same ref under different alias versions -> different fingerprints ->
    # different object_id -> both rows persist (no cross-generation dedupe).
    empty = collect_review_candidates(
        _fit(undercall_known_active=["rt_003"], alias_rules_version=ALIAS_RULES_EMPTY),
        _sweep(),
        source="eval_failure",
    )
    v1 = collect_review_candidates(
        _fit(undercall_known_active=["rt_003"], alias_rules_version=ALIAS_RULES_VERSION),
        _sweep(),
        source="eval_failure",
    )
    sessions = _sessions()
    persist_candidates(sessions, empty)
    persist_candidates(sessions, v1)
    with sessions() as session:
        rows = session.scalars(select(ReviewCandidate)).all()
        assert len(rows) == 2  # same ref, two generations, two rows


def test_candidates_module_is_model_free():
    import el.review.candidates as module

    source = Path(module.__file__).read_text()
    assert "el.models" not in source
    assert "genai" not in source
