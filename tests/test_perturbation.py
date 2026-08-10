"""Learning Loop v0 — perturbation robustness (anti-bad-twin + anti-brittle)."""

from datetime import date
from pathlib import Path

from el.domain.enums import FitClass
from el.domain.structures import (
    ClaimHorizon,
    Entity,
    ExtractedStructure,
    MarketHorizon,
    MarketStructure,
    Mechanism,
    Metric,
)
from el.review.perturbation import (
    FAMILY_BRITTLE,
    FAMILY_SURVIVOR,
    PerturbationResult,
    perturb_and_check,
    perturbation_candidates,
)


def _direct_pair():
    claim = ExtractedStructure(
        claim_summary="Acme ships Product X by 2026.",
        entities=[
            Entity(name="Acme", role="subject"),
            Entity(name="Product X", role="object"),
        ],
        event_stage="launched",
        metric=Metric(
            what="public release of Product X", measured_by="Acme", objective=True
        ),
        horizon=ClaimHorizon(window_end=date(2026, 12, 31), precision="day"),
        mechanism=Mechanism(),
        stance="yes",
        resolution_source_class="press",
        contractible_version="Will Acme publicly release Product X by Dec 31, 2026?",
    )
    market = MarketStructure(
        market_id="mkt_acme",
        snapshot_id="snap",
        event_stage="launched",
        metric=Metric(
            what="public release of Product X", measured_by="Acme", objective=True
        ),
        horizon=MarketHorizon(resolution_date=date(2026, 12, 31)),
        entities=[Entity(name="Acme", role="subject")],
        resolution_source_class="press",
        extraction_policy_version=2,
    )
    return claim, market


def test_genuine_direct_demotes_on_relevant_stable_on_irrelevant():
    claim, market = _direct_pair()
    results = perturb_and_check(claim, market)
    assert results, "base should be direct/indirect"
    relevant = [r for r in results if r.kind == "relevant"]
    irrelevant = [r for r in results if r.kind == "irrelevant"]
    assert relevant and irrelevant
    assert all(r.demoted for r in relevant)  # anti-bad-twin: relevant must bite
    assert all(r.stable for r in irrelevant)  # anti-brittle: irrelevant must not
    assert not any(r.is_anomaly for r in results)  # genuine fit -> no anomalies
    assert perturbation_candidates(results, "mkt_acme", "alias-v1") == []


def test_weak_base_has_nothing_to_perturb():
    claim, market = _direct_pair()
    broken = claim.model_copy(
        update={
            "metric": claim.metric.model_copy(
                update={"what": "wholly unrelated quantity"}
            )
        }
    )
    assert perturb_and_check(broken, market) == []


def test_anomaly_maps_to_candidate_family():
    # The harness can't easily synthesize a real gate weakness; assert the
    # candidate builder maps each anomaly kind to the right family.
    survivor = PerturbationResult(
        perturbation="swap_metric_token",
        kind="relevant",
        base_class=FitClass.DIRECT,
        perturbed_class=FitClass.DIRECT,
        demoted=False,
        stable=True,
        is_anomaly=True,
    )
    brittle = PerturbationResult(
        perturbation="recase_metric",
        kind="irrelevant",
        base_class=FitClass.DIRECT,
        perturbed_class=FitClass.WEAK_PROXY,
        demoted=True,
        stable=False,
        is_anomaly=True,
    )
    specs = perturbation_candidates([survivor, brittle], "mkt_x", "alias-v1")
    assert {s.failure_family for s in specs} == {FAMILY_SURVIVOR, FAMILY_BRITTLE}


def test_perturbation_module_is_model_free():
    import el.review.perturbation as module

    source = Path(module.__file__).read_text()
    assert "el.models" not in source
    assert "genai" not in source
