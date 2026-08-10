"""Promotion verifier — eval-delta-gated structure-check promotion (Loop 4).

A port of MFTA's repair-loop verifier (run_repair_loop.py), but where MFTA
used a predicted-relabel proxy, FitCheck runs the REAL gate twice — before
(alias-v0-empty) and after (alias-v1) — and applies shipping gates over the
two FitEvalReports + PoolSweepReports. This is the machinery that makes
"no unverified learning ships" structural: a structure-check change (a new
alias-table version) ships only on a recorded GO.

Verdict:
- go            — the targeted under-call resolves AND every safety gate passes.
- candidate_only— safety holds but no gain (keep evidence, do not promote).
- no_go         — a safety gate failed (the change is unsafe).

The verifier is PURE (no file I/O, no code edits): it returns a verdict and
an EvalDelta; writing the artifact and flipping the default alias version are
separate, deliberate acts (no_auto_promotion). Model-free.
"""

import argparse
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from el.evals.fit import (
    FitEvalReport,
    FitMetrics,
    PoolSweepReport,
    _default_paths,
    run_fit_eval,
    run_pool_sweep,
)
from el.fitgate.composite_v2 import (
    COMPOSITE_COVERAGE_VERSION,
    CompositeCoveragePolicy,
    load_composite_coverage_policy,
)
from el.fitgate.aliases import ALIAS_RULES_EMPTY, ALIAS_RULES_VERSION
from el.fitgate.policy import FitPolicy

PROMOTION_VERIFIER_VERSION = "promo-v1"


class GateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: str
    status: str  # "pass" | "fail"
    detail: str


class EvalDelta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    targeted_case_id: str
    targeted_family: str
    targeted_undercall_resolved: bool
    direct_fp_before: list[str]
    direct_fp_after: list[str]
    off_label_direct_before: int
    off_label_direct_after: int
    gated_passed_before: bool
    gated_passed_after: bool
    new_undercalls: list[str]


class PromotionVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verifier_version: str = PROMOTION_VERIFIER_VERSION
    alias_before: str
    alias_after: str
    verdict: str  # "go" | "candidate_only" | "no_go"
    gates: list[GateResult]
    blocking_gates: list[str]
    delta: EvalDelta


def _case(report: FitEvalReport, case_id: str):
    return next((c for c in report.cases if c.case_id == case_id), None)


def run_promotion_verifier(
    *,
    targeted_case_id: str,
    targeted_family: str = "undercall_known_active",
    before_policy: FitPolicy | None = None,
    after_policy: FitPolicy | None = None,
    paths: list | None = None,
) -> PromotionVerdict:
    """Run the real gate before/after, then apply the shipping gates."""
    before_policy = before_policy or FitPolicy(
        alias_rules_version=ALIAS_RULES_EMPTY
    )
    after_policy = after_policy or FitPolicy(
        alias_rules_version=ALIAS_RULES_VERSION
    )
    paths = paths or list(_default_paths())
    return evaluate_promotion(
        before_fit=run_fit_eval(*paths, policy=before_policy),
        after_fit=run_fit_eval(*paths, policy=after_policy),
        before_sweep=run_pool_sweep(*paths, policy=before_policy),
        after_sweep=run_pool_sweep(*paths, policy=after_policy),
        targeted_case_id=targeted_case_id,
        targeted_family=targeted_family,
    )


def evaluate_promotion(
    *,
    before_fit: FitEvalReport,
    after_fit: FitEvalReport,
    before_sweep: PoolSweepReport,
    after_sweep: PoolSweepReport,
    targeted_case_id: str,
    targeted_family: str = "undercall_known_active",
) -> PromotionVerdict:
    """Pure gate logic over before/after reports (no I/O) — testable on
    synthetic reports, decoupled from the live fixture state."""
    gates: list[GateResult] = []

    def gate(name: str, ok: bool, detail: str) -> None:
        gates.append(
            GateResult(gate=name, status="pass" if ok else "fail", detail=detail)
        )

    # The gain (real danger reduced): the targeted case under-called before
    # and resolves after. Read the case's own .undercall flag directly so the
    # result is reproducible regardless of which bucket (known_active vs
    # pending vs marker_stale) the case lands in — a membership check broke
    # when the marker moved.
    before_case = _case(before_fit, targeted_case_id)
    after_case = _case(after_fit, targeted_case_id)
    resolved = bool(
        before_case
        and after_case
        and before_case.undercall
        and not after_case.undercall
    )
    gate(
        "targeted_undercall_resolves",
        resolved,
        f"{targeted_case_id}: under-call before="
        f"{before_case.undercall if before_case else 'absent'}, "
        f"after={after_case.undercall if after_case else 'absent'}",
    )

    # Safety gates.
    new_direct_fp = sorted(
        set(after_fit.ceiling_direct_fp) - set(before_fit.ceiling_direct_fp)
    )
    gate(
        "no_new_direct_fp",
        not new_direct_fp,
        f"new deterministic direct FPs: {new_direct_fp or 'none'}",
    )
    gate(
        "gated_set_stays_green",
        after_fit.gated_passed,
        f"after.gated_passed={after_fit.gated_passed}",
    )
    new_off_label = sorted(
        set(after_sweep.off_label_direct) - set(before_sweep.off_label_direct)
    )
    gate(
        "no_new_off_label_direct",
        not new_off_label,
        f"new off-label directs (bad-twin pressure): {new_off_label or 'none'}",
    )
    before_uc = set(before_fit.undercall_known_active) | set(
        before_fit.undercall_pending
    )
    after_uc = set(after_fit.undercall_known_active) | set(
        after_fit.undercall_pending
    )
    new_undercalls = sorted(after_uc - before_uc)
    gate(
        "no_collateral_undercall",
        not new_undercalls,
        f"new under-calls elsewhere: {new_undercalls or 'none'}",
    )
    gate(
        "no_model_owned_final_class",
        True,
        "deterministic eval harness; no model participates in the promotion",
    )
    gate(
        "no_auto_promotion",
        True,
        "verifier emits a verdict + artifact only; the default-version flip "
        "is a separate human/Loop-4 act",
    )
    laundering_ok = (
        before_fit.gate_policy_version == after_fit.gate_policy_version
        and before_fit.token_rules_version == after_fit.token_rules_version
        and before_fit.alias_rules_version != after_fit.alias_rules_version
    )
    gate(
        "no_laundering",
        laundering_ok,
        f"only the alias axis moved (gate/token versions unchanged): "
        f"{before_fit.alias_rules_version} -> {after_fit.alias_rules_version}",
    )

    safety = [g for g in gates if g.gate != "targeted_undercall_resolves"]
    safety_ok = all(g.status == "pass" for g in safety)
    if safety_ok and resolved:
        verdict = "go"
    elif safety_ok:
        verdict = "candidate_only"
    else:
        verdict = "no_go"

    return PromotionVerdict(
        alias_before=before_fit.alias_rules_version,
        alias_after=after_fit.alias_rules_version,
        verdict=verdict,
        gates=gates,
        blocking_gates=[g.gate for g in gates if g.status == "fail"],
        delta=EvalDelta(
            targeted_case_id=targeted_case_id,
            targeted_family=targeted_family,
            targeted_undercall_resolved=resolved,
            direct_fp_before=before_fit.ceiling_direct_fp,
            direct_fp_after=after_fit.ceiling_direct_fp,
            off_label_direct_before=len(before_sweep.off_label_direct),
            off_label_direct_after=len(after_sweep.off_label_direct),
            gated_passed_before=before_fit.gated_passed,
            gated_passed_after=after_fit.gated_passed,
            new_undercalls=new_undercalls,
        ),
    )


COMPOSITE_TRAP_CASES = (
    "rt_005_composite_single_leg",
    "rt_006_composite_and_vs_or",
    "rt_007_composite_extra_condition",
    "rt_008_composite_duplicate_leg",
    "rt_009_composite_wrong_entity",
    "rt_010_composite_wrong_horizon",
    "rt_011_composite_wrong_threshold",
    "rt_012_composite_unknown_operator",
)


class CompositeTrapResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    expected_class: str
    actual_class: str
    passed: bool
    capped: bool


class CompositePromotionDelta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    targeted_case_id: str
    targeted_undercall_resolved: bool
    targeted_before_class: str | None
    targeted_after_class: str | None
    trap_results: list[CompositeTrapResult]
    direct_fp_before: list[str]
    direct_fp_after: list[str]
    off_label_direct_before: int
    off_label_direct_after: int
    gated_passed_before: bool
    gated_passed_after: bool
    new_undercalls: list[str]
    metrics_before_all: FitMetrics
    metrics_after_all: FitMetrics
    metrics_before_gated: FitMetrics
    metrics_after_gated: FitMetrics


class CompositePromotionVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verifier_version: str = PROMOTION_VERIFIER_VERSION
    composite_before: str
    composite_after: str
    verdict: str  # "go" | "candidate_only" | "no_go"
    gates: list[GateResult]
    blocking_gates: list[str]
    delta: CompositePromotionDelta


def _default_composite_policy_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "fit"
        / "composite_v2_sidecars.json"
    )


def run_composite_coverage_verifier(
    *,
    composite_path: str | Path | None = None,
    paths: list | None = None,
    targeted_case_id: str = "rt_004_composite_full_cover",
) -> CompositePromotionVerdict:
    """Run schema-v1 X2 before / composite-v2 sidecar after.

    This is a candidate verifier only. It does not flip the production default:
    FitPolicy(composite_coverage=None) remains the default until a human
    promotion action changes that deliberately.
    """
    paths = paths or list(_default_paths())
    policy_path = (
        Path(composite_path) if composite_path else _default_composite_policy_path()
    )
    composite = load_composite_coverage_policy(policy_path)
    before = FitPolicy(composite_coverage=None)
    after = FitPolicy(composite_coverage=composite)

    return evaluate_composite_coverage_promotion(
        before_fit=run_fit_eval(*paths, policy=before),
        after_fit=run_fit_eval(*paths, policy=after),
        before_sweep=run_pool_sweep(*paths, policy=before),
        after_sweep=run_pool_sweep(*paths, policy=after),
        composite_after=composite,
        targeted_case_id=targeted_case_id,
    )


def evaluate_composite_coverage_promotion(
    *,
    before_fit: FitEvalReport,
    after_fit: FitEvalReport,
    before_sweep: PoolSweepReport,
    after_sweep: PoolSweepReport,
    composite_after: CompositeCoveragePolicy,
    targeted_case_id: str,
) -> CompositePromotionVerdict:
    gates: list[GateResult] = []

    def gate(name: str, ok: bool, detail: str) -> None:
        gates.append(
            GateResult(gate=name, status="pass" if ok else "fail", detail=detail)
        )

    before_case = _case(before_fit, targeted_case_id)
    after_case = _case(after_fit, targeted_case_id)
    resolved = bool(
        before_case
        and after_case
        and before_case.undercall
        and not after_case.undercall
        and after_case.actual_class.value == "direct"
    )
    gate(
        "targeted_undercall_resolves",
        resolved,
        f"{targeted_case_id}: "
        f"{before_case.actual_class.value if before_case else 'absent'} -> "
        f"{after_case.actual_class.value if after_case else 'absent'}",
    )

    trap_results: list[CompositeTrapResult] = []
    for case_id in COMPOSITE_TRAP_CASES:
        result = _case(after_fit, case_id)
        if result is None:
            trap_results.append(
                CompositeTrapResult(
                    case_id=case_id,
                    expected_class="absent",
                    actual_class="absent",
                    passed=False,
                    capped=False,
                )
            )
            continue
        trap_results.append(
            CompositeTrapResult(
                case_id=case_id,
                expected_class=result.expected_class.value,
                actual_class=result.actual_class.value,
                passed=result.passed,
                capped=result.actual_class.value != "direct",
            )
        )
    traps_ok = all(t.passed and t.capped for t in trap_results)
    gate(
        "composite_traps_stay_capped",
        traps_ok,
        ", ".join(
            f"{t.case_id}:{t.actual_class}/passed={t.passed}/capped={t.capped}"
            for t in trap_results
        ),
    )

    gate(
        "direct_fp_after_zero",
        after_fit.ceiling_direct_fp == [],
        f"after deterministic direct FPs: {after_fit.ceiling_direct_fp or 'none'}",
    )
    gate(
        "gated_set_stays_green",
        after_fit.gated_passed,
        f"after.gated_passed={after_fit.gated_passed}",
    )
    gate(
        "off_label_direct_after_zero",
        after_sweep.off_label_direct == [],
        f"after off-label directs: {len(after_sweep.off_label_direct)}",
    )
    before_uc = set(before_fit.undercall_known_active) | set(
        before_fit.undercall_pending
    )
    after_uc = set(after_fit.undercall_known_active) | set(
        after_fit.undercall_pending
    )
    new_undercalls = sorted(after_uc - before_uc)
    gate(
        "no_collateral_undercall",
        not new_undercalls,
        f"new under-calls elsewhere: {new_undercalls or 'none'}",
    )
    gate(
        "no_model_owned_final_class",
        True,
        "deterministic eval harness; no model participates in the promotion",
    )
    gate(
        "no_auto_promotion",
        True,
        "verifier emits a verdict + artifact only; the default flip is a "
        "separate human/Loop-4 act",
    )
    axis_ok = (
        before_fit.gate_policy_version == after_fit.gate_policy_version
        and before_fit.token_rules_version == after_fit.token_rules_version
        and before_fit.alias_rules_version == after_fit.alias_rules_version
        and composite_after.version == COMPOSITE_COVERAGE_VERSION
    )
    gate(
        "no_laundering",
        axis_ok,
        "only the composite sidecar axis moved; gate/token/alias unchanged "
        f"(alias={after_fit.alias_rules_version}, "
        f"composite=disabled->{composite_after.version})",
    )

    safety = [g for g in gates if g.gate != "targeted_undercall_resolves"]
    safety_ok = all(g.status == "pass" for g in safety)
    if safety_ok and resolved:
        verdict = "go"
    elif safety_ok:
        verdict = "candidate_only"
    else:
        verdict = "no_go"

    return CompositePromotionVerdict(
        composite_before="disabled",
        composite_after=composite_after.version,
        verdict=verdict,
        gates=gates,
        blocking_gates=[g.gate for g in gates if g.status == "fail"],
        delta=CompositePromotionDelta(
            targeted_case_id=targeted_case_id,
            targeted_undercall_resolved=resolved,
            targeted_before_class=(
                before_case.actual_class.value if before_case else None
            ),
            targeted_after_class=(
                after_case.actual_class.value if after_case else None
            ),
            trap_results=trap_results,
            direct_fp_before=before_fit.ceiling_direct_fp,
            direct_fp_after=after_fit.ceiling_direct_fp,
            off_label_direct_before=len(before_sweep.off_label_direct),
            off_label_direct_after=len(after_sweep.off_label_direct),
            gated_passed_before=before_fit.gated_passed,
            gated_passed_after=after_fit.gated_passed,
            new_undercalls=new_undercalls,
            metrics_before_all=before_fit.metrics_all,
            metrics_after_all=after_fit.metrics_all,
            metrics_before_gated=before_fit.metrics_gated,
            metrics_after_gated=after_fit.metrics_gated,
        ),
    )


def write_promotion_artifact(verdict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdict.model_dump(), indent=2) + "\n")


# === M1 direction-token floor guard (governed Step 5 / Loop 4) ============
# A DIFFERENT axis from the alias table: this tightens M1's OVERLAP path
# (CHANGE_DIRECTION_TOKENS subtracted before the floor clears), so a shared
# "decline" alone no longer passes. Same eval-delta discipline, but the gain
# is OVER-call reduction (false-directs -> weak_proxy), shown on aligned
# adversarial probes; the fixture eval proves no regression. The "before"
# baseline is the guard OFF (the leak present), not an alias change — so the
# laundering gate here asserts the alias/gate/token axes did NOT move.

_M1_GUARD_REJECT_METRICS = (
    "market share decline",
    "patent count decline",
    "employee headcount decline",
    "GPU shipment decline",
    "championship trophies decline",
    "share price decline",
    "sales volume decline",
    "unit sales decline",
)
_M1_GUARD_MARKET_METRIC = "revenue decline"
_M1_GUARD_PRESERVE_METRIC = "sales contraction"  # rt_003 alias path, must hold


def _m1_probe_pair(claim_metric: str):
    """Claim/market aligned on every axis EXCEPT the metric, so M1 is the
    sole differentiator (isolates the floor decision). Model-free."""
    from datetime import date

    from el.domain.enums import EventStage, ResolutionSourceClass, Stance
    from el.domain.structures import (
        ClaimHorizon,
        Entity,
        ExtractedStructure,
        MarketHorizon,
        MarketStructure,
        Mechanism,
        Metric,
    )

    claim = ExtractedStructure(
        claim_summary="m1-guard probe",
        entities=[Entity(name="Acme", role="subject")],
        event_stage=EventStage.MEASURED,
        metric=Metric(what=claim_metric, measured_by="filings", objective=True),
        horizon=ClaimHorizon(window_end=date(2026, 12, 31), precision="year"),
        mechanism=Mechanism(),
        stance=Stance.DECREASE,
        resolution_source_class=ResolutionSourceClass.FILING,
        contractible_version="probe",
    )
    market = MarketStructure(
        market_id="mkt_rev_decline",
        snapshot_id="snap_probe",
        event_stage=EventStage.MEASURED,
        metric=Metric(
            what=_M1_GUARD_MARKET_METRIC, measured_by="filings", objective=True
        ),
        horizon=MarketHorizon(resolution_date=date(2026, 12, 31)),
        entities=[Entity(name="Acme", role="subject")],
        threshold="decline vs 2025",
        direction="decline",
        resolution_source_class=ResolutionSourceClass.FILING,
    )
    return claim, market


class M1GuardVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verifier_version: str = PROMOTION_VERIFIER_VERSION
    guard_version: str
    guard_before: bool
    guard_after: bool
    verdict: str  # "go" | "no_go"
    gates: list[GateResult]
    blocking_gates: list[str]
    targeted_false_directs_resolved: list[str]  # direct (off) -> weak_proxy (on)
    preserved_clears: list[str]  # rt_003 alias path stays direct


def run_m1_guard_verifier(*, paths: list | None = None) -> M1GuardVerdict:
    """Real gate before (guard off) / after (guard on), then shipping gates."""
    from el.domain.enums import FitClass
    from el.fitgate.checks import M1_FLOOR_GUARD_VERSION
    from el.fitgate.policy import FitPolicy, evaluate_market

    paths = paths or list(_default_paths())
    before = FitPolicy(m1_direction_guard=False)  # leak present
    after = FitPolicy(m1_direction_guard=True)  # guard on (current default)

    before_fit = run_fit_eval(*paths, policy=before)
    after_fit = run_fit_eval(*paths, policy=after)
    before_sweep = run_pool_sweep(*paths, policy=before)
    after_sweep = run_pool_sweep(*paths, policy=after)

    gates: list[GateResult] = []

    def gate(name: str, ok: bool, detail: str) -> None:
        gates.append(
            GateResult(gate=name, status="pass" if ok else "fail", detail=detail)
        )

    # Gain: aligned adversarial false-directs resolve direct -> weak_proxy.
    resolved: list[str] = []
    for metric in _M1_GUARD_REJECT_METRICS:
        claim, market = _m1_probe_pair(metric)
        before_cls = evaluate_market(claim, market, before).published
        after_cls = evaluate_market(claim, market, after).published
        if before_cls is FitClass.DIRECT and after_cls is FitClass.WEAK_PROXY:
            resolved.append(metric)
    gate(
        "targeted_false_directs_resolve",
        len(resolved) == len(_M1_GUARD_REJECT_METRICS),
        f"{len(resolved)}/{len(_M1_GUARD_REJECT_METRICS)} direct->weak_proxy: "
        f"{resolved}",
    )

    # Preserve: rt_003's alias path still clears to direct under the guard.
    pclaim, pmarket = _m1_probe_pair(_M1_GUARD_PRESERVE_METRIC)
    preserved_ok = evaluate_market(pclaim, pmarket, after).published is (
        FitClass.DIRECT
    )
    gate(
        "preserves_alias_clear",
        preserved_ok,
        f"{_M1_GUARD_PRESERVE_METRIC!r} stays direct under the guard "
        "(alias path unaffected)",
    )

    # Safety on the fixture set: floor tightening must not regress anything.
    new_direct_fp = sorted(
        set(after_fit.ceiling_direct_fp) - set(before_fit.ceiling_direct_fp)
    )
    gate(
        "no_new_direct_fp",
        not new_direct_fp,
        f"new deterministic direct FPs: {new_direct_fp or 'none'}",
    )
    gate(
        "gated_set_stays_green",
        after_fit.gated_passed,
        f"after.gated_passed={after_fit.gated_passed} (incl. eval_009 indirect)",
    )
    new_off = sorted(
        set(after_sweep.off_label_direct) - set(before_sweep.off_label_direct)
    )
    gate(
        "no_new_off_label_direct",
        not new_off,
        f"new off-label directs: {new_off or 'none'} "
        f"(off-label {len(before_sweep.off_label_direct)} -> "
        f"{len(after_sweep.off_label_direct)})",
    )
    before_uc = set(before_fit.undercall_known_active) | set(
        before_fit.undercall_pending
    )
    after_uc = set(after_fit.undercall_known_active) | set(
        after_fit.undercall_pending
    )
    new_uc = sorted(after_uc - before_uc)
    gate(
        "no_collateral_undercall",
        not new_uc,
        f"new under-calls (floor over-refusal): {new_uc or 'none'}",
    )
    gate(
        "no_model_owned_final_class",
        True,
        "deterministic eval harness; no model participates in the promotion",
    )
    gate(
        "no_auto_promotion",
        True,
        "verifier emits a verdict + artifact only; the default flip is a "
        "separate human/Loop-4 act",
    )
    axis_ok = (
        before_fit.alias_rules_version == after_fit.alias_rules_version
        and before_fit.gate_policy_version == after_fit.gate_policy_version
        and before_fit.token_rules_version == after_fit.token_rules_version
    )
    gate(
        "no_laundering",
        axis_ok,
        "only the M1 direction-guard axis moved; alias/gate/token unchanged "
        f"(alias={after_fit.alias_rules_version}, "
        f"gate={after_fit.gate_policy_version})",
    )

    blocking = [g.gate for g in gates if g.status == "fail"]
    return M1GuardVerdict(
        guard_version=M1_FLOOR_GUARD_VERSION,
        guard_before=False,
        guard_after=True,
        verdict="go" if not blocking else "no_go",
        gates=gates,
        blocking_gates=blocking,
        targeted_false_directs_resolved=resolved,
        preserved_clears=[_M1_GUARD_PRESERVE_METRIC] if preserved_ok else [],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a promotion verifier.")
    parser.add_argument("--case", default="rt_003_synonym_undercall")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--m1-guard",
        action="store_true",
        help="run the M1 direction-token floor-guard verifier instead",
    )
    mode.add_argument(
        "--composite-coverage",
        action="store_true",
        help="run the composite-v2 sidecar coverage verifier instead",
    )
    parser.add_argument(
        "--composite-path",
        help="override the composite-v2 sidecar fixture path",
    )
    parser.add_argument("--write", action="store_true", help="write the artifact")
    args = parser.parse_args()

    promotions = Path(__file__).resolve().parents[2] / "evals" / "promotions"
    if args.composite_coverage:
        result = run_composite_coverage_verifier(
            composite_path=args.composite_path
        )
        print(
            f"composite coverage: {result.verdict.upper()} "
            f"({result.composite_before} -> {result.composite_after})"
        )
        for g in result.gates:
            print(f"  [{g.status:>4}] {g.gate}: {g.detail}")
        if args.write:
            out = promotions / "rt_004_composite_coverage_v2.json"
            write_promotion_artifact(result, out)
            print(f"wrote {out}")
    elif args.m1_guard:
        guard = run_m1_guard_verifier()
        print(
            f"M1 direction-guard: {guard.verdict.upper()} "
            f"(guard {guard.guard_before} -> {guard.guard_after})"
        )
        for g in guard.gates:
            print(f"  [{g.status:>4}] {g.gate}: {g.detail}")
        if args.write:
            out = promotions / "m1_direction_token_floor_guard_v1.json"
            write_promotion_artifact(guard, out)
            print(f"wrote {out}")
    else:
        result = run_promotion_verifier(targeted_case_id=args.case)
        print(
            f"promotion {args.case}: {result.verdict.upper()} "
            f"({result.alias_before} -> {result.alias_after})"
        )
        for g in result.gates:
            print(f"  [{g.status:>4}] {g.gate}: {g.detail}")
        if args.write:
            out = promotions / f"{args.case}_{result.alias_after}.json"
            write_promotion_artifact(result, out)
            print(f"wrote {out}")
