"""Small, opt-in primitives for scoring time-bounded binary forecasts.

Nothing in this module creates schema or registers a runtime hook. The SQLite
adapter only operates when a caller supplies an already compatible table.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

_MAX_HORIZON = timedelta(days=90)
_EPSILON = 1e-12


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("forecast timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Forecast:
    prediction_id: str
    claim_id: str
    probability: float
    issued_at: datetime
    resolve_by: datetime
    anchors: tuple[str, ...]
    resolver: str


def validate_forecast(
    forecast: Forecast, *, max_horizon: timedelta = _MAX_HORIZON
) -> Forecast:
    """Validate a forecast without mutating or normalizing caller data."""
    if not forecast.prediction_id.strip() or not forecast.claim_id.strip():
        raise ValueError("prediction_id and claim_id are required")
    if not math.isfinite(forecast.probability) or not 0 <= forecast.probability <= 1:
        raise ValueError("probability must be finite and between zero and one")
    issued = _aware_utc(forecast.issued_at)
    due = _aware_utc(forecast.resolve_by)
    if due <= issued:
        raise ValueError("resolve_by must be later than issued_at")
    if due - issued > max_horizon:
        raise ValueError("forecast horizon exceeds the configured maximum")
    if not forecast.anchors or any(not anchor.strip() for anchor in forecast.anchors):
        raise ValueError("at least one non-empty evidence anchor is required")
    if not forecast.resolver.strip():
        raise ValueError("resolver is required")
    return forecast


@dataclass(frozen=True)
class ForecastScore:
    brier: float
    log_score: float


def score_forecast(probability: float, outcome: bool | None) -> ForecastScore | None:
    """Return proper scores for a binary outcome; ``None`` means void."""
    if outcome is None:
        return None
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("probability must be finite and between zero and one")
    numeric = 1.0 if outcome else 0.0
    brier = (probability - numeric) ** 2
    likelihood = probability if outcome else 1.0 - probability
    return ForecastScore(brier=brier, log_score=-math.log(max(likelihood, _EPSILON)))


@dataclass(frozen=True)
class Resolution:
    forecast: Forecast
    status: str
    outcome: bool | None = None
    score: ForecastScore | None = None
    reason: str | None = None


Resolver = Callable[[Forecast], bool | None]


class ResolverRegistry:
    """Explicit resolver allow-list; resolver failures become auditable voids."""

    def __init__(self) -> None:
        self._resolvers: dict[str, Resolver] = {}

    def register(self, name: str, resolver: Resolver) -> None:
        if not name.strip():
            raise ValueError("resolver name is required")
        if name in self._resolvers:
            raise ValueError(f"resolver already registered: {name}")
        self._resolvers[name] = resolver

    def resolve(self, forecast: Forecast, *, now: datetime) -> Resolution:
        validate_forecast(forecast)
        if _aware_utc(now) < _aware_utc(forecast.resolve_by):
            return Resolution(forecast, "pending", reason="not_due")
        resolver = self._resolvers.get(forecast.resolver)
        if resolver is None:
            return Resolution(forecast, "void", reason="resolver_unavailable")
        try:
            outcome = resolver(forecast)
        except Exception as exc:  # resolver boundaries must fail closed
            return Resolution(
                forecast,
                "void",
                reason=f"resolver_error:{type(exc).__name__}",
            )
        if outcome is not None and not isinstance(outcome, bool):
            return Resolution(forecast, "void", reason="non_binary_outcome")
        if outcome is None:
            return Resolution(forecast, "void", reason="unresolved")
        return Resolution(
            forecast,
            "resolved",
            outcome=outcome,
            score=score_forecast(forecast.probability, outcome),
        )


@dataclass(frozen=True)
class CalibrationSummary:
    total: int
    resolved: int
    void: int
    mean_brier: float | None
    mean_log_score: float | None
    sharpness: float | None
    theater_flag: bool


def summarize_calibration(
    resolutions: Iterable[Resolution],
    *,
    minimum_sample: int = 5,
    minimum_resolution_rate: float = 0.5,
    minimum_sharpness: float = 0.05,
) -> CalibrationSummary:
    """Aggregate scores and flag samples too unresolved or flat to be useful."""
    rows = list(resolutions)
    scored = [row for row in rows if row.status == "resolved" and row.score]
    void = sum(row.status == "void" for row in rows)
    if scored:
        mean_brier = sum(row.score.brier for row in scored if row.score) / len(scored)
        mean_log = sum(row.score.log_score for row in scored if row.score) / len(scored)
        sharpness = sum(
            abs(row.forecast.probability - 0.5) * 2 for row in scored
        ) / len(scored)
    else:
        mean_brier = mean_log = sharpness = None
    resolution_rate = len(scored) / len(rows) if rows else 0.0
    theater = len(rows) >= minimum_sample and (
        resolution_rate < minimum_resolution_rate
        or (sharpness is not None and sharpness < minimum_sharpness)
    )
    return CalibrationSummary(
        total=len(rows),
        resolved=len(scored),
        void=void,
        mean_brier=mean_brier,
        mean_log_score=mean_log,
        sharpness=sharpness,
        theater_flag=theater,
    )


class ThresholdFireRegistry:
    """Process-local duplicate guard for threshold-triggered forecast creation."""

    def __init__(self) -> None:
        self._fired: set[str] = set()

    def observe(self, key: str, value: float, threshold: float) -> bool:
        if not all(math.isfinite(item) for item in (value, threshold)):
            raise ValueError("threshold observations must be finite")
        if key in self._fired or value < threshold:
            return False
        self._fired.add(key)
        return True


class SQLitePredictionAdapter:
    """Zero-DDL adapter for a caller-provided forecast protocol table."""

    TABLE = "predictions"
    REQUIRED_COLUMNS = frozenset(
        {
            "id",
            "claim_id",
            "probability",
            "issued_at",
            "resolve_by",
            "anchors_json",
            "resolver",
            "status",
            "outcome",
            "brier",
            "log_score",
            "resolved_at",
        }
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def available(self) -> bool:
        table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (self.TABLE,),
        ).fetchone()
        if table is None:
            return False
        columns = {
            str(row[1])
            for row in self.conn.execute(f"PRAGMA table_info('{self.TABLE}')")
        }
        return self.REQUIRED_COLUMNS <= columns

    def _require_slot(self) -> None:
        if not self.available():
            raise RuntimeError("compatible prediction storage is unavailable")

    def insert(self, forecast: Forecast) -> bool:
        self._require_slot()
        validate_forecast(forecast)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO predictions "
            "(id, claim_id, probability, issued_at, resolve_by, anchors_json, "
            "resolver, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (
                forecast.prediction_id,
                forecast.claim_id,
                forecast.probability,
                _aware_utc(forecast.issued_at).isoformat(),
                _aware_utc(forecast.resolve_by).isoformat(),
                json.dumps(forecast.anchors),
                forecast.resolver,
            ),
        )
        return cur.rowcount == 1

    def pending_due(self, now: datetime) -> tuple[Forecast, ...]:
        self._require_slot()
        rows = self.conn.execute(
            "SELECT id, claim_id, probability, issued_at, resolve_by, "
            "anchors_json, resolver FROM predictions "
            "WHERE status='pending' "
            "AND julianday(resolve_by)<=julianday(?) "
            "ORDER BY julianday(resolve_by), id",
            (_aware_utc(now).isoformat(),),
        ).fetchall()
        return tuple(
            Forecast(
                prediction_id=str(row[0]),
                claim_id=str(row[1]),
                probability=float(row[2]),
                issued_at=datetime.fromisoformat(str(row[3])),
                resolve_by=datetime.fromisoformat(str(row[4])),
                anchors=tuple(json.loads(str(row[5]))),
                resolver=str(row[6]),
            )
            for row in rows
        )

    def record(self, resolution: Resolution, *, resolved_at: datetime) -> bool:
        self._require_slot()
        if resolution.status not in {"resolved", "void"}:
            raise ValueError("only terminal resolutions can be recorded")
        resolved = _aware_utc(resolved_at)
        if resolved < _aware_utc(resolution.forecast.resolve_by):
            raise ValueError("terminal resolution cannot precede resolve_by")
        score = resolution.score
        cur = self.conn.execute(
            "UPDATE predictions SET status=?, outcome=?, brier=?, log_score=?, "
            "resolved_at=? WHERE id=? AND status='pending'",
            (
                resolution.status,
                None if resolution.outcome is None else int(resolution.outcome),
                score.brier if score else None,
                score.log_score if score else None,
                resolved.isoformat(),
                resolution.forecast.prediction_id,
            ),
        )
        return cur.rowcount == 1
