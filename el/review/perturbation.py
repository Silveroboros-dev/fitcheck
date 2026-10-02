"""Perturbation robustness — label-free anti-bad-twin + anti-brittleness signal.

Takes a (claim, market) the gate called direct/indirect and applies ONE
minimal structure perturbation, then compares the published class. Two
complementary properties, no truth label consulted:

- RELEVANT perturbation must DEMOTE (anti-bad-twin / Deutsch hard-to-vary):
  swap the metric to something disjoint (should trip M1), shift the event
  stage (should trip S1). A *survivor* (did not demote) = a weak proxy that
  the gate over-blessed -> candidate (family "perturbation_survivor").
- IRRELEVANT perturbation must stay STABLE (anti-brittleness): recase /
  pad the metric (tok_v1 normalizes -> no change), reword the free-text
  claim summary (the checks never read it). A *spurious demotion* = an
  over-reactive gate -> candidate (family "perturbation_brittle").

A demotion-only test set can hide brittleness; a stability-only set can
hide a dull gate. We test both. Pure, model-free; structures are frozen so
perturbations use model_copy, never mutate.
"""

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from el.domain.enums import EventStage, FitClass
from el.domain.structures import ExtractedStructure, MarketStructure
from el.fitgate.m1_subject_only import evaluate_market
from el.fitgate.policy import FitPolicy, class_rank
from el.review.candidates import ReviewCandidateSpec, candidate_fingerprint

PERTURBATION_VERSION = "perturb-v1"

FAMILY_SURVIVOR = "perturbation_survivor"
FAMILY_BRITTLE = "perturbation_brittle"

_Pair = tuple[ExtractedStructure, MarketStructure]
_PerturbFn = Callable[[ExtractedStructure, MarketStructure], _Pair]


def _swap_metric_token(claim: ExtractedStructure, market: MarketStructure) -> _Pair:
    disjoint = claim.metric.model_copy(
        update={"what": "zzqx wholly unrelated nonsense quantity"}
    )
    return claim.model_copy(update={"metric": disjoint}), market


def _shift_event_stage(claim: ExtractedStructure, market: MarketStructure) -> _Pair:
    stages = list(EventStage)
    nxt = stages[(stages.index(claim.event_stage) + 1) % len(stages)]
    return claim.model_copy(update={"event_stage": nxt}), market


def _recase_metric(claim: ExtractedStructure, market: MarketStructure) -> _Pair:
    # casefold + whitespace are normalized by tok_v1 -> same tokens.
    recased = claim.metric.model_copy(
        update={"what": claim.metric.what.upper() + "   "}
    )
    return claim.model_copy(update={"metric": recased}), market


def _reword_summary(claim: ExtractedStructure, market: MarketStructure) -> _Pair:
    # claim_summary is free text the deterministic checks never read.
    return (
        claim.model_copy(
            update={"claim_summary": f"(reworded) {claim.claim_summary}"}
        ),
        market,
    )


_RELEVANT: tuple[tuple[str, _PerturbFn], ...] = (
    ("swap_metric_token", _swap_metric_token),
    ("shift_event_stage", _shift_event_stage),
)
_IRRELEVANT: tuple[tuple[str, _PerturbFn], ...] = (
    ("recase_metric", _recase_metric),
    ("reword_summary", _reword_summary),
)


class PerturbationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    perturbation: str
    kind: str  # "relevant" | "irrelevant"
    base_class: FitClass
    perturbed_class: FitClass
    demoted: bool
    stable: bool
    is_anomaly: bool  # survivor (relevant) or brittle (irrelevant)


def perturb_and_check(
    claim: ExtractedStructure,
    market: MarketStructure,
    policy: FitPolicy = FitPolicy(),
) -> list[PerturbationResult]:
    """Intended for a base the gate called direct/indirect (a weaker base
    has nothing to demote). Returns [] otherwise."""
    base = evaluate_market(claim, market, policy).deterministic_ceiling
    if base not in (FitClass.DIRECT, FitClass.INDIRECT):
        return []
    results: list[PerturbationResult] = []
    for kind, perturbations in (("relevant", _RELEVANT), ("irrelevant", _IRRELEVANT)):
        for name, fn in perturbations:
            claim2, market2 = fn(claim, market)
            perturbed = evaluate_market(claim2, market2, policy).deterministic_ceiling
            demoted = class_rank(perturbed) < class_rank(base)
            stable = perturbed == base
            anomaly = (not demoted) if kind == "relevant" else (not stable)
            results.append(
                PerturbationResult(
                    perturbation=name,
                    kind=kind,
                    base_class=base,
                    perturbed_class=perturbed,
                    demoted=demoted,
                    stable=stable,
                    is_anomaly=anomaly,
                )
            )
    return results


def perturbation_candidates(
    results: list[PerturbationResult],
    object_ref: str,
    alias_version: str,
) -> list[ReviewCandidateSpec]:
    specs: list[ReviewCandidateSpec] = []
    for result in results:
        if not result.is_anomaly:
            continue
        family = (
            FAMILY_SURVIVOR if result.kind == "relevant" else FAMILY_BRITTLE
        )
        ref = f"{object_ref}::{result.perturbation}"
        detail = (
            f"{ref}: {result.kind} perturbation {result.base_class.value}->"
            f"{result.perturbed_class.value} "
            + (
                "survived (should have demoted) — over-blessed weak proxy"
                if family == FAMILY_SURVIVOR
                else "demoted spuriously (should have been stable) — brittle gate"
            )
        )
        specs.append(
            ReviewCandidateSpec(
                object_type="claim_market_pair",
                object_ref=ref,
                source="eval_failure",
                failure_family=family,
                signal_detail=detail,
                fingerprint=candidate_fingerprint(family, ref, alias_version),
            )
        )
    return specs
