"""Deterministic retrieval contract for remembered-phrase lookup."""

from __future__ import annotations

import re
from typing import Any

RETRIEVAL_CONTRACT_VERSION = "memory_lookup_v2"

LOOKUP_SPLIT_RE = re.compile(r"[\s\-_]+")

SURFACE_SCORE_RULES: dict[str, dict[str, Any]] = {
    "title": {
        "family": "title",
        "exact": 400.0,
        "substring": 320.0,
        "normalized": 280.0,
        "all_words": 220.0,
        "priority": 70,
    },
    "name": {
        "family": "title",
        "exact": 400.0,
        "substring": 320.0,
        "normalized": 280.0,
        "all_words": 220.0,
        "priority": 68,
    },
    "description": {
        "family": "text",
        "exact": 185.0,
        "normalized": 165.0,
        "all_words": 135.0,
        "priority": 60,
    },
    "observations": {
        "family": "text",
        "exact": 180.0,
        "normalized": 160.0,
        "all_words": 130.0,
        "priority": 58,
    },
    "notes": {
        "family": "text",
        "exact": 165.0,
        "normalized": 145.0,
        "all_words": 120.0,
        "priority": 52,
    },
    "project": {
        "family": "text",
        "exact": 150.0,
        "normalized": 130.0,
        "all_words": 110.0,
        "priority": 45,
    },
    "entity_type": {
        "family": "text",
        "exact": 120.0,
        "normalized": 110.0,
        "all_words": 95.0,
        "priority": 35,
    },
}

HIGH_CONFIDENCE_FLOOR = 220.0
MEDIUM_CONFIDENCE_FLOOR = 160.0
VISIBLE_SCORE_FLOOR = 125.0
LOW_SIGNAL_SURFACES = frozenset({"project", "entity_type"})
STRONG_SIGNAL_SURFACES = frozenset(
    {"title", "name", "description", "observations", "notes"}
)


def normalize_lookup_text(text: str | None) -> str:
    """Normalize text for forgiving remembered-phrase comparisons."""
    return " ".join(
        part for part in LOOKUP_SPLIT_RE.split((text or "").casefold()) if part
    )


def score_lookup_surface(surface: str, query: str, candidate: str | None) -> float:
    """Score a query against a single retrieval surface."""
    rules = SURFACE_SCORE_RULES.get(surface)
    raw_q = (query or "").casefold().strip()
    raw_c = (candidate or "").casefold().strip()
    if not rules or not raw_q or not raw_c:
        return 0.0

    norm_q = normalize_lookup_text(raw_q)
    norm_c = normalize_lookup_text(raw_c)
    if rules["family"] == "title":
        if raw_q == raw_c or (norm_q and norm_q == norm_c):
            return float(rules["exact"])
        if raw_q in raw_c:
            return float(rules["substring"]) + min(len(raw_q), 80) / 100.0
        if norm_q and norm_q in norm_c:
            return float(rules["normalized"]) + min(len(norm_q), 80) / 100.0
    else:
        if raw_q in raw_c:
            return float(rules["exact"]) + min(len(raw_q), 80) / 100.0
        if norm_q and norm_q in norm_c:
            return float(rules["normalized"]) + min(len(norm_q), 80) / 100.0

    q_words = [w for w in norm_q.split() if w]
    if q_words and all(w in norm_c for w in q_words):
        return float(rules["all_words"]) + len(q_words)
    return 0.0


def order_surface_hits(surface_scores: dict[str, float]) -> list[tuple[str, float]]:
    """Order surface hits by score, then by signal priority."""
    return sorted(
        surface_scores.items(),
        key=lambda item: (
            float(item[1]),
            int(SURFACE_SCORE_RULES.get(item[0], {}).get("priority", 0)),
            item[0],
        ),
        reverse=True,
    )


def classify_lookup_confidence(surface_scores: dict[str, float]) -> str:
    """Map surface scores to high/medium/low confidence buckets."""
    if not surface_scores:
        return "low"
    ordered = order_surface_hits(surface_scores)
    primary_surface, primary_score = ordered[0]
    if primary_surface in {"title", "name"}:
        return "high"
    if primary_score >= HIGH_CONFIDENCE_FLOOR:
        return "high"
    if (
        primary_score >= MEDIUM_CONFIDENCE_FLOOR
        and primary_surface in STRONG_SIGNAL_SURFACES
    ):
        return "medium"
    if (
        primary_score >= VISIBLE_SCORE_FLOOR
        and primary_surface not in LOW_SIGNAL_SURFACES
    ):
        return "medium"
    return "low"


def is_visible_lookup_match(surface_scores: dict[str, float]) -> bool:
    """Return True when a result is strong enough to show without fallback mode."""
    if not surface_scores:
        return False
    primary_surface, primary_score = order_surface_hits(surface_scores)[0]
    return primary_surface in {"title", "name"} or primary_score >= VISIBLE_SCORE_FLOOR
