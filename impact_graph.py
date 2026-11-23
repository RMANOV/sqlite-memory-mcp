"""Impact Graph — Downstream impact propagation via bounded BFS.

Phase 4 of Intelligence v2:
- Weighted BFS traversal of impact_edges
- Materiality-aware ranking
- Impact summaries for sessions, snapshots, mappings, validations, exports
"""

from __future__ import annotations

import sqlite3
from collections import deque
from typing import Any

from db_utils import now_iso
from intelligence_v2 import (
    _new_id,
    load_config,
    log_enrichment_run,
)

# ── Impact Types ─────────────────────────────────────────────────────────

IMPACT_TYPES = ("informs", "blocks", "risks", "invalidates", "requires_review")

SOURCE_KINDS = ("chunk", "claim", "fact")
TARGET_KINDS = ("session", "snapshot", "mapping", "validation", "export", "fact")

# Depth presets
_DEPTH_LIMITS = {
    "quick": 1,
    "standard": 3,
    "deep": 5,
}


# ── Core: Explain Impact ─────────────────────────────────────────────────


def _context_pack_impact(
    conn: sqlite3.Connection, source_kind: str, source_ref: str
) -> dict[str, Any]:
    """Follow recorded fact provenance to packs, including retained summaries."""
    if not source_kind or not source_ref:
        return {"error": "source_kind and source_ref are required"}
    rows = conn.execute(
        """
        WITH facts(fact_id) AS (
            SELECT subject_ref FROM provenance_links
            WHERE source_kind = :kind AND source_ref = :ref AND subject_kind = 'fact'
            UNION
            SELECT fact_id FROM canonical_facts WHERE :kind = 'fact' AND fact_id = :ref
        ), targets AS (
            SELECT DISTINCT f.fact_id, l.subject_kind, l.subject_ref
            FROM facts f JOIN provenance_links l
              ON l.source_kind = 'fact' AND l.source_ref = f.fact_id
            WHERE l.subject_kind IN ('context_pack', 'artifact')
        ), pack_facts(fact_id, pack_id) AS (
            SELECT fact_id, subject_ref FROM targets WHERE subject_kind = 'context_pack'
            UNION
            SELECT t.fact_id, a.scope_ref
            FROM targets t JOIN memory_artifacts a ON t.subject_ref = a.artifact_id
            WHERE t.subject_kind = 'artifact' AND a.artifact_kind = 'summary'
              AND a.scope_kind = 'context_pack'
        )
        SELECT f.fact_id, pf.pack_id, cp.pack_id IS NOT NULL AS available,
               cp.pack_type, cp.target_ref, summary.artifact_id
        FROM facts f
        LEFT JOIN pack_facts pf ON pf.fact_id = f.fact_id
        LEFT JOIN context_packs cp ON cp.pack_id = pf.pack_id
        LEFT JOIN memory_artifacts summary ON summary.artifact_kind = 'summary'
              AND summary.scope_kind = 'context_pack' AND summary.scope_ref = pf.pack_id
        ORDER BY pf.pack_id, f.fact_id, summary.artifact_id
        """,
        {"kind": source_kind, "ref": source_ref},
    )
    fact_ids = set()
    by_pack = {}
    for fact_id, pack_id, available, pack_type, target_ref, summary_id in rows:
        fact_ids.add(fact_id)
        if pack_id is None:
            continue
        pack = by_pack.setdefault(
            pack_id,
            {
                "pack_id": pack_id,
                "available": bool(available),
                "pack_type": pack_type,
                "target_ref": target_ref,
                "fact_ids": set(),
                "summary_ids": set(),
            },
        )
        pack["fact_ids"].add(fact_id)
        if summary_id is not None:
            pack["summary_ids"].add(summary_id)

    packs = []
    missing_packs = []
    for pack in by_pack.values():
        pack["fact_ids"] = sorted(pack["fact_ids"])
        pack["summary_ids"] = sorted(pack["summary_ids"])
        (packs if pack["available"] else missing_packs).append(pack)
    return {
        "view": "context_packs",
        "source_kind": source_kind,
        "source_ref": source_ref,
        "fact_ids": sorted(fact_ids),
        "packs": packs,
        "missing_packs": missing_packs,
    }


def explain_impact(
    conn: sqlite3.Connection,
    source_kind: str = "chunk",
    source_ref: str = "",
    depth: str = "standard",
    view: str = "impact_edges",
) -> dict[str, Any]:
    """Show downstream impact of a knowledge change via bounded BFS.

    Traverses impact_edges from the source, expanding up to depth levels.
    Returns affected targets grouped by kind, sorted by impact_score.
    """
    config = load_config()
    started = now_iso()

    if not config["enabled"]:
        return {"status": "disabled"}

    if view == "context_packs":
        return _context_pack_impact(conn, source_kind, source_ref)
    if view != "impact_edges":
        return {"error": "view must be impact_edges or context_packs"}

    if source_kind not in SOURCE_KINDS:
        return {
            "error": f"Invalid source_kind: {source_kind}. Use one of: {SOURCE_KINDS}"
        }

    if not source_ref:
        return {"error": "source_ref is required"}

    max_depth = _DEPTH_LIMITS.get(depth, 3)

    # Verify source exists
    source_exists = _verify_source(conn, source_kind, source_ref)
    if not source_exists:
        return {"error": f"Source {source_kind}:{source_ref} not found"}

    # BFS traversal
    visited: set[tuple[str, str]] = set()
    queue: deque[tuple[str, str, int, float]] = (
        deque()
    )  # (kind, ref, depth, cumulative_score)
    queue.append((source_kind, source_ref, 0, 1.0))
    visited.add((source_kind, source_ref))

    impacts: list[dict[str, Any]] = []

    while queue:
        current_kind, current_ref, current_depth, cumulative_score = queue.popleft()

        if current_depth >= max_depth:
            continue

        # Find outgoing edges
        edges = conn.execute(
            "SELECT edge_id, target_kind, target_ref, impact_type, impact_score, rationale "
            "FROM impact_edges "
            "WHERE source_kind = ? AND source_ref = ?",
            (current_kind, current_ref),
        ).fetchall()

        for edge in edges:
            target_key = (edge["target_kind"], edge["target_ref"])
            propagated_score = cumulative_score * edge["impact_score"]

            impacts.append(
                {
                    "edge_id": edge["edge_id"],
                    "source": f"{current_kind}:{current_ref}",
                    "target_kind": edge["target_kind"],
                    "target_ref": edge["target_ref"],
                    "impact_type": edge["impact_type"],
                    "impact_score": round(edge["impact_score"], 3),
                    "propagated_score": round(propagated_score, 3),
                    "depth": current_depth + 1,
                    "rationale": edge["rationale"],
                }
            )

            # Continue BFS if not visited (targets can also be sources)
            if target_key not in visited:
                visited.add(target_key)
                queue.append(
                    (
                        edge["target_kind"],
                        edge["target_ref"],
                        current_depth + 1,
                        propagated_score,
                    )
                )

    # Group by target_kind
    grouped: dict[str, list[dict[str, Any]]] = {}
    for imp in impacts:
        kind = imp["target_kind"]
        grouped.setdefault(kind, []).append(imp)

    # Sort each group by propagated_score descending
    for kind in grouped:
        grouped[kind].sort(key=lambda x: x["propagated_score"], reverse=True)

    # Summary
    summary_parts = []
    for kind in TARGET_KINDS:
        if kind in grouped:
            count = len(grouped[kind])
            max_score = grouped[kind][0]["propagated_score"]
            summary_parts.append(
                f"{kind}: {count} affected (max score: {max_score:.2f})"
            )

    log_enrichment_run(
        conn,
        "explain_impact",
        "success",
        f"{source_kind}:{source_ref}:{depth}",
        started_at=started,
    )

    return {
        "source": f"{source_kind}:{source_ref}",
        "depth": depth,
        "max_depth": max_depth,
        "total_impacts": len(impacts),
        "impacts_by_kind": grouped,
        "summary": "; ".join(summary_parts)
        if summary_parts
        else "No downstream impacts found.",
    }


# ── Impact Edge Management ───────────────────────────────────────────────


def add_impact_edge(
    conn: sqlite3.Connection,
    source_kind: str,
    source_ref: str,
    target_kind: str,
    target_ref: str,
    impact_type: str,
    impact_score: float,
    rationale: str | None = None,
) -> dict[str, Any]:
    """Create a new impact edge in the graph.

    Returns dict with: edge_id, created.
    """
    if source_kind not in SOURCE_KINDS:
        return {"error": f"Invalid source_kind: {source_kind}"}
    if target_kind not in TARGET_KINDS:
        return {"error": f"Invalid target_kind: {target_kind}"}
    if impact_type not in IMPACT_TYPES:
        return {"error": f"Invalid impact_type: {impact_type}"}

    impact_score = max(0.0, min(1.0, impact_score))

    edge_id = _new_id()
    now = now_iso()

    conn.execute(
        "INSERT INTO impact_edges "
        "(edge_id, source_kind, source_ref, target_kind, target_ref, "
        "impact_type, impact_score, rationale, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            edge_id,
            source_kind,
            source_ref,
            target_kind,
            target_ref,
            impact_type,
            impact_score,
            rationale,
            now,
        ),
    )

    return {"edge_id": edge_id, "created": True}


# ── Helpers ──────────────────────────────────────────────────────────────


def _verify_source(conn: sqlite3.Connection, kind: str, ref: str) -> bool:
    """Check if a source entity exists in the appropriate table."""
    if kind == "chunk":
        return (
            conn.execute(
                "SELECT 1 FROM context_chunks WHERE chunk_id = ?", (ref,)
            ).fetchone()
            is not None
        )
    elif kind == "claim":
        return (
            conn.execute(
                "SELECT 1 FROM candidate_claims WHERE claim_id = ?", (ref,)
            ).fetchone()
            is not None
        )
    elif kind == "fact":
        return (
            conn.execute(
                "SELECT 1 FROM canonical_facts WHERE fact_id = ?", (ref,)
            ).fetchone()
            is not None
        )
    return False
