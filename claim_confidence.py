"""Correlation-aware claim confidence aggregation with explicit abstention."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


def _logit(probability: float) -> float:
    bounded = min(1 - 1e-9, max(1e-9, probability))
    return math.log(bounded / (1 - bounded))


def _logistic(value: float) -> float:
    return 1 / (1 + math.exp(-value))


@dataclass(frozen=True)
class ConfidenceConfig:
    prior_probability: float = 0.5
    same_origin_correlation: float = 0.75
    minimum_independent_origins: int = 2
    abstain_margin: float = 0.08
    single_origin_cap: float = 0.8
    maximum_abs_log_odds: float = 8.0
    base_interval_width: float = 0.18
    dissent_interval_width: float = 0.3

    def __post_init__(self) -> None:
        if not 0 < self.prior_probability < 1:
            raise ValueError("prior_probability must be in (0, 1)")
        if not 0 <= self.same_origin_correlation < 1:
            raise ValueError("same_origin_correlation must be in [0, 1)")
        if self.minimum_independent_origins < 1:
            raise ValueError("minimum_independent_origins must be positive")
        if not 0 <= self.abstain_margin < 0.5:
            raise ValueError("abstain_margin must be in [0, 0.5)")
        if not 0.5 <= self.single_origin_cap < 1:
            raise ValueError("single_origin_cap must be in [0.5, 1)")
        if self.maximum_abs_log_odds <= 0:
            raise ValueError("maximum_abs_log_odds must be positive")
        if self.base_interval_width < 0 or self.dissent_interval_width < 0:
            raise ValueError("interval widths must be non-negative")


@dataclass(frozen=True)
class Evidence:
    direction: int
    reliability: float
    origins: frozenset[str]
    strength: float = 1.0

    def __post_init__(self) -> None:
        if self.direction not in {-1, 1}:
            raise ValueError("direction must be -1 or 1")
        if not math.isfinite(self.reliability) or not 0 < self.reliability < 1:
            raise ValueError("reliability must be finite and in (0, 1)")
        if not self.origins or any(not value.strip() for value in self.origins):
            raise ValueError("evidence requires non-empty origins")
        if not math.isfinite(self.strength) or self.strength <= 0:
            raise ValueError("strength must be finite and positive")


@dataclass(frozen=True)
class ConfidenceResult:
    probability: float
    interval: tuple[float, float]
    label: str
    evidence_count: int
    independent_origins: int
    effective_sample_size: float
    dissent_ratio: float


def _origin_clusters(rows: list[Evidence]) -> list[list[int]]:
    parents = list(range(len(rows)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            if rows[left].origins & rows[right].origins:
                union(left, right)
    grouped: dict[int, list[int]] = {}
    for index in range(len(rows)):
        grouped.setdefault(find(index), []).append(index)
    return list(grouped.values())


def aggregate_confidence(
    evidence: Iterable[Evidence], config: ConfidenceConfig | None = None
) -> ConfidenceResult:
    """Combine evidence in log-odds space and expose uncertainty explicitly."""
    cfg = config or ConfidenceConfig()
    rows = list(evidence)
    if not rows:
        return ConfidenceResult(
            probability=cfg.prior_probability,
            interval=(0.0, 1.0),
            label="ABSTAIN",
            evidence_count=0,
            independent_origins=0,
            effective_sample_size=0.0,
            dissent_ratio=0.0,
        )

    contributions = [
        row.direction * _logit(row.reliability) * row.strength for row in rows
    ]
    clusters = _origin_clusters(rows)
    effective_sample = 0.0
    adjusted = [0.0] * len(rows)
    for cluster in clusters:
        count = len(cluster)
        effective = count / (1 + (count - 1) * cfg.same_origin_correlation)
        effective_sample += effective
        scale = effective / count
        for index in cluster:
            adjusted[index] = contributions[index] * scale

    odds = _logit(cfg.prior_probability) + sum(adjusted)
    odds = min(cfg.maximum_abs_log_odds, max(-cfg.maximum_abs_log_odds, odds))
    probability = _logistic(odds)
    if len(clusters) == 1:
        probability = min(
            cfg.single_origin_cap,
            max(1 - cfg.single_origin_cap, probability),
        )

    positive = sum(value for value in adjusted if value > 0)
    negative = -sum(value for value in adjusted if value < 0)
    total_opinion = positive + negative
    dissent = 2 * min(positive, negative) / total_opinion if total_opinion > 0 else 0.0
    half_width = cfg.base_interval_width / math.sqrt(max(effective_sample, 1.0))
    half_width += cfg.dissent_interval_width * dissent
    interval = (max(0.0, probability - half_width), min(1.0, probability + half_width))

    if (
        len(clusters) < cfg.minimum_independent_origins
        or abs(probability - 0.5) < cfg.abstain_margin
    ):
        label = "ABSTAIN"
    else:
        label = "SUPPORTED" if probability > 0.5 else "REFUTED"
    return ConfidenceResult(
        probability=probability,
        interval=interval,
        label=label,
        evidence_count=len(rows),
        independent_origins=len(clusters),
        effective_sample_size=effective_sample,
        dissent_ratio=dissent,
    )
