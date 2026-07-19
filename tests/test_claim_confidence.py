from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from claim_confidence import (
    ConfidenceConfig,
    Evidence,
    aggregate_confidence,
)


def _evidence(direction: int, reliability: float, origin: str) -> Evidence:
    return Evidence(direction, reliability, frozenset({origin}))


def test_no_evidence_abstains_with_full_interval():
    result = aggregate_confidence([])

    assert result.label == "ABSTAIN"
    assert result.probability == 0.5
    assert result.interval == (0.0, 1.0)


def test_independent_support_can_reach_supported_label():
    result = aggregate_confidence([_evidence(1, 0.8, "a"), _evidence(1, 0.8, "b")])

    assert result.label == "SUPPORTED"
    assert result.probability > 0.8
    assert result.independent_origins == 2


def test_same_origin_is_discounted_by_effective_sample_size():
    correlated = aggregate_confidence(
        [_evidence(1, 0.8, "same"), _evidence(1, 0.8, "same")]
    )
    independent = aggregate_confidence([_evidence(1, 0.8, "a"), _evidence(1, 0.8, "b")])

    assert correlated.effective_sample_size < independent.effective_sample_size
    assert correlated.probability < independent.probability
    assert correlated.label == "ABSTAIN"


def test_transitive_origin_overlap_forms_one_cluster():
    rows = [
        Evidence(1, 0.8, frozenset({"a"})),
        Evidence(1, 0.8, frozenset({"a", "b"})),
        Evidence(1, 0.8, frozenset({"b"})),
    ]

    assert aggregate_confidence(rows).independent_origins == 1


def test_dissent_widens_interval_and_can_force_abstention():
    aligned = aggregate_confidence([_evidence(1, 0.75, "a"), _evidence(1, 0.75, "b")])
    split = aggregate_confidence([_evidence(1, 0.75, "a"), _evidence(-1, 0.75, "b")])

    aligned_width = aligned.interval[1] - aligned.interval[0]
    split_width = split.interval[1] - split.interval[0]
    assert split.dissent_ratio == pytest.approx(1.0)
    assert split_width > aligned_width
    assert split.label == "ABSTAIN"


def test_single_origin_probability_is_capped():
    result = aggregate_confidence(
        [_evidence(1, 0.999, "a")],
        ConfidenceConfig(single_origin_cap=0.77),
    )

    assert result.probability == pytest.approx(0.77)
    assert result.label == "ABSTAIN"


def test_independent_negative_evidence_can_refute():
    result = aggregate_confidence([_evidence(-1, 0.8, "a"), _evidence(-1, 0.8, "b")])

    assert result.label == "REFUTED"
    assert result.probability < 0.2
