from __future__ import annotations

import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from prediction_calibration import (
    Forecast,
    Resolution,
    ResolverRegistry,
    SQLitePredictionAdapter,
    ThresholdFireRegistry,
    score_forecast,
    summarize_calibration,
    validate_forecast,
)

NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)


def _forecast(
    prediction_id: str = "p1",
    *,
    probability: float = 0.7,
    days: int = 2,
) -> Forecast:
    return Forecast(
        prediction_id=prediction_id,
        claim_id="claim-1",
        probability=probability,
        issued_at=NOW,
        resolve_by=NOW + timedelta(days=days),
        anchors=("source:1",),
        resolver="mechanical",
    )


@pytest.mark.parametrize("probability", [-0.01, 1.01, math.nan, math.inf])
def test_forecast_probability_is_bounded_and_finite(probability):
    with pytest.raises(ValueError, match="probability"):
        validate_forecast(_forecast(probability=probability))


def test_forecast_requires_anchors_and_bounded_horizon():
    row = _forecast(days=91)
    with pytest.raises(ValueError, match="horizon"):
        validate_forecast(row)
    with pytest.raises(ValueError, match="anchor"):
        validate_forecast(
            Forecast(
                row.prediction_id,
                row.claim_id,
                row.probability,
                row.issued_at,
                NOW + timedelta(days=1),
                (),
                row.resolver,
            )
        )


def test_binary_scoring_and_void_semantics():
    positive = score_forecast(0.8, True)
    negative = score_forecast(0.8, False)

    assert positive is not None and positive.brier == pytest.approx(0.04)
    assert negative is not None and negative.brier == pytest.approx(0.64)
    assert positive.log_score < negative.log_score
    assert score_forecast(0.8, None) is None


def test_resolver_registry_waits_then_resolves_mechanically():
    registry = ResolverRegistry()
    registry.register("mechanical", lambda forecast: forecast.anchors == ("source:1",))
    forecast = _forecast()

    pending = registry.resolve(forecast, now=NOW)
    resolved = registry.resolve(forecast, now=NOW + timedelta(days=3))

    assert pending.status == "pending"
    assert resolved.status == "resolved"
    assert resolved.outcome is True
    assert resolved.score is not None


def test_missing_broken_and_nonbinary_resolvers_void():
    missing = _forecast()
    assert (
        ResolverRegistry().resolve(missing, now=NOW + timedelta(days=3)).reason
        == "resolver_unavailable"
    )

    broken = ResolverRegistry()

    def fail(_forecast):
        raise OSError("offline")

    broken.register("mechanical", fail)
    result = broken.resolve(missing, now=NOW + timedelta(days=3))
    assert result.status == "void"
    assert result.reason == "resolver_error:OSError"

    invalid = ResolverRegistry()
    invalid.register("mechanical", lambda _forecast: "yes")
    assert (
        invalid.resolve(missing, now=NOW + timedelta(days=3)).reason
        == "non_binary_outcome"
    )


def test_summary_flags_unresolved_or_uninformative_samples():
    flat = [
        Resolution(
            _forecast(str(index), probability=0.5),
            "resolved",
            outcome=True,
            score=score_forecast(0.5, True),
        )
        for index in range(5)
    ]
    assert summarize_calibration(flat).theater_flag is True

    mixed = flat[:2] + [Resolution(_forecast(str(index)), "void") for index in range(3)]
    summary = summarize_calibration(mixed)
    assert summary.resolved == 2
    assert summary.void == 3
    assert summary.theater_flag is True


def test_threshold_registry_fires_once_per_key():
    registry = ThresholdFireRegistry()

    assert registry.observe("claim", 0.4, 0.7) is False
    assert registry.observe("claim", 0.7, 0.7) is True
    assert registry.observe("claim", 0.9, 0.7) is False


def test_adapter_refuses_missing_slot_without_creating_schema():
    conn = sqlite3.connect(":memory:")
    adapter = SQLitePredictionAdapter(conn)

    assert adapter.available() is False
    with pytest.raises(RuntimeError, match="unavailable"):
        adapter.insert(_forecast())
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        == 0
    )


def test_adapter_uses_existing_slot_and_terminal_update_is_conditional():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE predictions ("
        "id TEXT PRIMARY KEY, claim_id TEXT, probability REAL, issued_at TEXT, "
        "resolve_by TEXT, anchors_json TEXT, resolver TEXT, status TEXT, "
        "outcome INTEGER, brier REAL, log_score REAL, resolved_at TEXT)"
    )
    adapter = SQLitePredictionAdapter(conn)
    forecast = _forecast()

    assert adapter.available() is True
    assert adapter.insert(forecast) is True
    assert adapter.insert(forecast) is False
    assert adapter.pending_due(NOW + timedelta(days=3)) == (forecast,)

    registry = ResolverRegistry()
    registry.register("mechanical", lambda _forecast: True)
    resolution = registry.resolve(forecast, now=NOW + timedelta(days=3))
    assert adapter.record(resolution, resolved_at=NOW + timedelta(days=3)) is True
    assert adapter.record(resolution, resolved_at=NOW + timedelta(days=3)) is False
    assert adapter.pending_due(NOW + timedelta(days=3)) == ()


def test_adapter_compares_offset_deadlines_as_instants():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE predictions ("
        "id TEXT PRIMARY KEY, claim_id TEXT, probability REAL, issued_at TEXT, "
        "resolve_by TEXT, anchors_json TEXT, resolver TEXT, status TEXT, "
        "outcome INTEGER, brier REAL, log_score REAL, resolved_at TEXT)"
    )
    conn.execute(
        "INSERT INTO predictions "
        "(id, claim_id, probability, issued_at, resolve_by, anchors_json, "
        "resolver, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
        (
            "offset-due",
            "claim-offset",
            0.6,
            "2026-07-19T10:00:00+02:00",
            "2026-07-19T13:00:00+02:00",
            '["source:offset"]',
            "mechanical",
        ),
    )

    due = SQLitePredictionAdapter(conn).pending_due(
        datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    )

    assert tuple(row.prediction_id for row in due) == ("offset-due",)


def test_adapter_rejects_terminal_resolution_before_deadline():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE predictions ("
        "id TEXT PRIMARY KEY, claim_id TEXT, probability REAL, issued_at TEXT, "
        "resolve_by TEXT, anchors_json TEXT, resolver TEXT, status TEXT, "
        "outcome INTEGER, brier REAL, log_score REAL, resolved_at TEXT)"
    )
    adapter = SQLitePredictionAdapter(conn)
    forecast = _forecast()
    assert adapter.insert(forecast) is True
    premature = Resolution(
        forecast,
        "resolved",
        outcome=True,
        score=score_forecast(forecast.probability, True),
    )

    with pytest.raises(ValueError, match="precede resolve_by"):
        adapter.record(premature, resolved_at=NOW + timedelta(days=1))

    assert adapter.pending_due(NOW + timedelta(days=3)) == (forecast,)
