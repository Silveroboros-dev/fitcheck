"""Candidate generation — gate signals -> review candidates (Loop 4 intake).

Wires the previously-dead "improvement" half of the loop: turns the eval
harness's OWN signals into ReviewCandidate specs, deterministically and
model-free. Two sources in the thin slice:
- FitEvalReport.undercall_known_active  (confirmed under-calls; e.g. rt_003)
- PoolSweepReport.off_label_direct      (bad-twin pressure, label-free)

The fingerprint binds the FULL governing context (gate / token / alias /
eval-pack / extraction-schema / market-structure-schema versions) so a
candidate generated under one policy generation never silently dedupes
against another. Run-invariant: no clocks, no UUIDs here — those belong to
the store boundary (el.review.store). Structured learning evidence lives in
the governance JSONL + promotion JSON, never stuffed into reviewer_notes.
"""

import hashlib

from pydantic import BaseModel, ConfigDict

from el.evals.fit import FitEvalReport, PoolSweepReport
from el.fitgate.checks import TOKEN_RULES_VERSION
from el.fitgate.policy import FIT_GATE_POLICY_VERSION

REVIEW_GEN_VERSION = "review-gen-v1"

FAMILY_UNDERCALL_KNOWN = "undercall_known_active"
FAMILY_OFF_LABEL_DIRECT = "off_label_direct"

# Governing-context constants stamped into the fingerprint. The gate/token/
# alias versions that actually move the verdict are taken live (alias from
# the report); these three are stable v1 constants (extraction + market-
# structure schema = 1; eval pack = phase0-fit-v1). Bumping any is a
# deliberate edit that changes every fingerprint — correct.
EVAL_PACK_VERSION = "phase0-fit-v1"
EXTRACTION_SCHEMA_VERSION = 1
MARKET_STRUCTURE_SCHEMA_VERSION = 1


class ReviewCandidateSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    object_type: str  # "fit_case" | "claim_market_pair"
    object_ref: str  # case_id, or "case_id::market_id" (the sweep key)
    source: str  # ReviewSource value
    failure_family: str  # the cluster key
    signal_detail: str  # human-readable, for reviewer_notes
    fingerprint: str  # sha256 over the full governing context


def candidate_fingerprint(family: str, ref: str, alias_version: str) -> str:
    """sha256 over the full governing context (versions) for dedupe."""
    context = "|".join(
        [
            family,
            ref,
            FIT_GATE_POLICY_VERSION,
            TOKEN_RULES_VERSION,
            alias_version,
            EVAL_PACK_VERSION,
            str(EXTRACTION_SCHEMA_VERSION),
            str(MARKET_STRUCTURE_SCHEMA_VERSION),
        ]
    )
    return hashlib.sha256(context.encode()).hexdigest()


def candidates_from_fit_report(
    report: FitEvalReport, source: str
) -> list[ReviewCandidateSpec]:
    """Confirmed under-calls (truth-labeled, gate under-calls) — the
    highest-value candidate: a real gap with a known-correct answer."""
    specs = []
    for case_id in report.undercall_known_active:
        specs.append(
            ReviewCandidateSpec(
                object_type="fit_case",
                object_ref=case_id,
                source=source,
                failure_family=FAMILY_UNDERCALL_KNOWN,
                signal_detail=(
                    f"{case_id}: truth-labeled but the gate under-calls "
                    "(known_undercall_v1) — a confirmed metric/structure gap"
                ),
                fingerprint=candidate_fingerprint(
                    FAMILY_UNDERCALL_KNOWN, case_id, report.alias_rules_version
                ),
            )
        )
    return specs


def candidates_from_pool_sweep(
    report: PoolSweepReport, source: str
) -> list[ReviewCandidateSpec]:
    """Off-label ceiling-direct pairs — label-free bad-twin pressure: any
    pair the gate calls direct against a market that is not its expected
    recommendation is review-candidate material (never auto-accepted)."""
    specs = []
    for ref in report.off_label_direct:
        specs.append(
            ReviewCandidateSpec(
                object_type="claim_market_pair",
                object_ref=ref,
                source=source,
                failure_family=FAMILY_OFF_LABEL_DIRECT,
                signal_detail=(
                    f"{ref}: gate ceiling reached direct on an off-label "
                    "market (bad-twin pressure)"
                ),
                fingerprint=candidate_fingerprint(
                    FAMILY_OFF_LABEL_DIRECT, ref, report.alias_rules_version
                ),
            )
        )
    return specs


def collect_review_candidates(
    fit_report: FitEvalReport,
    sweep_report: PoolSweepReport,
    source: str,
) -> list[ReviewCandidateSpec]:
    """All candidates from both signals, deduped by fingerprint, sorted
    deterministically (run-invariant)."""
    specs = candidates_from_fit_report(
        fit_report, source
    ) + candidates_from_pool_sweep(sweep_report, source)
    seen: dict[str, ReviewCandidateSpec] = {}
    for spec in specs:
        seen.setdefault(spec.fingerprint, spec)
    return sorted(
        seen.values(), key=lambda s: (s.failure_family, s.object_ref)
    )
