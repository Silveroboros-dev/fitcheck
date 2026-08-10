"""Fit-gate eval harness — the Loop 3 CI gates (blueprint §8, §14).

Three modes:

1. GATED (CI): labeled pairs only — pool per case is exactly the
   judgment-relevant markets (expected recommended + expected rejected).
   Deterministic path, zero model calls, zero new labeling: the
   loop3-v1 ceiling must reproduce the labels by itself. Retrieval is
   not in this loop (eval_003's lexically unreachable tempting market
   binds the future end-to-end smoke test, not this harness).
2. REPORTED (CI, never gated): full pool × claims sweep. Metrics:
   ceiling-direct rate on off-label pairs (bad-twin pressure — any hit
   is a review candidate) and the under-call rates, including the
   known_undercall_v1 red-team cases (the safe direction is not free;
   its product cost is measured here, not asserted).
3. ADVISORY AGREEMENT (off-CI, live): lands with the fit adapter
   (build commit 8). Arm A of H-P2.

GATED_CASES grows one stress family per commit — a case joins the set
in the same commit as the check that makes its label reproducible
(eval-first invariant). known_undercall_v1 cases NEVER gate: they are
labeled at truth and fail by design until structure (schema-v2 legs,
alias tables) earns their gating via Loop 4.

Eval truth = frozen golden structures only (invariant #2). This module
must not import model adapters; a CI test asserts that over its source.
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from el.domain.enums import FitClass
from el.domain.structures import ExtractedStructure, MarketStructure
from el.fitgate.aliases import ALIAS_RULES_EMPTY
from el.fitgate.checks import TOKEN_RULES_VERSION
from el.fitgate.policy import (
    FIT_GATE_POLICY_VERSION,
    STRONG_CLASSES,
    WEAK_CLASSES,
    FitPolicy,
    aggregate_thesis,
    class_rank,
    evaluate_market,
)

# A case joins in the same commit as the check that makes its label
# reproducible (eval-first invariant):
# commit 1 (E1+M1 twin-safety core): eval_001/003/004/005/008 + rt_001/rt_002
# commit 2 (S1 event-stage):         eval_002 (corrected label), 006, 007
# commit 3 (H1 horizon tolerance):   eval_010
# commit 4 (X1 mechanism):           eval_009 (all 10 seed cases gated)
# commit 6 (X2 composite):           rt_005 (single leg); rt_004 undercall activates
# commit 7 (composite-v2 candidate): rt_006-012 adversarial traps stay capped
GATED_CASES: frozenset[str] = frozenset(
    {
        "eval_001",
        "eval_002",
        "eval_003",
        "eval_004",
        "eval_005",
        "eval_006",
        "eval_007",
        "eval_008",
        "eval_009",
        "eval_010",
        "rt_001_polarity_no",
        "rt_002_deadline_split",
        "rt_005_composite_single_leg",
        "rt_006_composite_and_vs_or",
        "rt_007_composite_extra_condition",
        "rt_008_composite_duplicate_leg",
        "rt_009_composite_wrong_entity",
        "rt_010_composite_wrong_horizon",
        "rt_011_composite_wrong_threshold",
        "rt_012_composite_unknown_operator",
    }
)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FitCase(_Model):
    case_id: str
    source: str  # "phase0" | "el-native"
    structure: ExtractedStructure
    expected_class: FitClass
    expected_recommended: str | None
    expected_rejected: list[str] = []
    expected_thesis_side: str | None = None
    known_undercall_v1: bool = False
    pool_market_ids: list[str]


class MarketVerdictSummary(_Model):
    market_id: str
    ceiling: FitClass
    hard_fails: int
    fired: list[str]


class FitCaseResult(_Model):
    case_id: str
    source: str
    gated: bool
    known_undercall_v1: bool
    expected_class: FitClass
    actual_class: FitClass
    expected_recommended: str | None
    actual_recommended: str | None
    expected_rejected: list[str]
    actual_rejected: list[str]
    expected_thesis_side: str | None
    actual_thesis_side: str | None  # P1 polarity is live; None only when no recommendation
    per_market: list[MarketVerdictSummary]
    undercall: bool
    overcall: bool
    passed: bool


class ClassMetric(_Model):
    precision: float | None
    recall: float | None  # == TPR (true positive rate) for this class
    support: int
    predicted: int
    true_positive: int
    # One-vs-rest denominators (defaulted so synthetic ClassMetric(...)
    # constructions stay valid). tnr = specificity = TN / (TN + FP).
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0
    tnr: float | None = None


class FitMetrics(_Model):
    case_count: int
    confusion_matrix: dict[str, dict[str, int]]
    per_class: dict[str, ClassMetric]
    undercall_count: int
    overcall_count: int
    # Case ids where the truth is weak/no-clean but the gate published a
    # recommendable (direct OR indirect) class — false STRONG recommendations.
    # Defaulted for back-compat with serialized/synthetic FitMetrics.
    false_strong: list[str] = []


class FitEvalReport(_Model):
    gate_policy_version: str = FIT_GATE_POLICY_VERSION
    token_rules_version: str = TOKEN_RULES_VERSION
    alias_rules_version: str = ALIAS_RULES_EMPTY
    gated_cases: list[str]
    cases: list[FitCaseResult]
    gated_passed: bool
    # The hard zero (claim discipline: DETERMINISTIC direct FPs): labeled
    # non-direct theses where the ceiling published direct.
    ceiling_direct_fp: list[str]
    # Broadened hard zero — "zero false STRONG recommendations": gated theses
    # whose truth is weak/no-clean but the gate published a recommendable
    # (direct OR indirect) class. Strict superset of ceiling_direct_fp
    # (direct ⊂ strong); defaulted for synthetic FitEvalReport construction.
    false_strong_gated: list[str] = []
    undercall_known_active: list[str]
    undercall_marker_stale: list[str]
    undercall_pending: list[str]  # ungated, unmarked, currently undercalling
    metrics_all: FitMetrics
    metrics_gated: FitMetrics
    passed: bool


class PoolSweepReport(_Model):
    gate_policy_version: str = FIT_GATE_POLICY_VERSION
    alias_rules_version: str = ALIAS_RULES_EMPTY
    pairs_total: int
    off_label_direct: list[str]  # "case_id::market_id"
    off_label_direct_rate: float


def _repo_root() -> Path:
    return Path(__file__).parents[2]


def load_market_registry(
    golden_markets_path: str | Path, el_native_path: str | Path
) -> dict[str, MarketStructure]:
    registry: dict[str, MarketStructure] = {}
    golden = json.loads(Path(golden_markets_path).read_text())
    for raw in golden["structures"]:
        structure = MarketStructure.model_validate(raw)
        registry[structure.market_id] = structure
    native = json.loads(Path(el_native_path).read_text())
    for raw in native["markets"]:
        structure = MarketStructure.model_validate(raw)
        registry[structure.market_id] = structure
    return registry


def load_fit_cases(
    eval_set_path: str | Path,
    golden_claims_path: str | Path,
    el_native_path: str | Path,
) -> list[FitCase]:
    cases: list[FitCase] = []

    labels = {c["id"]: c for c in json.loads(Path(eval_set_path).read_text())}
    golden_claims = json.loads(Path(golden_claims_path).read_text())
    for entry in golden_claims["cases"]:
        case_id = entry["case_id"]
        fit_card = labels[case_id]["expected_fit_card"]
        recommended = fit_card["recommended_market_id"]
        rejected = fit_card.get("rejected_market_ids", [])
        pool = ([recommended] if recommended else []) + list(rejected)
        cases.append(
            FitCase(
                case_id=case_id,
                source="phase0",
                structure=ExtractedStructure.model_validate(entry["structure"]),
                expected_class=FitClass(fit_card["semantic_fit_class"]),
                expected_recommended=recommended,
                expected_rejected=list(rejected),
                pool_market_ids=pool,
            )
        )

    native = json.loads(Path(el_native_path).read_text())
    for entry in native["cases"]:
        fit_card = entry["expected_fit_card"]
        cases.append(
            FitCase(
                case_id=entry["case_id"],
                source="el-native",
                structure=ExtractedStructure.model_validate(entry["structure"]),
                expected_class=FitClass(fit_card["semantic_fit_class"]),
                expected_recommended=fit_card["recommended_market_id"],
                expected_rejected=list(fit_card.get("rejected_market_ids", [])),
                expected_thesis_side=entry.get("expected_thesis_side"),
                known_undercall_v1=entry.get("known_undercall_v1", False),
                pool_market_ids=list(entry["pool_market_ids"]),
            )
        )
    return cases


def _evaluate_case(
    case: FitCase,
    registry: dict[str, MarketStructure],
    policy: FitPolicy,
) -> FitCaseResult:
    verdicts = [
        (evaluate_market(case.structure, registry[market_id], policy), rank)
        for rank, market_id in enumerate(case.pool_market_ids)
    ]
    thesis = aggregate_thesis(verdicts)
    actual_rejected = [v.market_id for v in thesis.rejected]

    class_ok = thesis.fit_class == case.expected_class
    recommended_ok = thesis.recommended_market_id == case.expected_recommended
    rejected_ok = set(case.expected_rejected) <= set(actual_rejected)
    # Thesis side = the P1 side on the RECOMMENDED market (a weak/no-clean
    # card has no recommendation to record intent against).
    actual_side: str | None = None
    if thesis.recommended_market_id is not None:
        recommended_verdict = next(
            v for v, _ in verdicts if v.market_id == thesis.recommended_market_id
        )
        actual_side = recommended_verdict.thesis_side
    side_ok = (
        None
        if case.expected_thesis_side is None or actual_side is None
        else actual_side == case.expected_thesis_side
    )

    expected_rank = class_rank(case.expected_class)
    actual_rank = class_rank(thesis.fit_class)
    return FitCaseResult(
        case_id=case.case_id,
        source=case.source,
        gated=case.case_id in GATED_CASES,
        known_undercall_v1=case.known_undercall_v1,
        expected_class=case.expected_class,
        actual_class=thesis.fit_class,
        expected_recommended=case.expected_recommended,
        actual_recommended=thesis.recommended_market_id,
        expected_rejected=case.expected_rejected,
        actual_rejected=actual_rejected,
        expected_thesis_side=case.expected_thesis_side,
        actual_thesis_side=actual_side,
        per_market=[
            MarketVerdictSummary(
                market_id=v.market_id,
                ceiling=v.deterministic_ceiling,
                hard_fails=v.hard_fail_count,
                fired=v.fired(),
            )
            for v, _ in verdicts
        ],
        undercall=actual_rank < expected_rank,
        overcall=actual_rank > expected_rank,
        passed=class_ok and recommended_ok and rejected_ok and side_ok is not False,
    )


def _fit_metrics(results: list[FitCaseResult]) -> FitMetrics:
    classes = [fit_class.value for fit_class in FitClass]
    matrix = {
        expected: {actual: 0 for actual in classes}
        for expected in classes
    }
    for result in results:
        matrix[result.expected_class.value][result.actual_class.value] += 1

    case_count = len(results)
    per_class: dict[str, ClassMetric] = {}
    for fit_class in classes:
        true_positive = matrix[fit_class][fit_class]
        support = sum(matrix[fit_class][actual] for actual in classes)
        predicted = sum(matrix[expected][fit_class] for expected in classes)
        # One-vs-rest cells: FP = predicted off-target, FN = missed support,
        # TN = everything neither predicted nor truly this class.
        false_positive = predicted - true_positive
        false_negative = support - true_positive
        true_negative = case_count - true_positive - false_positive - false_negative
        per_class[fit_class] = ClassMetric(
            precision=(true_positive / predicted if predicted else None),
            recall=(true_positive / support if support else None),
            support=support,
            predicted=predicted,
            true_positive=true_positive,
            false_positive=false_positive,
            false_negative=false_negative,
            true_negative=true_negative,
            tnr=(
                true_negative / (true_negative + false_positive)
                if (true_negative + false_positive)
                else None
            ),
        )
    false_strong = [
        result.case_id
        for result in results
        if result.expected_class in WEAK_CLASSES
        and result.actual_class in STRONG_CLASSES
    ]
    return FitMetrics(
        case_count=case_count,
        confusion_matrix=matrix,
        per_class=per_class,
        undercall_count=sum(1 for result in results if result.undercall),
        overcall_count=sum(1 for result in results if result.overcall),
        false_strong=false_strong,
    )


def run_fit_eval(
    eval_set_path: str | Path,
    golden_claims_path: str | Path,
    golden_markets_path: str | Path,
    el_native_path: str | Path,
    policy: FitPolicy = FitPolicy(),
) -> FitEvalReport:
    """Mode 1 — the CI gate over labeled pairs."""
    registry = load_market_registry(golden_markets_path, el_native_path)
    cases = load_fit_cases(eval_set_path, golden_claims_path, el_native_path)
    results = [_evaluate_case(case, registry, policy) for case in cases]

    gated_results = [r for r in results if r.gated and not r.known_undercall_v1]
    ceiling_direct_fp = [
        r.case_id
        for r in results
        if r.expected_class is not FitClass.DIRECT
        and r.actual_class is FitClass.DIRECT
        and r.gated
    ]
    # Broadened hard zero over the recommendable set (direct OR indirect). Same
    # gated scoping as ceiling_direct_fp; a strict superset of it.
    false_strong_gated = [
        r.case_id
        for r in results
        if r.expected_class in WEAK_CLASSES
        and r.actual_class in STRONG_CLASSES
        and r.gated
    ]
    return FitEvalReport(
        alias_rules_version=policy.alias_rules_version,
        gated_cases=sorted(GATED_CASES),
        cases=results,
        gated_passed=all(r.passed for r in gated_results),
        ceiling_direct_fp=ceiling_direct_fp,
        false_strong_gated=false_strong_gated,
        undercall_known_active=[
            r.case_id for r in results if r.known_undercall_v1 and r.undercall
        ],
        undercall_marker_stale=[
            r.case_id for r in results if r.known_undercall_v1 and not r.undercall
        ],
        undercall_pending=[
            r.case_id
            for r in results
            if r.undercall and not r.known_undercall_v1 and not r.gated
        ],
        metrics_all=_fit_metrics(results),
        metrics_gated=_fit_metrics(gated_results),
        passed=(
            all(r.passed for r in gated_results)
            and not ceiling_direct_fp
            and not false_strong_gated
        ),
    )


def run_pool_sweep(
    eval_set_path: str | Path,
    golden_claims_path: str | Path,
    golden_markets_path: str | Path,
    el_native_path: str | Path,
    policy: FitPolicy = FitPolicy(),
) -> PoolSweepReport:
    """Mode 2 — every claim against every known market (reported only).

    Off-label ceiling-direct pairs are the standing bad-twin pressure
    metric: the count must trend to zero as stress families land, and
    any residue is review-candidate material, never silently accepted.
    """
    registry = load_market_registry(golden_markets_path, el_native_path)
    cases = load_fit_cases(eval_set_path, golden_claims_path, el_native_path)
    allowed_direct_by_claim: dict[str, set[str]] = {}
    for case in cases:
        if (
            case.expected_class is FitClass.DIRECT
            and case.expected_recommended is not None
        ):
            allowed_direct_by_claim.setdefault(
                case.structure.claim_summary, set()
            ).add(case.expected_recommended)
    off_label: list[str] = []
    pairs = 0
    for case in cases:
        allowed_direct = allowed_direct_by_claim.get(
            case.structure.claim_summary, set()
        )
        for market_id, market in registry.items():
            pairs += 1
            verdict = evaluate_market(case.structure, market, policy)
            if (
                verdict.deterministic_ceiling is FitClass.DIRECT
                and market_id not in allowed_direct
            ):
                off_label.append(f"{case.case_id}::{market_id}")
    return PoolSweepReport(
        alias_rules_version=policy.alias_rules_version,
        pairs_total=pairs,
        off_label_direct=sorted(off_label),
        off_label_direct_rate=len(off_label) / pairs if pairs else 0.0,
    )


def _default_paths() -> tuple[Path, Path, Path, Path]:
    root = _repo_root()
    return (
        root / "evals" / "data" / "eval_set.json",
        root / "tests" / "fixtures" / "retrieval" / "recall_golden_phase0.json",
        root / "tests" / "fixtures" / "markets" / "golden_market_structures.json",
        root / "tests" / "fixtures" / "fit" / "el_native_cases.json",
    )


def _print_per_class(label: str, metrics: "FitMetrics") -> None:
    print(f"  per-class metrics ({label}):")
    for fit_class, metric in metrics.per_class.items():
        precision = "n/a" if metric.precision is None else f"{metric.precision:.3f}"
        tpr = "n/a" if metric.recall is None else f"{metric.recall:.3f}"
        tnr = "n/a" if metric.tnr is None else f"{metric.tnr:.3f}"
        print(
            f"    {fit_class}: precision={precision} "
            f"({metric.true_positive}/{metric.predicted} predicted), "
            f"TPR={tpr} ({metric.true_positive}/{metric.support} support), "
            f"TNR={tnr} ({metric.true_negative}/"
            f"{metric.true_negative + metric.false_positive} non-target)"
        )
    print(f"    false_strong (truth weak/no-clean -> strong): {metrics.false_strong or 0}")


if __name__ == "__main__":
    eval_set, claims, markets, native = _default_paths()
    report = run_fit_eval(eval_set, claims, markets, native)
    sweep = run_pool_sweep(eval_set, claims, markets, native)
    print(f"fit eval — {report.gate_policy_version} / {report.token_rules_version}")
    print(f"  gated: {'PASS' if report.passed else 'FAIL'} "
          f"({len(report.gated_cases)} cases)")
    for result in report.cases:
        marker = (
            "GATED" if result.gated else
            "undercall-marked" if result.known_undercall_v1 else "reported"
        )
        status = "ok" if result.passed else "MISMATCH"
        print(
            f"  [{marker:>16}] {result.case_id}: expected "
            f"{result.expected_class} -> actual {result.actual_class} ({status})"
        )
    print(f"  ceiling-direct FP (gated): {report.ceiling_direct_fp or 0}")
    print(f"  false-strong FP (gated): {report.false_strong_gated or 0}")
    print(f"  undercall known-active: {report.undercall_known_active}")
    print(f"  undercall marker-stale: {report.undercall_marker_stale}")
    print(f"  undercall pending (ungated): {report.undercall_pending}")
    _print_per_class("all cases", report.metrics_all)
    print(
        f"  under/over calls (all): {report.metrics_all.undercall_count}/"
        f"{report.metrics_all.overcall_count}"
    )
    _print_per_class("gated cases", report.metrics_gated)
    print(
        f"  sweep: {len(report.cases)} claims x {sweep.pairs_total // len(report.cases)} "
        f"markets = {sweep.pairs_total} pairs; off-label direct: "
        f"{len(sweep.off_label_direct)} ({sweep.off_label_direct_rate:.0%})"
    )
