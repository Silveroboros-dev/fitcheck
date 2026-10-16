"""Public synthetic coverage for immutable fit-policy runtime data (FC-R04)."""

from dataclasses import asdict
from datetime import date
import inspect
import json
import pickle

import pytest

from el.domain.structures import ExtractedStructure, MarketStructure
from el.fitgate.composite_v2 import CompositeCoveragePolicy
from el.fitgate.service import FitService


def _composite_case():
    claim = ExtractedStructure.model_validate(
        {
            "claim_summary": "Acme quarterly revenue declines by 2026",
            "contractible_version": "Acme quarterly revenue declines by 2026",
            "entities": [{"name": "Acme", "role": "subject"}],
            "event_stage": "measured",
            "metric": {
                "what": "quarterly revenue",
                "measured_by": "filings",
                "objective": True,
            },
            "horizon": {
                "window_end": date(2026, 12, 31),
                "precision": "year",
            },
            "mechanism": {"is_composite": True},
            "stance": "decrease",
            "resolution_source_class": "filing",
        }
    )
    market = MarketStructure.model_validate(
        {
            "market_id": "market",
            "snapshot_id": "snapshot",
            "entities": [{"name": "Acme", "role": "subject"}],
            "event_stage": "measured",
            "metric": {
                "what": "quarterly revenue",
                "measured_by": "filings",
                "objective": True,
            },
            "horizon": {"resolution_date": date(2026, 12, 31)},
            "direction": "decline",
            "resolution_source_class": "filing",
        }
    )
    coverage = CompositeCoveragePolicy.model_validate(
        {
            "claims": {
                claim.claim_summary: {
                    "claim_summary": claim.claim_summary,
                    "operator": "all",
                    "legs": [
                        {
                            "leg_id": "claim-leg",
                            "entities": claim.entities,
                            "event_stage": claim.event_stage,
                            "metric": claim.metric,
                            "horizon": claim.horizon,
                            "direction": "decline",
                            "stance": claim.stance,
                            "resolution_source_class": (
                                claim.resolution_source_class
                            ),
                        }
                    ],
                }
            },
            "markets": {
                market.market_id: {
                    "market_id": market.market_id,
                    "operator": "all",
                    "legs": [
                        {
                            "leg_id": "market-leg",
                            "entities": market.entities,
                            "event_stage": market.event_stage,
                            "metric": market.metric,
                            "horizon": market.horizon,
                            "direction": market.direction,
                            "resolution_source_class": (
                                market.resolution_source_class
                            ),
                        }
                    ],
                }
            },
        }
    )
    return claim, market, coverage


def test_default_services_reject_shared_nested_mutation():
    first, second = FitService(None, None), FitService(None, None)
    original = second._policy.horizon_tolerances["day"]
    with pytest.raises(TypeError, match="immutable"):
        first._policy.horizon_tolerances["day"] = (999, 999)
    assert second._policy.horizon_tolerances["day"] == original
    assert first._policy is not second._policy
    assert first._policy.horizon_tolerances is not second._policy.horizon_tolerances


@pytest.mark.parametrize(
    "operation",
    [
        lambda values: values.update(day=(999, 999)),
        lambda values: values.pop("day"),
        lambda values: values.clear(),
        lambda values: values.setdefault("new", (1, 2)),
        lambda values: values.__delitem__("day"),
        lambda values: values.__ior__({"day": (999, 999)}),
        lambda values: values.__init__({"day": (999, 999)}),
    ],
)
def test_runtime_horizon_mapping_rejects_mutators(operation):
    from el.fitgate.runtime_policy import FitPolicy

    with pytest.raises(TypeError, match="immutable"):
        operation(FitPolicy().horizon_tolerances)


def test_explicit_legacy_policy_is_defensively_copied_at_service_boundary():
    from el.fitgate.policy import FitPolicy as LegacyFitPolicy

    source = LegacyFitPolicy(horizon_tolerances={"day": [7, 14]})
    first, second = FitService(None, None, source), FitService(None, None, source)
    source.horizon_tolerances["day"][0] = 999
    assert first._policy.horizon_tolerances["day"] == (7, 14)
    assert second._policy.horizon_tolerances["day"] == (7, 14)
    assert type(first._policy) is LegacyFitPolicy
    with pytest.raises(TypeError, match="immutable"):
        first._policy.horizon_tolerances["day"] = (999, 999)


def test_runtime_policy_pins_replay_with_same_identity_and_serialization():
    from el.fitgate.m1_subject_only import (
        fit_policy_from_pins,
        gate_policy_version,
        ordinary_discovery_fit_policy,
    )
    from el.fitgate.policy import FitPolicy as LegacyFitPolicy
    from el.fitgate.runtime_policy import FitPolicy

    assert json.dumps(asdict(FitPolicy()), sort_keys=True) == json.dumps(
        asdict(LegacyFitPolicy()), sort_keys=True
    )
    assert FitPolicy().horizon_tolerances is not FitPolicy().horizon_tolerances
    for policy in (FitPolicy(), ordinary_discovery_fit_policy()):
        pins = asdict(policy)
        pins.pop("composite_coverage")
        pins["gate_policy_version"] = gate_policy_version(policy)
        replayed = fit_policy_from_pins(**pins)
        assert gate_policy_version(replayed) == gate_policy_version(policy)
        assert asdict(replayed) == asdict(policy)
        with pytest.raises(TypeError, match="immutable"):
            replayed.horizon_tolerances["day"] = (999, 999)


@pytest.mark.parametrize("successor", [False, True], ids=["legacy", "successor"])
def test_composite_policy_is_detached_before_caller_mutation_can_change_verdict(
    successor,
):
    from el.fitgate.m1_subject_only import (
        M1SubjectOnlyFitPolicy,
        evaluate_market,
        gate_policy_version,
    )
    from el.fitgate.policy import FitPolicy as LegacyFitPolicy

    claim, market, coverage = _composite_case()
    policy_type = M1SubjectOnlyFitPolicy if successor else LegacyFitPolicy
    service = FitService(None, None, policy_type(composite_coverage=coverage))
    wire_before = service._policy.composite_coverage.model_dump(mode="json")
    identity_before = gate_policy_version(service._policy)
    verdict_before = evaluate_market(claim, market, service._policy)

    coverage.claims.pop(claim.claim_summary)
    coverage.markets.pop(market.market_id)

    assert evaluate_market(claim, market, service._policy) == verdict_before
    assert gate_policy_version(service._policy) == identity_before
    assert service._policy.composite_coverage.model_dump(mode="json") == wire_before


def test_every_nested_composite_policy_container_is_immutable():
    from el.fitgate.policy import FitPolicy as LegacyFitPolicy
    from el.fitgate.runtime_policy import immutable_fit_policy

    _, _, coverage = _composite_case()
    policy = immutable_fit_policy(LegacyFitPolicy(composite_coverage=coverage))
    frozen = policy.composite_coverage
    claim = next(iter(frozen.claims.values()))

    with pytest.raises(TypeError, match="immutable"):
        frozen.claims.pop(claim.claim_summary)
    with pytest.raises(TypeError, match="immutable"):
        claim.legs.append(claim.legs[0])
    with pytest.raises(TypeError, match="immutable"):
        claim.legs[0].entities.clear()


def test_deeply_frozen_policy_preserves_json_and_pickle_round_trips():
    from el.fitgate.m1_subject_only import M1SubjectOnlyFitPolicy
    from el.fitgate.runtime_policy import FitPolicy

    _, _, coverage = _composite_case()
    expected_wire = coverage.model_dump(mode="json")
    composite_policy = M1SubjectOnlyFitPolicy(composite_coverage=coverage)

    assert composite_policy.composite_coverage.model_dump(mode="json") == expected_wire
    assert json.loads(
        json.dumps(
            composite_policy.composite_coverage.model_dump(mode="json"),
            sort_keys=True,
        )
    ) == expected_wire

    for policy in (FitPolicy(), composite_policy):
        replayed = pickle.loads(pickle.dumps(policy))
        assert type(replayed) is type(policy)
        assert asdict(replayed) == asdict(policy)
        with pytest.raises(TypeError, match="immutable"):
            replayed.horizon_tolerances["day"] = (999, 999)
        if replayed.composite_coverage is not None:
            with pytest.raises(TypeError, match="immutable"):
                replayed.composite_coverage.claims.clear()


def test_current_call_boundaries_do_not_construct_policy_in_default_arguments():
    from el.fitgate.gate import merge_advisory
    from el.fitgate.m1_subject_only import evaluate_market

    for call in (FitService, evaluate_market, merge_advisory):
        assert inspect.signature(call).parameters["policy"].default is None
