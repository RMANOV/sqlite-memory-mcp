from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lazy_verification import (
    HeatCalibration,
    VerificationPolicy,
    VerificationTarget,
    heat_score,
    independent_origin_count,
    plan_prefetch,
    sensor_value,
)


def _target(
    target_id: str,
    origin: str,
    *,
    predictability: dict[str, float] | None = None,
) -> VerificationTarget:
    return VerificationTarget(
        target_id,
        frozenset({origin}),
        predictability if predictability is not None else {"default": 0.8},
        f"fetch:{target_id}",
    )


def test_heat_is_weighted_and_calibrated_monotonically():
    raw = heat_score(importance=1, uncertainty=1, conflict=0, recency=0)
    calibration = HeatCalibration(((0.0, 0.0), (0.5, 0.3), (1.0, 1.0)))

    assert raw == pytest.approx(0.65)
    assert calibration.apply(0.75) == pytest.approx(0.65)


def test_transitive_origin_overlap_counts_one_component():
    count = independent_origin_count(
        [frozenset({"a"}), frozenset({"a", "b"}), frozenset({"b"})]
    )

    assert count == 1
    assert independent_origin_count([frozenset({"a"}), frozenset({"b"})]) == 2


def test_sensor_specific_fallback_is_explicit():
    assert sensor_value({"fast": 0.8}, "fast", fallback=0.2) == (0.8, False)
    assert sensor_value({"fast": 0.8}, "slow", fallback=0.2) == (0.2, True)


@pytest.mark.parametrize(
    ("relevance", "heat", "reason"),
    [(0.2, 0.9, "low_decision_relevance"), (0.9, 0.2, "low_heat")],
)
def test_low_value_candidates_do_not_produce_requests(relevance, heat, reason):
    result = plan_prefetch(
        decision_relevance=relevance,
        heat=heat,
        sensor="default",
        sensor_observations={},
        existing_origins=frozenset(),
        targets=(_target("a", "oa"), _target("b", "ob")),
    )

    assert result.eligible is False
    assert result.reason == reason
    assert result.requests == ()


def test_sensor_divergence_becomes_missingness_not_confidence():
    result = plan_prefetch(
        decision_relevance=0.9,
        heat=0.9,
        sensor="default",
        sensor_observations={"one": 0.1, "two": 0.9},
        existing_origins=frozenset(),
        targets=(_target("a", "oa"), _target("b", "ob")),
    )

    assert result.eligible is False
    assert result.missingness is True
    assert result.reason == "sensor_divergence"


def test_plan_selects_only_predictable_nonoverlapping_targets():
    result = plan_prefetch(
        decision_relevance=0.9,
        heat=0.9,
        sensor="default",
        sensor_observations={"one": 0.7, "two": 0.8},
        existing_origins=frozenset({"used"}),
        targets=(
            _target("skip-existing", "used"),
            _target("skip-low", "low", predictability={"default": 0.2}),
            _target("b", "ob"),
            _target("a", "oa"),
        ),
    )

    assert result.eligible is True
    assert result.target_ids == ("a", "b")
    assert result.requests == ("fetch:a", "fetch:b")


def test_missing_sensor_uses_fallback_and_can_fail_closed():
    policy = VerificationPolicy(predictability_fallback=0.1)
    result = plan_prefetch(
        decision_relevance=0.9,
        heat=0.9,
        sensor="unseen",
        sensor_observations={},
        existing_origins=frozenset(),
        targets=(_target("a", "oa"), _target("b", "ob")),
        policy=policy,
    )

    assert result.eligible is False
    assert result.reason == "insufficient_independent_targets"
    assert result.used_fallback is True


def test_overlapping_targets_cannot_satisfy_independence():
    targets = (
        VerificationTarget("a", frozenset({"shared"}), {"default": 0.9}, "fetch:a"),
        VerificationTarget("b", frozenset({"shared"}), {"default": 0.9}, "fetch:b"),
    )

    result = plan_prefetch(
        decision_relevance=0.9,
        heat=0.9,
        sensor="default",
        sensor_observations={},
        existing_origins=frozenset(),
        targets=targets,
    )

    assert result.eligible is False
    assert result.requests == ()
