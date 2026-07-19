"""Pure planning gates for selective, decision-relevant verification.

Returned requests are inert descriptors. This module performs no retrieval,
network access, persistence, or task execution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping


def _unit(value: float, name: str) -> float:
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be finite and in [0, 1]")
    return value


@dataclass(frozen=True)
class HeatWeights:
    importance: float = 0.35
    uncertainty: float = 0.3
    conflict: float = 0.25
    recency: float = 0.1

    def __post_init__(self) -> None:
        values = (self.importance, self.uncertainty, self.conflict, self.recency)
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("heat weights must be finite and non-negative")
        if sum(values) <= 0:
            raise ValueError("at least one heat weight is required")


def heat_score(
    *,
    importance: float,
    uncertainty: float,
    conflict: float,
    recency: float,
    weights: HeatWeights | None = None,
) -> float:
    cfg = weights or HeatWeights()
    values = (
        _unit(importance, "importance"),
        _unit(uncertainty, "uncertainty"),
        _unit(conflict, "conflict"),
        _unit(recency, "recency"),
    )
    coefficients = (cfg.importance, cfg.uncertainty, cfg.conflict, cfg.recency)
    return sum(value * weight for value, weight in zip(values, coefficients)) / sum(
        coefficients
    )


@dataclass(frozen=True)
class HeatCalibration:
    """Monotone calibration knots with deterministic linear interpolation."""

    points: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("at least two calibration points are required")
        previous_x = -1.0
        previous_y = -1.0
        for x_value, y_value in self.points:
            _unit(x_value, "raw heat")
            _unit(y_value, "calibrated heat")
            if x_value <= previous_x or y_value < previous_y:
                raise ValueError("calibration points must be monotone")
            previous_x, previous_y = x_value, y_value

    def apply(self, raw: float) -> float:
        raw = _unit(raw, "raw heat")
        if raw <= self.points[0][0]:
            return self.points[0][1]
        if raw >= self.points[-1][0]:
            return self.points[-1][1]
        for (left_x, left_y), (right_x, right_y) in zip(self.points, self.points[1:]):
            if left_x <= raw <= right_x:
                fraction = (raw - left_x) / (right_x - left_x)
                return left_y + fraction * (right_y - left_y)
        raise AssertionError("calibration interval not found")


def independent_origin_count(origin_sets: Iterable[frozenset[str]]) -> int:
    """Count independent components; any transitive overlap is correlated."""
    rows = list(origin_sets)
    if any(not row or any(not value.strip() for value in row) for row in rows):
        raise ValueError("origin sets must be non-empty")
    parents = list(range(len(rows)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            if rows[left] & rows[right]:
                left_root, right_root = find(left), find(right)
                if left_root != right_root:
                    parents[right_root] = left_root
    return len({find(index) for index in range(len(rows))})


def sensor_value(
    values: Mapping[str, float], sensor: str, *, fallback: float
) -> tuple[float, bool]:
    """Return a sensor-specific value and whether an explicit fallback was used."""
    fallback = _unit(fallback, "predictability fallback")
    value = values.get(sensor)
    if value is None:
        return fallback, True
    return _unit(value, f"predictability for {sensor}"), False


@dataclass(frozen=True)
class VerificationTarget:
    target_id: str
    origins: frozenset[str]
    predictability: Mapping[str, float]
    request: str

    def __post_init__(self) -> None:
        if not self.target_id.strip() or not self.request.strip() or not self.origins:
            raise ValueError("verification target fields must be non-empty")


@dataclass(frozen=True)
class VerificationPolicy:
    minimum_relevance: float = 0.6
    minimum_heat: float = 0.55
    minimum_predictability: float = 0.6
    predictability_fallback: float = 0.0
    maximum_sensor_divergence: float = 0.35
    required_independent_targets: int = 2

    def __post_init__(self) -> None:
        for name in (
            "minimum_relevance",
            "minimum_heat",
            "minimum_predictability",
            "predictability_fallback",
            "maximum_sensor_divergence",
        ):
            _unit(getattr(self, name), name)
        if self.required_independent_targets < 1:
            raise ValueError("required_independent_targets must be positive")


@dataclass(frozen=True)
class PrefetchPlan:
    eligible: bool
    reason: str
    requests: tuple[str, ...] = ()
    target_ids: tuple[str, ...] = ()
    missingness: bool = False
    used_fallback: bool = False


def plan_prefetch(
    *,
    decision_relevance: float,
    heat: float,
    sensor: str,
    sensor_observations: Mapping[str, float],
    existing_origins: frozenset[str],
    targets: Iterable[VerificationTarget],
    policy: VerificationPolicy | None = None,
) -> PrefetchPlan:
    """Build a non-executing plan only when every verification gate passes."""
    cfg = policy or VerificationPolicy()
    relevance = _unit(decision_relevance, "decision_relevance")
    heat = _unit(heat, "heat")
    if any(not origin.strip() for origin in existing_origins):
        raise ValueError("existing origins must be non-empty")
    if relevance < cfg.minimum_relevance:
        return PrefetchPlan(False, "low_decision_relevance")
    if heat < cfg.minimum_heat:
        return PrefetchPlan(False, "low_heat")

    observations = [
        _unit(value, f"sensor observation {name}")
        for name, value in sensor_observations.items()
    ]
    if (
        observations
        and max(observations) - min(observations) > cfg.maximum_sensor_divergence
    ):
        return PrefetchPlan(False, "sensor_divergence", missingness=True)

    selected: list[VerificationTarget] = []
    occupied = set(existing_origins)
    used_fallback = False
    for target in sorted(targets, key=lambda item: item.target_id):
        value, fell_back = sensor_value(
            target.predictability,
            sensor,
            fallback=cfg.predictability_fallback,
        )
        used_fallback = used_fallback or fell_back
        if value < cfg.minimum_predictability or target.origins & occupied:
            continue
        selected.append(target)
        occupied.update(target.origins)
        if len(selected) == cfg.required_independent_targets:
            break
    if len(selected) < cfg.required_independent_targets:
        return PrefetchPlan(
            False,
            "insufficient_independent_targets",
            used_fallback=used_fallback,
        )
    return PrefetchPlan(
        True,
        "eligible",
        requests=tuple(item.request for item in selected),
        target_ids=tuple(item.target_id for item in selected),
        used_fallback=used_fallback,
    )
