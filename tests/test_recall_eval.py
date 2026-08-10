"""Retrieval-recall harness — CI gate + structural eval-isolation check."""

from pathlib import Path

from el.evals.recall import RECALL_K_DEFAULT, run_recall_eval

FIXTURES = Path(__file__).parent / "fixtures" / "retrieval"
GOLDEN = FIXTURES / "recall_golden_phase0.json"
SNAPSHOT = FIXTURES / "frozen_snapshot_phase0.json"


def test_recall_gate_passes_on_seed_set():
    # THE Loop 2 CI gate: every known-good market in top-K (hard 1.0).
    # recommended_total is 4 since the eval_002 governed label correction
    # (direct -> weak_proxy; blueprint ratification item 9) moved its
    # market to the tempting list.
    report = run_recall_eval(golden_path=GOLDEN, snapshot_path=SNAPSHOT)
    assert report.passed, report.model_dump_json(indent=2)
    assert report.recommended_recall == 1.0
    assert report.recommended_total == 4
    assert report.k == RECALL_K_DEFAULT


def test_recall_run_invariance():
    # Identical inputs -> identical report (deterministic retrieval,
    # ranking, and gating end to end).
    first = run_recall_eval(golden_path=GOLDEN, snapshot_path=SNAPSHOT)
    second = run_recall_eval(golden_path=GOLDEN, snapshot_path=SNAPSHOT)
    assert first.model_dump() == second.model_dump()


def test_tempting_recall_reported_not_gated():
    # Tempting-market recall is informational until the EL-native golden
    # set fixes its threshold (eval-set requirements doc). It must be
    # present in the report; it must NOT affect `passed`.
    report = run_recall_eval(golden_path=GOLDEN, snapshot_path=SNAPSHOT)
    assert report.tempting_total == 8
    assert 0.0 <= report.tempting_recall <= 1.0


def test_evals_cannot_reach_live_provider():
    # Invariant #2, structural: eval truth is frozen snapshots; the live
    # provider must not be importable from the eval harness (mirrors
    # MFTA's no-live-providers-in-evals rule as a machine check).
    import el.evals.recall as recall_module

    source = Path(recall_module.__file__).read_text()
    assert "PolyData" not in source
    assert "build_market_provider" not in source
    assert "FixtureMarketProvider" in source
