"""Lazy Enrichment (Layer 2) — inline claim extraction + periodic health sweep.

L2a: add_observations() → auto-extract SPO claims via regex (+5-15ms inline).
L2b: periodic sweep → dedup, contradictions, staleness, auto-promote.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import uuid
from typing import Any

from db_utils import (
    now_iso,
    tokenize_for_similarity as _tokenize,
)

# ── Adaptive confidence per predicate ──────────────────────────────────────

_PREDICATE_BASE_CONFIDENCE: dict[str, float] = {
    "uses": 0.6,
    "depends_on": 0.7,
    "is": 0.4,
    "requires": 0.65,
    "produces": 0.6,
    "validates": 0.7,
    "contains": 0.5,
    "replaces": 0.55,
}

EVIDENCE_BOOST_PER = 0.1
EVIDENCE_BOOST_CAP = 0.3
REJECTION_PENALTY = 0.15
AUTO_PROMOTE_THRESHOLD = 0.85

# ── Regex patterns (reuse from claim_graph if available) ───────────────────

_PATTERNS: list[tuple[re.Pattern, str]] | None = None


def _get_patterns() -> list[tuple[re.Pattern, str]]:
    """Lazy-load relation patterns, falling back to built-in set."""
    global _PATTERNS
    if _PATTERNS is not None:
        return _PATTERNS
    try:
        from claim_graph import _RELATION_PATTERNS

        _PATTERNS = _RELATION_PATTERNS
    except Exception:
        # Standalone fallback — same patterns without import dependency
        _PATTERNS = [
            (
                re.compile(
                    r"(\b\w[\w\s]{1,40}?)\s+(?:uses?|използва)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
                    re.I,
                ),
                "uses",
            ),
            (
                re.compile(
                    r"(\b\w[\w\s]{1,40}?)\s+(?:depends?\s+on|зависи\s+от)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
                    re.I,
                ),
                "depends_on",
            ),
            (
                re.compile(
                    r"(\b\w[\w\s]{1,40}?)\s+(?:is|е|са)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
                    re.I,
                ),
                "is",
            ),
            (
                re.compile(
                    r"(\b\w[\w\s]{1,40}?)\s+(?:requires?|изисква)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
                    re.I,
                ),
                "requires",
            ),
            (
                re.compile(
                    r"(\b\w[\w\s]{1,40}?)\s+(?:produces?|генерира|създава)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
                    re.I,
                ),
                "produces",
            ),
            (
                re.compile(
                    r"(\b\w[\w\s]{1,40}?)\s+(?:validates?|валидира)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
                    re.I,
                ),
                "validates",
            ),
            (
                re.compile(
                    r"(\b\w[\w\s]{1,40}?)\s+(?:contains?|съдържа)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
                    re.I,
                ),
                "contains",
            ),
            (
                re.compile(
                    r"(\b\w[\w\s]{1,40}?)\s+(?:replaces?|замества|заменя)\s+(\b\w[\w\s]{1,40}?)(?:\.|,|$)",
                    re.I,
                ),
                "replaces",
            ),
        ]
    return _PATTERNS


# ── L2a: Inline extraction ─────────────────────────────────────────────────


def extract_inline_claims(
    conn: sqlite3.Connection,
    entity_id: int,
    observation_id: int,
    observation_text: str,
) -> int:
    """Extract SPO claims from observation text via regex.

    Applies adaptive confidence and auto-promotes if >= threshold.
    Returns count of claims created/updated.
    """
    now = now_iso()
    patterns = _get_patterns()
    count = 0

    for regex, predicate in patterns:
        for m in regex.finditer(observation_text):
            subject = m.group(1).strip()
            object_text = m.group(2).strip()

            # Skip trivially short matches
            if len(subject) < 2 or len(object_text) < 2:
                continue

            # Compute adaptive confidence
            base = _PREDICATE_BASE_CONFIDENCE.get(predicate, 0.5)
            existing = conn.execute(
                "SELECT claim_id, confidence, status FROM lazy_claims "
                "WHERE subject = ? AND predicate = ? AND object_text = ?",
                (subject, predicate, object_text),
            ).fetchone()

            if existing:
                if existing["status"] == "rejected":
                    # Count rejections for penalty
                    rej_count = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM lazy_claims "
                        "WHERE subject = ? AND predicate = ? AND object_text = ? "
                        "AND status = 'rejected'",
                        (subject, predicate, object_text),
                    ).fetchone()["cnt"]
                    confidence = max(0.0, base - REJECTION_PENALTY * rej_count)
                else:
                    # Evidence accumulation — count prior evidence for cap
                    evidence_count = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM lazy_claims "
                        "WHERE subject = ? AND predicate = ? AND object_text = ?",
                        (subject, predicate, object_text),
                    ).fetchone()["cnt"]
                    boost = min(EVIDENCE_BOOST_CAP, evidence_count * EVIDENCE_BOOST_PER)
                    confidence = min(1.0, base + boost)
                # Update existing claim
                conn.execute(
                    "UPDATE lazy_claims SET confidence = ?, updated_at = ? "
                    "WHERE claim_id = ?",
                    (confidence, now, existing["claim_id"]),
                )
                claim_id = existing["claim_id"]
            else:
                # New claim
                confidence = base
                claim_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO lazy_claims "
                    "(claim_id, entity_id, observation_id, subject, predicate, "
                    "object_text, confidence, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)",
                    (
                        claim_id,
                        entity_id,
                        observation_id,
                        subject,
                        predicate,
                        object_text,
                        confidence,
                        now,
                        now,
                    ),
                )
            count += 1

            # Auto-promote if threshold met
            if confidence >= AUTO_PROMOTE_THRESHOLD:
                auto_promote_claim(conn, claim_id, confidence)

    return count


def auto_promote_claim(
    conn: sqlite3.Connection, claim_id: str, confidence: float
) -> str | None:
    """Promote a lazy claim to canonical_facts with validation_mode='auto_lazy'.

    Returns fact_id on success, None if claim not found or already promoted.
    """
    row = conn.execute(
        "SELECT * FROM lazy_claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    if not row or row["status"] == "promoted":
        return None

    now = now_iso()
    fact_id = f"lf-{uuid.uuid4()}"

    try:
        conn.execute(
            "INSERT INTO canonical_facts "
            "(fact_id, subject, predicate, object_text, object_type, fact_scope, "
            "provenance_summary, confidence, validation_mode, source_claim_id, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'text', 'entity', ?, ?, 'auto_lazy', NULL, ?, ?)",
            (
                fact_id,
                row["subject"],
                row["predicate"],
                row["object_text"],
                f"Auto-promoted from lazy_claim {claim_id} (evidence accumulation)",
                confidence,
                now,
                now,
            ),
        )
    except Exception as e:
        logger.warning("auto_promote_claim insert failed: %s", e)
        return None

    conn.execute(
        "UPDATE lazy_claims SET status = 'promoted', promoted_to_fact_id = ?, "
        "updated_at = ? WHERE claim_id = ?",
        (fact_id, now, claim_id),
    )
    return fact_id


# ── L2b: Periodic health sweep ─────────────────────────────────────────────


def detect_near_duplicates(conn: sqlite3.Connection) -> list[dict]:
    """Find near-duplicate observations within the same entity (Jaccard >= 0.7)."""
    results: list[dict] = []
    entities = conn.execute("SELECT id, name FROM entities").fetchall()

    for ent in entities:
        obs = conn.execute(
            "SELECT id, content FROM observations WHERE entity_id = ? ORDER BY id",
            (ent["id"],),
        ).fetchall()
        if len(obs) < 2:
            continue

        # Pairwise Jaccard (O(n^2) but n is small per entity)
        tokenized = [(o["id"], o["content"], _tokenize(o["content"])) for o in obs]
        for i, (id_a, text_a, tokens_a) in enumerate(tokenized):
            if not tokens_a:
                continue
            for id_b, text_b, tokens_b in tokenized[i + 1 :]:
                if not tokens_b:
                    continue
                intersection = tokens_a & tokens_b
                union = tokens_a | tokens_b
                jaccard = len(intersection) / len(union) if union else 0.0
                if jaccard >= 0.7:
                    results.append(
                        {
                            "entity": ent["name"],
                            "entity_id": ent["id"],
                            "obs_a_id": id_a,
                            "obs_b_id": id_b,
                            "jaccard": round(jaccard, 3),
                            "text_a": text_a[:100],
                            "text_b": text_b[:100],
                        }
                    )
    return results


# Opposing predicate pairs for contradiction detection
_OPPOSING_PREDICATES = {
    "uses": "replaces",
    "replaces": "uses",
    "depends_on": "replaces",
    "requires": "replaces",
}


def detect_contradictions(conn: sqlite3.Connection) -> list[dict]:
    """Find contradicting claims for the same entity (opposing predicates)."""
    results: list[dict] = []
    try:
        claims = conn.execute(
            "SELECT claim_id, entity_id, subject, predicate, object_text, confidence "
            "FROM lazy_claims WHERE status != 'rejected' ORDER BY entity_id"
        ).fetchall()
    except Exception:
        return results

    # Group by entity
    by_entity: dict[int, list[dict]] = {}
    for c in claims:
        by_entity.setdefault(c["entity_id"], []).append(dict(c))

    for eid, entity_claims in by_entity.items():
        for i, ca in enumerate(entity_claims):
            opposite = _OPPOSING_PREDICATES.get(ca["predicate"])
            if not opposite:
                continue
            for cb in entity_claims[i + 1 :]:
                if (
                    cb["predicate"] == opposite
                    and ca["subject"] == cb["subject"]
                    and ca["object_text"] == cb["object_text"]
                ):
                    results.append(
                        {
                            "entity_id": eid,
                            "claim_a": ca["claim_id"],
                            "claim_b": cb["claim_id"],
                            "subject": ca["subject"],
                            "predicate_a": ca["predicate"],
                            "predicate_b": cb["predicate"],
                            "object": ca["object_text"],
                        }
                    )
    return results


def detect_stale_entities(
    conn: sqlite3.Connection, staleness_days: int = 90
) -> list[dict]:
    """Find entities with no updates or accesses in N days."""
    results: list[dict] = []
    rows = conn.execute(
        "SELECT e.id, e.name, e.entity_type, e.project, e.updated_at "
        "FROM entities e "
        "WHERE e.updated_at < datetime('now', ? || ' days') "
        "ORDER BY e.updated_at ASC LIMIT 100",
        (f"-{staleness_days}",),
    ).fetchall()

    for r in rows:
        # Check access log for recent access
        try:
            access = conn.execute(
                "SELECT MAX(accessed_at) AS last_access "
                "FROM entity_access_log WHERE entity_id = ?",
                (r["id"],),
            ).fetchone()
            last_access = access["last_access"] if access else None
        except Exception:
            last_access = None

        if last_access:
            from datetime import datetime

            try:
                if datetime.fromisoformat(last_access) > datetime.fromisoformat(
                    r["updated_at"]
                ):
                    continue  # Recently accessed, not stale
            except (ValueError, TypeError):
                if last_access > r["updated_at"]:
                    continue

        results.append(
            {
                "entity_id": r["id"],
                "name": r["name"],
                "entity_type": r["entity_type"],
                "project": r["project"],
                "last_updated": r["updated_at"],
                "last_accessed": last_access,
            }
        )
    return results


def promote_ready_claims(conn: sqlite3.Connection) -> list[dict]:
    """Auto-promote claims with confidence >= threshold."""
    results: list[dict] = []
    try:
        ready = conn.execute(
            "SELECT claim_id, confidence FROM lazy_claims "
            "WHERE status = 'candidate' AND confidence >= ?",
            (AUTO_PROMOTE_THRESHOLD,),
        ).fetchall()
    except Exception:
        return results

    for row in ready:
        fact_id = auto_promote_claim(conn, row["claim_id"], row["confidence"])
        if fact_id:
            results.append(
                {
                    "claim_id": row["claim_id"],
                    "fact_id": fact_id,
                    "confidence": row["confidence"],
                }
            )
    return results


def run_health_sweep(conn: sqlite3.Connection) -> dict:
    """Orchestrate all health checks. Returns JSON-serializable report."""
    report: dict[str, Any] = {}

    report["near_duplicates"] = detect_near_duplicates(conn)
    report["contradictions"] = detect_contradictions(conn)
    report["stale_entities"] = detect_stale_entities(conn)
    report["promoted"] = promote_ready_claims(conn)

    report["summary"] = {
        "duplicates_found": len(report["near_duplicates"]),
        "contradictions_found": len(report["contradictions"]),
        "stale_entities_found": len(report["stale_entities"]),
        "claims_promoted": len(report["promoted"]),
    }
    return report


# ── CLI entrypoint ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    from db_utils import DB_PATH, get_conn

    parser = argparse.ArgumentParser(description="Knowledge health sweep")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report only, no promotions"
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output"
    )
    args = parser.parse_args()

    with get_conn(args.db) as conn:
        if args.dry_run:
            # Skip promotions in dry-run
            report: dict[str, Any] = {
                "near_duplicates": detect_near_duplicates(conn),
                "contradictions": detect_contradictions(conn),
                "stale_entities": detect_stale_entities(conn),
                "promoted": [],
                "summary": {},
            }
            report["summary"] = {
                "duplicates_found": len(report["near_duplicates"]),
                "contradictions_found": len(report["contradictions"]),
                "stale_entities_found": len(report["stale_entities"]),
                "claims_promoted": 0,
                "dry_run": True,
            }
        else:
            report = run_health_sweep(conn)

    if args.json_output:
        print(json.dumps(report, indent=2))
    else:
        s = report["summary"]
        print(f"Near-duplicates: {s['duplicates_found']}")
        print(f"Contradictions:  {s['contradictions_found']}")
        print(f"Stale entities:  {s['stale_entities_found']}")
        print(f"Claims promoted: {s['claims_promoted']}")
        if s.get("dry_run"):
            print("(dry run — no changes applied)")
