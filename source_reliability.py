"""Deterministic, domain-aware source reliability estimates.

The model is deliberately in-memory. Callers may export a versioned JSON
artifact, but no database table or background update is installed here.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ReliabilityConfig:
    alpha: float = 0.2
    initial: float = 0.5
    minimum_samples: int = 5
    cofire_discount: float = 0.5
    floor: float = 0.01
    ceiling: float = 0.99

    def __post_init__(self) -> None:
        if not 0 < self.alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        if not 0 <= self.initial <= 1:
            raise ValueError("initial must be in [0, 1]")
        if self.minimum_samples < 1:
            raise ValueError("minimum_samples must be positive")
        if not 0 < self.cofire_discount <= 1:
            raise ValueError("cofire_discount must be in (0, 1]")
        if not 0 <= self.floor < self.ceiling <= 1:
            raise ValueError("invalid reliability bounds")


@dataclass(frozen=True)
class ReliabilityEstimate:
    probability: float
    samples: int
    label: str


@dataclass
class _State:
    score: float
    samples: int = 0


class SourceReliabilityModel:
    """EMA estimates keyed by source and optional evidence domain."""

    ARTIFACT_VERSION = 1

    def __init__(self, config: ReliabilityConfig | None = None) -> None:
        self.config = config or ReliabilityConfig()
        self._states: dict[tuple[str, str], _State] = {}

    @staticmethod
    def _key(source: str, domain: str | None) -> tuple[str, str]:
        source = source.strip()
        domain_key = (domain or "*").strip()
        if not source or not domain_key:
            raise ValueError("source and domain must be non-empty")
        return source, domain_key

    def update(
        self,
        source: str,
        correct: bool,
        *,
        domain: str | None = None,
        cofire_sources: Iterable[str] = (),
    ) -> ReliabilityEstimate:
        """Update one source; correlated co-fires reduce this observation's weight."""
        if not isinstance(correct, bool):
            raise TypeError("correct must be boolean")
        key = self._key(source, domain)
        correlated = {
            item.strip()
            for item in cofire_sources
            if item.strip() and item.strip() != key[0]
        }
        weight = self.config.cofire_discount ** len(correlated)
        effective_alpha = self.config.alpha * weight
        state = self._states.setdefault(key, _State(self.config.initial))
        target = 1.0 if correct else 0.0
        state.score += effective_alpha * (target - state.score)
        state.score = min(self.config.ceiling, max(self.config.floor, state.score))
        state.samples += 1
        return self.estimate(source, domain=domain)

    def raw_probability(self, source: str, *, domain: str | None = None) -> float:
        state = self._states.get(self._key(source, domain))
        return state.score if state else self.config.initial

    def estimate(
        self, source: str, *, domain: str | None = None
    ) -> ReliabilityEstimate:
        state = self._states.get(self._key(source, domain))
        if state is None or state.samples < self.config.minimum_samples:
            return ReliabilityEstimate(0.5, state.samples if state else 0, "UNKNOWN")
        if state.score > 0.55:
            label = "RELIABLE"
        elif state.score < 0.45:
            label = "UNRELIABLE"
        else:
            label = "NEUTRAL"
        return ReliabilityEstimate(state.score, state.samples, label)

    def to_artifact(self) -> str:
        rows = [
            {
                "source": source,
                "domain": domain,
                "score": state.score,
                "samples": state.samples,
            }
            for (source, domain), state in sorted(self._states.items())
        ]
        return json.dumps(
            {
                "version": self.ARTIFACT_VERSION,
                "config": asdict(self.config),
                "states": rows,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_artifact(cls, artifact: str) -> SourceReliabilityModel:
        try:
            payload: dict[str, Any] = json.loads(artifact)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid reliability artifact") from exc
        if payload.get("version") != cls.ARTIFACT_VERSION:
            raise ValueError("unsupported reliability artifact version")
        model = cls(ReliabilityConfig(**payload["config"]))
        for row in payload.get("states", []):
            score = float(row["score"])
            samples = int(row["samples"])
            if not math.isfinite(score) or not 0 <= score <= 1 or samples < 0:
                raise ValueError("invalid reliability state")
            key = model._key(str(row["source"]), str(row["domain"]))
            if key in model._states:
                raise ValueError("duplicate reliability state")
            model._states[key] = _State(score=score, samples=samples)
        return model
