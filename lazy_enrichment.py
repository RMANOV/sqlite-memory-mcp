"""Lazy Enrichment (Layer 2) — inline claim extraction + periodic health sweep.

L2a: add_observations() → auto-extract SPO claims via regex (+5-15ms inline).
L2b: periodic sweep → dedup, contradictions, staleness, review candidates.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from db_utils import (
    now_iso,
    tokenize_for_similarity as _tokenize,
)
from enrichment_constants import _PREDICATE_BASE_CONFIDENCE

logger = logging.getLogger("lazy_enrichment")

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
    """Extract review candidates from actual observations, never canonical facts.

    One observation is one source. Replays and copied statements under another
    entity must not inflate the first claim's evidence or confidence.
    """
    from intelligence_v2 import load_config
    if not load_config()["enabled"]:
        return 0
    actual = conn.execute("SELECT entity_id, content FROM observations WHERE id=?", (observation_id,)).fetchone()
    if actual is None or actual["entity_id"] != entity_id or actual["content"] != observation_text:
        return 0
    now = now_iso()
    count = 0
    for regex, predicate in _get_patterns():
        for match in regex.finditer(observation_text):
            subject, object_text = match.group(1).strip(), match.group(2).strip()
            if len(subject) < 2 or len(object_text) < 2:
                continue
            base = _PREDICATE_BASE_CONFIDENCE.get(predicate, 0.5)
            existing = conn.execute(
                "SELECT claim_id, observation_id, status FROM lazy_claims "
                "WHERE entity_id=? AND subject=? AND predicate=? AND object_text=? "
                "ORDER BY rowid DESC LIMIT 1", (entity_id,subject,predicate,object_text),
            ).fetchone()
            if existing:
                if existing["status"] != "candidate":
                    continue
                claim_id = existing["claim_id"]
                # Retain known original lineage for pre-upgrade rows; old
                # inflated hit_count itself is not evidence.
                conn.execute("INSERT OR IGNORE INTO lazy_claim_observations(claim_id,observation_id) VALUES (?,?)", (claim_id,existing["observation_id"]))
                changed=conn.execute("INSERT OR IGNORE INTO lazy_claim_observations(claim_id,observation_id) VALUES (?,?)", (claim_id,observation_id)).rowcount
                if not changed:
                    continue
                hits=conn.execute("SELECT count(*) FROM lazy_claim_observations WHERE claim_id=?", (claim_id,)).fetchone()[0]
                confidence=min(1.0,base+min(EVIDENCE_BOOST_CAP,(hits-1)*EVIDENCE_BOOST_PER))
                conn.execute("UPDATE lazy_claims SET confidence=?,hit_count=?,updated_at=? WHERE claim_id=?", (confidence,hits,now,claim_id))
            else:
                claim_id=str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO lazy_claims(claim_id,entity_id,observation_id,subject,predicate,object_text,confidence,hit_count,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,1,'candidate',?,?)",
                    (claim_id,entity_id,observation_id,subject,predicate,object_text,base,now,now),
                )
                conn.execute("INSERT INTO lazy_claim_observations(claim_id,observation_id) VALUES (?,?)", (claim_id,observation_id))
            count += 1
    return count


def auto_promote_claim(
    conn: sqlite3.Connection,
    claim_id: str,
    confidence: float,
    *,
    has_valid_from: bool | None = None,
) -> str | None:
    """Compatibility hook: confidence never authorizes canonical promotion.

    Use the separate operator CLI with --kind lazy, then consume its approval
    through promote_candidate. Existing callers retain the no-promotion result.
    """
    return None


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
    except sqlite3.OperationalError:
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
    try:
        rows = conn.execute(
            "SELECT e.id, e.name, e.entity_type, e.project, e.updated_at, "
            "MAX(a.accessed_at) AS last_access "
            "FROM entities e "
            "LEFT JOIN entity_access_log a ON a.entity_id = e.id "
            "WHERE e.updated_at < datetime('now', ? || ' days') "
            "GROUP BY e.id, e.name, e.entity_type, e.project, e.updated_at "
            "ORDER BY e.updated_at ASC LIMIT 100",
            (f"-{staleness_days}",),
        ).fetchall()
    except sqlite3.OperationalError:
        # Compatibility with intentionally minimal/legacy schemas.
        rows = conn.execute(
            "SELECT e.id, e.name, e.entity_type, e.project, e.updated_at, "
            "NULL AS last_access FROM entities e "
            "WHERE e.updated_at < datetime('now', ? || ' days') "
            "ORDER BY e.updated_at ASC LIMIT 100",
            (f"-{staleness_days}",),
        ).fetchall()

    for r in rows:
        last_access = r["last_access"]

        if last_access:
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
    """Compatibility hook: automatic sweeps leave candidates for review."""
    return []


def run_health_sweep(conn: sqlite3.Connection) -> dict:
    """Orchestrate all health checks. Returns JSON-serializable report."""
    report: dict[str, Any] = {}

    report["near_duplicates"] = detect_near_duplicates(conn)
    report["contradictions"] = detect_contradictions(conn)
    report["stale_entities"] = detect_stale_entities(conn)
    report["promoted"] = promote_ready_claims(conn)
    report["review_candidates"] = [dict(row) for row in conn.execute(
        "SELECT claim_id,confidence,hit_count FROM lazy_claims "
        "WHERE status='candidate' AND confidence>=? ORDER BY confidence DESC,claim_id",
        (AUTO_PROMOTE_THRESHOLD,),
    )]

    report["summary"] = {
        "duplicates_found": len(report["near_duplicates"]),
        "contradictions_found": len(report["contradictions"]),
        "stale_entities_found": len(report["stale_entities"]),
        "claims_promoted": len(report["promoted"]),
        "claims_awaiting_review": len(report["review_candidates"]),
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
