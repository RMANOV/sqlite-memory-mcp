from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from source_reliability import ReliabilityConfig, SourceReliabilityModel


def test_small_samples_are_neutral_unknown():
    model = SourceReliabilityModel(ReliabilityConfig(minimum_samples=3))
    model.update("wire", True)
    model.update("wire", True)

    estimate = model.estimate("wire")

    assert estimate.label == "UNKNOWN"
    assert estimate.probability == 0.5
    assert model.raw_probability("wire") > 0.5


def test_ema_moves_in_observed_direction_after_minimum_sample():
    good = SourceReliabilityModel(
        ReliabilityConfig(alpha=0.5, minimum_samples=2, ceiling=1.0)
    )
    bad = SourceReliabilityModel(
        ReliabilityConfig(alpha=0.5, minimum_samples=2, floor=0.0)
    )
    for _ in range(2):
        good.update("source", True)
        bad.update("source", False)

    assert good.estimate("source").label == "RELIABLE"
    assert bad.estimate("source").label == "UNRELIABLE"
    assert good.estimate("source").probability == pytest.approx(0.875)
    assert bad.estimate("source").probability == pytest.approx(0.125)


def test_cofire_discount_reduces_correlated_credit():
    independent = SourceReliabilityModel(
        ReliabilityConfig(alpha=0.4, minimum_samples=1, cofire_discount=0.5)
    )
    correlated = SourceReliabilityModel(independent.config)

    independent.update("wire", True)
    correlated.update("wire", True, cofire_sources=("mirror-a", "mirror-b"))

    assert independent.raw_probability("wire") > correlated.raw_probability("wire")


def test_domains_have_independent_estimates():
    model = SourceReliabilityModel(ReliabilityConfig(minimum_samples=1))
    model.update("wire", True, domain="weather")
    model.update("wire", False, domain="finance")

    assert model.raw_probability("wire", domain="weather") > 0.5
    assert model.raw_probability("wire", domain="finance") < 0.5
    assert model.estimate("wire").label == "UNKNOWN"


def test_versioned_json_artifact_round_trips_deterministically():
    model = SourceReliabilityModel(ReliabilityConfig(minimum_samples=1))
    model.update("b", False, domain="d")
    model.update("a", True)

    artifact = model.to_artifact()
    restored = SourceReliabilityModel.from_artifact(artifact)

    assert restored.to_artifact() == artifact
    assert restored.estimate("a") == model.estimate("a")


def test_artifact_rejects_unknown_version_and_duplicate_state():
    model = SourceReliabilityModel(ReliabilityConfig(minimum_samples=1))
    model.update("a", True)
    payload = json.loads(model.to_artifact())
    payload["version"] = 99
    with pytest.raises(ValueError, match="version"):
        SourceReliabilityModel.from_artifact(json.dumps(payload))

    payload["version"] = 1
    payload["states"].append(dict(payload["states"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        SourceReliabilityModel.from_artifact(json.dumps(payload))
