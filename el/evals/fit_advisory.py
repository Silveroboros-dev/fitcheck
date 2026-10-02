"""Mode 3 — advisory agreement harness (off-CI for live models; §14).

Runs the same labeled cases through an ADVISORY proposer and the
demotion-only merge gate, measuring what the advisory layer adds or
costs relative to the deterministic ceiling. This is arm A of H-P2 —
the single-call baseline the Program 2 lens panel must beat.

Metrics:
- agreement: advisory-mapped class == deterministic ceiling per pair;
- published_false_strong: ground-truth weak/no-clean theses published
  as direct/indirect with confidence >= 0.7 — MUST be empty, and is
  structurally near-impossible (published = min(ceiling, advisory);
  the gated ceiling already reproduces those labels);
- veto/disagreement rates (escalation base-rate inputs).

The proposer is INJECTED — this module never constructs a live client;
CI uses FixtureFitProposer, live runs build their own proposer behind
an env key. Separate from el.evals.fit so the deterministic harness
keeps its hard structural isolation from model adapters.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from el.domain.enums import FitClass
from el.evals.fit import load_fit_cases, load_market_registry
from el.domain.structures import ExtractedStructure
from el.fitgate.gate import merge_advisory, quote_span_violations
from el.fitgate.m1_subject_only import evaluate_market
from el.fitgate.policy import (
    STRONG_CLASSES,
    WEAK_CLASSES,
    FitPolicy,
    class_rank,
)
from el.models.fit_adapter import FitAdvisoryProposer

FALSE_STRONG_CONFIDENCE_FLOOR = 0.7


def _claim_corpus(structure: ExtractedStructure) -> str:
    """Claim-side text an advisory may quote from (mirrors the service)."""
    parts = [
        structure.claim_summary,
        structure.contractible_version,
        structure.metric.what,
        structure.metric.measured_by,
        *[entity.name for entity in structure.entities],
    ]
    return " ".join(part for part in parts if part)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AdvisoryPairResult(_Model):
    case_id: str
    market_id: str
    ceiling: FitClass
    advisory_class: FitClass | None
    suggested_class: FitClass | None
    published: FitClass
    confidence: float | None
    accepted: bool
    vetoed: bool
    agreement: bool | None


class AdvisoryAgreementReport(_Model):
    pairs: list[AdvisoryPairResult]
    pairs_total: int
    accepted_total: int
    # Share of pairs whose advisory was ACCEPTED (complete + citation-bearing,
    # not rejected/fallback) — the quality metric, distinct from the
    # false-strong safety check.
    accepted_rate: float
    agreement_rate: float
    veto_rate: float
    published_false_strong: list[str]  # "case_id::market_id" — must be []
    passed: bool


def run_advisory_agreement(
    eval_set_path: str | Path,
    golden_claims_path: str | Path,
    golden_markets_path: str | Path,
    el_native_path: str | Path,
    proposer: FitAdvisoryProposer,
    policy: FitPolicy = FitPolicy(),
    rules_text_by_market: dict[str, str] | None = None,
    enforce_citations: bool = True,
) -> AdvisoryAgreementReport:
    """Arm A baseline. `rules_text_by_market` supplies captured rules text
    so the advisory can be citation-bearing (live runs pass the frozen
    snapshot's rules); `enforce_citations` rejects advisories whose
    evidence is not a verbatim quote (a non-citing advisory falls back to
    the deterministic ceiling). Fixtures are generic, so the CI plumbing
    test runs with enforce_citations=False; the live run enforces."""
    rules_text_by_market = rules_text_by_market or {}
    registry = load_market_registry(golden_markets_path, el_native_path)
    cases = load_fit_cases(eval_set_path, golden_claims_path, el_native_path)

    weak_truth = WEAK_CLASSES
    strong = STRONG_CLASSES
    pairs: list[AdvisoryPairResult] = []
    false_strong: list[str] = []

    for case in cases:
        for market_id in case.pool_market_ids:
            market = registry[market_id]
            rules_text = rules_text_by_market.get(market_id, "")
            deterministic = evaluate_market(case.structure, market, policy)
            try:
                result = proposer.propose_fit(
                    market_id=market_id,
                    snapshot_id=market.snapshot_id,
                    claim_structure=case.structure,
                    market_structure=market,
                    input_text=case.structure.claim_summary,
                    contract_terms_text="",
                    resolution_rules_text=rules_text,
                )
                advisory = result.advisory
            except Exception:
                advisory = None
            if advisory is not None and enforce_citations:
                if quote_span_violations(
                    advisory,
                    claim_text=_claim_corpus(case.structure),
                    rules_text=rules_text,
                ):
                    advisory = None  # non-citing -> falls to the ceiling
            merge = merge_advisory(
                deterministic, advisory, market_id=market_id, policy=policy
            )
            verdict = merge.verdict
            confidence = advisory.confidence if merge.accepted else None
            pairs.append(
                AdvisoryPairResult(
                    case_id=case.case_id,
                    market_id=market_id,
                    ceiling=deterministic.deterministic_ceiling,
                    advisory_class=merge.advisory_class,
                    suggested_class=merge.suggested_class,
                    published=verdict.published,
                    confidence=confidence,
                    accepted=merge.accepted,
                    vetoed=class_rank(verdict.published)
                    < class_rank(deterministic.deterministic_ceiling),
                    agreement=(
                        merge.advisory_class
                        == deterministic.deterministic_ceiling
                        if merge.accepted
                        else None
                    ),
                )
            )
            if (
                case.expected_class in weak_truth
                and verdict.published in strong
                and confidence is not None
                and confidence >= FALSE_STRONG_CONFIDENCE_FLOOR
            ):
                false_strong.append(f"{case.case_id}::{market_id}")

    accepted = [p for p in pairs if p.accepted]
    return AdvisoryAgreementReport(
        pairs=pairs,
        pairs_total=len(pairs),
        accepted_total=len(accepted),
        accepted_rate=(len(accepted) / len(pairs) if pairs else 0.0),
        agreement_rate=(
            sum(1 for p in accepted if p.agreement) / len(accepted)
            if accepted
            else 0.0
        ),
        veto_rate=(
            sum(1 for p in accepted if p.vetoed) / len(accepted)
            if accepted
            else 0.0
        ),
        published_false_strong=false_strong,
        passed=not false_strong,
    )


def rules_text_from_snapshot(snapshot_path: str | Path) -> dict[str, str]:
    """market_id -> resolution rules text, from a frozen snapshot file.
    Lets the live mode-3 run pass captured rules so the advisory can cite
    (markets absent from the snapshot simply get no rules text)."""
    import json

    out: dict[str, str] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            mid = node.get("market_id")
            rules = node.get("resolution_rules") or node.get("description")
            if mid and rules:
                out[mid] = rules
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(Path(snapshot_path).read_text()))
    return out


if __name__ == "__main__":
    # Non-live demo so `python -m el.evals.fit_advisory` is not silent.
    # Fixtures are generic and cannot cite, so citations are off here; the
    # citation-bearing baseline is scripts/run_fit_advisory_live.py.
    import json

    from el.evals.fit import _default_paths
    from el.models.fit_adapter import (
        REQUIRED_ADVISORY_CONDITIONS,
        ConditionVerdict,
        FitAdvisory,
        FixtureFitProposer,
    )

    def _demo_advisory(market_id: str) -> FitAdvisory:
        return FitAdvisory(
            market_id=market_id,
            condition_verdicts=[
                ConditionVerdict(
                    condition=condition,
                    status="pass",
                    claim_evidence="(demo)",
                    market_evidence="(demo)",
                )
                for condition in sorted(REQUIRED_ADVISORY_CONDITIONS)
            ],
            bridge_assumptions=[],
            falsifier="demo",
            suggested_class=FitClass.DIRECT,
            what_it_captures="demo",
            what_it_misses="demo",
            confidence=0.8,
        )

    eval_set, claims, markets, native = _default_paths()
    ids = {
        s["market_id"]
        for s in json.loads(Path(markets).read_text())["structures"]
    } | {
        m["market_id"] for m in json.loads(Path(native).read_text())["markets"]
    }
    report = run_advisory_agreement(
        eval_set,
        claims,
        markets,
        native,
        proposer=FixtureFitProposer({i: _demo_advisory(i) for i in ids}),
        enforce_citations=False,
    )
    print(
        f"mode-3 fixture demo (enforce_citations=False): "
        f"pairs={report.pairs_total} accepted={report.accepted_total} "
        f"accepted_rate={report.accepted_rate:.0%} "
        f"agreement={report.agreement_rate:.0%} "
        f"false_strong={report.published_false_strong} PASSED={report.passed}"
    )
    print(
        "citation-bearing baseline: scripts/run_fit_advisory_live.py "
        "(live model, rules wired, enforce_citations=True)"
    )
